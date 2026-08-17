import serial
import serial.tools.list_ports
import threading
import time
import struct
import logging
from typing import Optional, Callable

logger = logging.getLogger('Serial_Manager')

# Binary protocol constants
START_BYTE = 0xAA
RESPONSE_BYTE = 0xBB

# Command types (matches finalized STM32 firmware)
CMD_SINE = 0x01       # Bode_Start() sweep point; expects 0xBB response
CMD_CONSTANT = 0x06   # Bode_Start() constant torque; expects 0xBB response
CMD_CENTER = 0x08     # Bode_Center(); no response
CMD_SCALE = 0x09      # Bode_ScaleCal(); no response
CMD_SELFTEST = 0xFE   # Synthetic cosine sweep; expects 0xBB response
CMD_STOP = 0xFF       # Bode_Stop(); no response

# Map effect type names to command bytes
EFFECT_CMD_MAP = {
    'sine': CMD_SINE,
    'constant': CMD_CONSTANT,
    'center': CMD_CENTER,
    'scale': CMD_SCALE,
    'selftest': CMD_SELFTEST,
    'stop': CMD_STOP,
}


def scan_ports() -> list[dict]:
    '''Scan available serial ports and return list of {port, description}.'''
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            'port': p.device,
            'description': f'{p.description} ({p.device})',
        })
    return ports


def _pack_command(effect_type: str, freq_hz: float = 0.0, magnitude: int = 0,
                  settle_periods: int = 0, ramp_periods: int = 0,
                  max_torque_nm: float = 0.0) -> Optional[bytes]:
    '''Pack a command into the 13-byte binary format for the MCU.

    Format: [0xAA][cmd:1B][freqHz:4B][mag:2B][settle:1B][ramp:1B][maxTorque_cH:2B][checksum:1B]
    Struct:  <BBIhBBH (12 bytes) + 1-byte XOR checksum.
    max_torque_nm is encoded as centinewton-meters (e.g. 25.0 Nm = 2500 cN.m).
    '''
    cmd_byte = EFFECT_CMD_MAP.get((effect_type or '').lower())
    if cmd_byte is None:
        logger.error(f'Unknown effect type for serial: {effect_type}')
        return None

    freq_hz_int = int(max(1, min(10000, freq_hz)))
    mag_clamped = max(-32767, min(32767, int(magnitude)))  # int16 signed
    settle_c = max(0, min(4, int(settle_periods)))          # 0..4 (rtP.s_period)
    ramp_c = max(0, min(1, int(ramp_periods)))              # 0..1 (rtP.r_period)
    torque_cH = int(max(0, min(65535, round(max_torque_nm * 100))))  # cN.m

    body = struct.pack(
        '<BBIhBBH',
        START_BYTE,
        cmd_byte,
        freq_hz_int,
        mag_clamped,
        settle_c,
        ramp_c,
        torque_cH,
    )

    # Simple XOR checksum over bytes 0..11
    checksum = 0
    for b in body:
        checksum ^= b
    return body + struct.pack('<B', checksum)


def _parse_response(data: bytes) -> Optional[dict]:
    '''Parse MCU response packet and return Bode measurement data.

    Format: [0xBB][cmd_echo:1B][Mag:4B float dB][Ph_D:4B float deg]
    Struct: <BBff (10 bytes)
    '''
    if len(data) < 10:
        return None

    if data[0] != RESPONSE_BYTE:
        return None

    cmd_echo, mag_db, phase_deg = struct.unpack('<BBff', data[:10])

    return {
        'cmd': cmd_echo,
        'mag_db': round(float(mag_db), 4),
        'phase_deg': round(float(phase_deg), 4),
    }


class SerialManager:
    def __init__(self):
        self._port: Optional[serial.Serial] = None
        self._read_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._on_response: Optional[Callable[[dict], None]] = None
        self._buffer = bytearray()

    @property
    def is_connected(self) -> bool:
        return self._port is not None and self._port.is_open

    @property
    def port_name(self) -> Optional[str]:
        return self._port.port if self._port else None

    def set_response_callback(self, callback: Callable[[dict], None]):
        '''Set callback for incoming MCU response data.'''
        self._on_response = callback

    def connect(self, port: str, baud: int = 115200) -> bool:
        '''Open serial connection to the specified port.'''
        self.disconnect()
        try:
            self._port = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=1.0,
            )
            self._running = True
            self._buffer.clear()
            self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()
            logger.info(f'Serial connected to {port} @ {baud} baud')
            return True
        except Exception as e:
            logger.error(f'Serial connect failed: {e}')
            self._port = None
            return False

    def disconnect(self):
        '''Close serial connection.'''
        self._running = False
        if self._read_thread:
            self._read_thread.join(timeout=1.0)
            self._read_thread = None
        with self._lock:
            if self._port and self._port.is_open:
                try:
                    self._port.close()
                except Exception:
                    pass
            self._port = None
        self._buffer.clear()
        logger.info('Serial disconnected')

    def _write_packet(self, packet: Optional[bytes]) -> bool:
        '''Write a packed command to the serial port under lock.'''
        if packet is None:
            return False
        with self._lock:
            try:
                self._port.write(packet)
                return True
            except Exception as e:
                logger.error(f'Serial write failed: {e}')
                return False

    def send_effect(self, effect_type: str, freq_hz: float, magnitude: int,
                    settle_periods: int = 2, ramp_periods: int = 1,
                    max_torque_nm: float = 25.0) -> bool:
        '''Send a sweep-point effect command (SINE/CONSTANT/SELFTEST).

        Returns True on successful write, False otherwise.
        '''
        if not self.is_connected:
            logger.warning('Serial not connected, cannot send effect')
            return False

        packet = _pack_command(effect_type, freq_hz, magnitude,
                               settle_periods, ramp_periods, max_torque_nm)
        if packet is None:
            return False

        ok = self._write_packet(packet)
        if ok:
            logger.info(f'Serial sent: type={effect_type} freq={freq_hz} mag={magnitude} '
                        f'settle={settle_periods} ramp={ramp_periods} '
                        f'maxTorque={max_torque_nm:.1f}Nm')
        return ok

    def send_stop(self) -> bool:
        '''Send stop command (Bode_Stop). No response expected.'''
        if not self.is_connected:
            return False
        return self._write_packet(_pack_command('stop'))

    def send_center(self) -> bool:
        '''Send center command (Bode_Center). No response expected.'''
        if not self.is_connected:
            return False
        return self._write_packet(_pack_command('center'))

    def send_scale(self) -> bool:
        '''Send scale calibration command (Bode_ScaleCal). No response expected.'''
        if not self.is_connected:
            return False
        return self._write_packet(_pack_command('scale'))

    def send_selftest(self) -> bool:
        '''Send selftest command (synthetic cosine sweep). Expects 0xBB response.'''
        if not self.is_connected:
            return False
        return self._write_packet(_pack_command('selftest'))

    def _read_loop(self):
        '''Background thread: read bytes from serial port.'''
        while self._running:
            try:
                if self._port and self._port.is_open and self._port.in_waiting > 0:
                    data = self._port.read(self._port.in_waiting)
                    self._buffer.extend(data)
                    self._process_buffer()
                else:
                    time.sleep(0.005)
            except Exception as e:
                if self._running:
                    logger.error(f'Serial read error: {e}')
                    time.sleep(0.1)

    def _process_buffer(self):
        '''Extract and process complete 10-byte response packets.'''
        while len(self._buffer) >= 10:
            start_idx = -1
            for i in range(len(self._buffer)):
                if self._buffer[i] == RESPONSE_BYTE:
                    start_idx = i
                    break

            if start_idx == -1:
                self._buffer.clear()
                break

            if start_idx > 0:
                self._buffer = self._buffer[start_idx:]
                start_idx = 0

            if len(self._buffer) < 10:
                break

            packet = bytes(self._buffer[:10])
            parsed = _parse_response(packet)
            if parsed and self._on_response:
                try:
                    self._on_response(parsed)
                except Exception as e:
                    logger.error(f'Response callback error: {e}')

            self._buffer = self._buffer[10:]


# Global singleton instance
serial_manager = SerialManager()