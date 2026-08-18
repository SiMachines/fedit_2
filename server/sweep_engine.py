'''
Sweep Engine - Sinestream-style frequency sweep for system identification.

Generates a sequence of sine wave commands at specified frequencies,
sends them over serial, records the MCU Bode response (magnitude in dB
and phase shift in degrees), and produces Bode-plot-ready data.
'''

import time
import threading
import json
from typing import Optional, Callable
from dataclasses import dataclass, field

# Estimated number of measurement cycles the MCU runs per sweep point.
# Used only to size the host-side response timeout (the MCU replies when Ready).
MEASURE_CYCLES_ESTIMATE = 8


@dataclass
class SweepConfig:
    '''Configuration for a sinestream sweep.'''
    # Frequency range
    freq_min_hz: float = 30.0  # min 30 Hz: below this the firmware w_avg moving average overflows
    freq_max_hz: float = 500.0
    num_frequencies: int = 20
    spacing: str = 'logarithmic'  # 'logarithmic' or 'linear'

    # Signal parameters
    amplitude: int = 3276  # 10% of full scale (32767)
    max_torque_nm: float = 25.0
    settling_periods: int = 2  # 0..4 (rtP.s_period)
    ramp_periods: int = 1      # 0..1 (rtP.r_period)

    # Command type to send for each sweep point (e.g. 'sine', 'constant', 'selftest')
    command_type: str = 'sine'

    # Execution
    single_simulation: bool = True
    perform_filtering: bool = True

    def get_frequencies(self) -> list[float]:
        '''Compute the frequency list based on config.'''
        if self.num_frequencies < 1:
            return []
        if self.num_frequencies == 1:
            return [self.freq_min_hz]

        if self.spacing == 'logarithmic':
            ratio = (self.freq_max_hz / self.freq_min_hz) ** (1.0 / (self.num_frequencies - 1))
            return [self.freq_min_hz * (ratio ** i) for i in range(self.num_frequencies)]
        else:
            step = (self.freq_max_hz - self.freq_min_hz) / (self.num_frequencies - 1)
            return [self.freq_min_hz + step * i for i in range(self.num_frequencies)]

    def get_period_duration(self, freq_hz: float) -> float:
        '''Return the duration of one period in seconds.'''
        return 1.0 / max(freq_hz, 0.1)


@dataclass
class SweepResult:
    '''Bode measurement result for a single frequency point.'''
    freq_hz: float
    mag_db: float = 0.0      # Measured magnitude in dB
    phase_deg: float = 0.0   # Measured phase shift in degrees
    valid: bool = False


@dataclass
class SweepData:
    '''Complete sweep results.'''
    config: SweepConfig = field(default_factory=SweepConfig)
    results: list[SweepResult] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    status: str = 'idle'  # idle, running, complete, cancelled, error

    def to_dict(self):
        return {
            'config': {
                'freq_min_hz': self.config.freq_min_hz,
                'freq_max_hz': self.config.freq_max_hz,
                'num_frequencies': self.config.num_frequencies,
                'spacing': self.config.spacing,
                'amplitude': self.config.amplitude,
                'max_torque_nm': self.config.max_torque_nm,
                'settling_periods': self.config.settling_periods,
                'ramp_periods': self.config.ramp_periods,
            },
            'results': [
                {
                    'freq_hz': r.freq_hz,
                    'mag_db': r.mag_db,
                    'phase_deg': r.phase_deg,
                    'valid': r.valid,
                }
                for r in self.results
            ],
            'status': self.status,
            'duration_s': round(self.end_time - self.start_time, 2),
        }


class SweepEngine:
    '''
    Engine that runs a sinestream sweep:
    1. Generates frequency list from SweepConfig
    2. For each frequency, calls a send_effect callback
    3. Waits for the MCU Bode response via on_response callback
    4. Records magnitude (dB) and phase (deg)
    5. Reports progress and completion
    '''

    def __init__(self):
        self._data: Optional[SweepData] = None
        self._running = False
        self._response_event = threading.Event()
        self._response_data: Optional[dict] = None
        self._cancel_event = threading.Event()

        # Callbacks (to be set by the UI)
        self.on_send_effect: Optional[Callable] = None
        self.on_send_stop: Optional[Callable[[], None]] = None
        self.on_progress: Optional[Callable[[int, int, str], None]] = None
        self.on_complete: Optional[Callable[[SweepData], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None
        self.on_check_connection: Optional[Callable[[], bool]] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def _log(self, msg: str, level: str = 'info'):
        if self.on_log:
            self.on_log(msg, level)

    def set_callbacks(self,
                      on_send_effect: Optional[Callable] = None,
                      on_send_stop: Optional[Callable] = None,
                      on_progress: Optional[Callable] = None,
                      on_complete: Optional[Callable] = None,
                      on_log: Optional[Callable] = None,
                      on_check_connection: Optional[Callable] = None):
        self.on_send_effect = on_send_effect
        self.on_send_stop = on_send_stop
        self.on_progress = on_progress
        self.on_complete = on_complete
        self.on_log = on_log
        self.on_check_connection = on_check_connection

    def handle_response(self, data: dict):
        '''Called by the serial manager when a response packet arrives.'''
        self._response_data = data
        self._response_event.set()

    def run_sweep(self, config: SweepConfig):
        '''Run a sinestream sweep in a background thread.'''
        if self._running:
            self._log('Sweep already running')
            return

        self._data = SweepData(config=config)
        self._data.status = 'running'
        self._data.start_time = time.time()
        self._data.results = []
        self._running = True
        self._cancel_event.clear()

        self._log(f'Starting sweep: {config.num_frequencies} frequencies, '
                  f'{config.freq_min_hz:.1f}-{config.freq_max_hz:.1f} Hz '
                  f'({config.spacing})')

        frequencies = config.get_frequencies()
        total = len(frequencies)

        def _worker():
            try:
                for idx, freq_hz in enumerate(frequencies):
                    if self._cancel_event.is_set():
                        self._data.status = 'cancelled'
                        self._log('Sweep cancelled by user')
                        break

                    self._on_frequency_start(idx + 1, total, freq_hz)

                    period_dur = config.get_period_duration(freq_hz)

                    self._log(f'  Freq {freq_hz:.2f} Hz: sweeping '
                              f'(settle={config.settling_periods}, ramp={config.ramp_periods})')

                    # Clear any stale response
                    self._response_data = None
                    self._response_event.clear()

                    # Send the sweep-point command; the MCU runs settle + measure
                    # internally and replies with a 0xBB response when Ready.
                    if self.on_send_effect:
                        self.on_send_effect(config.command_type, freq_hz, config.amplitude,
                                            config.settling_periods,
                                            config.ramp_periods,
                                            config.max_torque_nm)

                    # Wait for the MCU response. Account for settle + ramp +
                    # measure cycles (each scaled by the period duration), add
                    # a fixed buffer for USB/processing overhead, then a 10%
                    # margin so the MCU has time to actually output before we
                    # consider it a timeout. Low frequencies need more headroom.
                    base_timeout = period_dur * (config.settling_periods +
                                                 config.ramp_periods +
                                                 MEASURE_CYCLES_ESTIMATE) + 2.0
                    timeout = base_timeout * 1.10
                    got_response = self._response_event.wait(timeout=timeout)

                    result = SweepResult(freq_hz=freq_hz)

                    if got_response and self._response_data:
                        rd = self._response_data
                        result.mag_db = rd.get('mag_db', 0.0)
                        result.phase_deg = rd.get('phase_deg', 0.0)
                        result.valid = True
                        self._log(f'  Result: Mag = {result.mag_db:.2f} dB, '
                                  f'Phase = {result.phase_deg:.2f} deg')
                    else:
                        # The MCU did not respond within the settle + ramp +
                        # measure window. This means the MCU is not running the
                        # expected sweep, so abort the whole sweep.
                        self._log('  Result: No response (timeout) - MCU did not '
                                  'respond when expected, stopping sweep',
                                  level='error')
                        self._data.status = 'error'
                        self._data.results.append(result)
                        break

                    self._data.results.append(result)

                    # If the MCU connection was lost at any point, stop the sweep.
                    if self.on_check_connection and not self.on_check_connection():
                        self._log('MCU connection lost - stopping sweep',
                                  level='error')
                        self._data.status = 'error'
                        break

                    # Small gap between frequencies
                    if idx < total - 1:
                        gap_ms = max(50, int(period_dur * 0.5 * 1000))
                        time.sleep(gap_ms / 1000.0)

                    # Report progress
                    if self.on_progress:
                        self.on_progress(idx + 1, total, f'Freq {freq_hz:.1f} Hz')

            except Exception as e:
                self._log(f'Sweep error: {e}', level='error')
                self._data.status = 'error'
            finally:
                # Send stop command
                if self.on_send_stop:
                    try:
                        self.on_send_stop()
                    except Exception:
                        pass

                self._running = False
                self._data.end_time = time.time()

                if self._data.status == 'running':
                    self._data.status = 'complete'

                # Calculate stats
                valid_results = [r for r in self._data.results if r.valid]
                self._log(f'Sweep {self._data.status}: '
                          f'{len(valid_results)}/{len(self._data.results)} valid responses '
                          f'in {self._data.end_time - self._data.start_time:.1f}s')

                if self.on_complete:
                    self.on_complete(self._data)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def cancel_sweep(self):
        '''Cancel a running sweep.'''
        self._cancel_event.set()
        if self.on_send_stop:
            try:
                self.on_send_stop()
            except Exception:
                pass

    def get_data(self) -> Optional[SweepData]:
        return self._data

    def export_results(self, path: str):
        '''Export sweep results to JSON.'''
        if self._data is None:
            return
        try:
            with open(path, 'w') as f:
                json.dump(self._data.to_dict(), f, indent=2)
            self._log(f'Results exported to {path}')
        except Exception as e:
            self._log(f'Export failed: {e}', level='error')

    def _on_frequency_start(self, idx: int, total: int, freq_hz: float):
        '''Called before each frequency starts (override for UI updates).'''
        pass


# Global singleton
sweep_engine = SweepEngine()