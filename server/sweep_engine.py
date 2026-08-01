"""
Sweep Engine — Sinestream-style frequency sweep for system identification.

Generates a sequence of sine wave clips at specified frequencies,
sends them over serial, records MCU response timing (Δ = T2 - T1),
and produces Bode-plot-ready data.
"""

import math
import time
import threading
from typing import Optional, Callable
from dataclasses import dataclass, field
import json


@dataclass
class SweepConfig:
    """Configuration for a sinestream sweep."""
    # Frequency range
    freq_min_hz: float = 10.0
    freq_max_hz: float = 500.0
    num_frequencies: int = 20
    spacing: str = "logarithmic"  # "logarithmic" or "linear"

    # Signal parameters
    amplitude: int = 3276  # 10% of full scale (32767)
    max_torque_nm: float = 25.0
    settling_periods: int = 2
    num_periods: int = 5
    ramp_periods: int = 1

    # Execution
    single_simulation: bool = True
    perform_filtering: bool = True

    def get_frequencies(self) -> list[float]:
        """Compute the frequency list based on config."""
        if self.num_frequencies < 1:
            return []
        if self.num_frequencies == 1:
            return [self.freq_min_hz]

        if self.spacing == "logarithmic":
            ratio = (self.freq_max_hz / self.freq_min_hz) ** (1.0 / (self.num_frequencies - 1))
            return [self.freq_min_hz * (ratio ** i) for i in range(self.num_frequencies)]
        else:
            step = (self.freq_max_hz - self.freq_min_hz) / (self.num_frequencies - 1)
            return [self.freq_min_hz + step * i for i in range(self.num_frequencies)]

    def get_period_duration(self, freq_hz: float) -> float:
        """Return the duration of one period in seconds."""
        return 1.0 / max(freq_hz, 0.1)


@dataclass
class SweepResult:
    """Results from a single frequency point in the sweep."""
    freq_hz: float
    t1: int = 0       # T1 timestamp from host
    t2: int = 0       # T2 timestamp from MCU
    delta_us: int = 0 # Response delta in microseconds
    delta_ms: float = 0.0
    valid: bool = False


@dataclass
class SweepData:
    """Complete sweep results."""
    config: SweepConfig = field(default_factory=SweepConfig)
    results: list[SweepResult] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    status: str = "idle"  # idle, running, complete, cancelled, error

    def to_dict(self):
        return {
            "config": {
                "freq_min_hz": self.config.freq_min_hz,
                "freq_max_hz": self.config.freq_max_hz,
                "num_frequencies": self.config.num_frequencies,
                "spacing": self.config.spacing,
                "amplitude": self.config.amplitude,
                "settling_periods": self.config.settling_periods,
                "num_periods": self.config.num_periods,
                "ramp_periods": self.config.ramp_periods,
            },
            "results": [
                {
                    "freq_hz": r.freq_hz,
                    "delta_us": r.delta_us,
                    "delta_ms": r.delta_ms,
                    "valid": r.valid,
                }
                for r in self.results
            ],
            "status": self.status,
            "duration_s": round(self.end_time - self.start_time, 2),
        }


class SweepEngine:
    """
    Engine that runs a sinestream sweep:
    1. Generates frequency list from SweepConfig
    2. For each frequency, calls a send_effect callback
    3. Waits for response via on_response callback
    4. Records timing data (T1, T2, Δ)
    5. Reports progress and completion
    """

    def __init__(self):
        self._data: Optional[SweepData] = None
        self._running = False
        self._current_result: Optional[SweepResult] = None
        self._response_event = threading.Event()
        self._response_data: Optional[dict] = None
        self._cancel_event = threading.Event()

        # Callbacks (to be set by the UI)
        self.on_send_effect: Optional[Callable] = None
        self.on_send_stop: Optional[Callable[[], None]] = None
        self.on_progress: Optional[Callable[[int, int, str], None]] = None
        self.on_complete: Optional[Callable[[SweepData], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def _log(self, msg: str):
        if self.on_log:
            self.on_log(msg)

    def set_callbacks(self,
                      on_send_effect: Optional[Callable] = None,
                      on_send_stop: Optional[Callable] = None,
                      on_progress: Optional[Callable] = None,
                      on_complete: Optional[Callable] = None,
                      on_log: Optional[Callable] = None):
        self.on_send_effect = on_send_effect
        self.on_send_stop = on_send_stop
        self.on_progress = on_progress
        self.on_complete = on_complete
        self.on_log = on_log

    def handle_response(self, data: dict):
        """Called by the serial manager when a response packet arrives."""
        self._response_data = data
        self._response_event.set()

    def run_sweep(self, config: SweepConfig):
        """Run a sinestream sweep in a background thread."""
        if self._running:
            self._log("Sweep already running")
            return

        self._data = SweepData(config=config)
        self._data.status = "running"
        self._data.start_time = time.time()
        self._data.results = []
        self._running = True
        self._cancel_event.clear()

        self._log(f"Starting sweep: {config.num_frequencies} frequencies, "
                   f"{config.freq_min_hz:.1f}-{config.freq_max_hz:.1f} Hz "
                   f"({config.spacing})")

        frequencies = config.get_frequencies()
        total = len(frequencies)

        def _worker():
            try:
                for idx, freq_hz in enumerate(frequencies):
                    if self._cancel_event.is_set():
                        self._data.status = "cancelled"
                        self._log("Sweep cancelled by user")
                        break

                    self._on_frequency_start(idx + 1, total, freq_hz)

                    # Calculate durations
                    period_dur = config.get_period_duration(freq_hz)
                    settling_dur = period_dur * config.settling_periods
                    measure_dur = period_dur * config.num_periods
                    total_dur_ms = int((settling_dur + measure_dur) * 1000)

                    # --- Settling phase (transients settle, no response capture) ---
                    self._log(f"  Freq {freq_hz:.2f} Hz: settling "
                               f"({config.settling_periods} periods = {settling_dur*1000:.0f}ms)")
                    if config.settling_periods > 0:
                        # Send a sine at this frequency for settling
                        if self.on_send_effect:
                            settling_ms = int(settling_dur * 1000) + 50
                            self.on_send_effect("sine", freq_hz, config.amplitude, settling_ms,
                                                config.max_torque_nm,
                                                config.settling_periods,
                                                config.num_periods,
                                                config.ramp_periods)
                        time.sleep(settling_dur + 0.02)

                    # --- Measurement phase (capture T1/T2/Δ) ---
                    self._log(f"  Freq {freq_hz:.2f} Hz: measuring "
                               f"({config.num_periods} periods = {measure_dur*1000:.0f}ms)")

                    # Clear any stale response
                    self._response_data = None
                    self._response_event.clear()

                    # Send measurement sine
                    measure_ms = int(measure_dur * 1000) + 50
                    t1 = None
                    if self.on_send_effect:
                        t1 = self.on_send_effect("sine", freq_hz, config.amplitude, measure_ms,
                                                 config.max_torque_nm,
                                                 config.settling_periods,
                                                 config.num_periods,
                                                 config.ramp_periods)

                    # Wait for MCU response with timeout slightly longer than the effect
                    timeout = measure_dur + 0.5
                    got_response = self._response_event.wait(timeout=timeout)

                    result = SweepResult(freq_hz=freq_hz)

                    if got_response and self._response_data:
                        rd = self._response_data
                        result.t1 = rd.get("t1", 0)
                        result.t2 = rd.get("t2", 0)
                        result.delta_us = rd.get("delta_us", 0)
                        result.delta_ms = rd.get("delta_ms", 0.0)
                        result.valid = True
                        self._log(f"  → Δ = {result.delta_ms:.3f} ms "
                                   f"(T1={result.t1} T2={result.t2})")
                    else:
                        self._log(f"  → No response (timeout)")
                        result.valid = False

                    self._data.results.append(result)

                    # Small gap between frequencies
                    if idx < total - 1:
                        gap_ms = max(50, int(period_dur * 0.5 * 1000))
                        time.sleep(gap_ms / 1000.0)

                    # Report progress
                    if self.on_progress:
                        pct = int((idx + 1) / total * 100)
                        self.on_progress(idx + 1, total, f"Freq {freq_hz:.1f} Hz")

            except Exception as e:
                self._log(f"Sweep error: {e}")
                self._data.status = "error"
            finally:
                # Send stop command
                if self.on_send_stop:
                    try:
                        self.on_send_stop()
                    except Exception:
                        pass

                self._running = False
                self._data.end_time = time.time()

                if self._data.status == "running":
                    self._data.status = "complete"

                # Calculate stats
                valid_results = [r for r in self._data.results if r.valid]
                self._log(f"Sweep {self._data.status}: "
                           f"{len(valid_results)}/{len(self._data.results)} valid responses "
                           f"in {self._data.end_time - self._data.start_time:.1f}s")

                if self.on_complete:
                    self.on_complete(self._data)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def cancel_sweep(self):
        """Cancel a running sweep."""
        self._cancel_event.set()
        if self.on_send_stop:
            try:
                self.on_send_stop()
            except Exception:
                pass

    def get_data(self) -> Optional[SweepData]:
        return self._data

    def export_results(self, path: str):
        """Export sweep results to JSON."""
        if self._data is None:
            return
        try:
            with open(path, "w") as f:
                json.dump(self._data.to_dict(), f, indent=2)
            self._log(f"Results exported to {path}")
        except Exception as e:
            self._log(f"Export failed: {e}")

    def _on_frequency_start(self, idx: int, total: int, freq_hz: float):
        """Called before each frequency starts (override for UI updates)."""
        pass


# Global singleton
sweep_engine = SweepEngine()