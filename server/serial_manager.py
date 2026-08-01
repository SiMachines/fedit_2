import serial
import serial.tools.list_ports
import threading
import time
import struct
import logging
from typing import Optional, Callable

logger = logging.getLogger("Serial_Manager")

# Binary protocol constants
START_BYTE = 0xAA
RESPONSE_BYTE = 0xBB

# Command types
CMD_SINE = 0x01
CMD_SQUARE = 0x02
CMD_TRIANGLE = 0x03
CMD_SAWTOOTHUP = 0x04
CMD_SAWTOOTHDOWN = 0x05
CMD_CONSTANT = 0x06
CMD_RAMP = 0x07
CMD_STOP = 0xFF

# Map effect type names to command bytes
EFFECT_CMD_MAP = {
    "sine": CMD_SINE,
    "square": CMD_SQUARE,
    "triangle": CMD_TRIANGLE,
    "sawtoothup": CMD_SAWTOOTHUP,
    "sawtoothdown": CMD_SAWTOOTHDOWN,
    "constant": CMD_CONSTANT,
    "ramp": CMD_RAMP,
}


def scan_ports() -> list[dict]:
    """Scan available serial ports and return list of {port, description}."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "port": p.device,
            "description": f"{p.description} ({p.device})",
        })
    return ports


def _pack_command(effect_type: str, freq_hz: float, magnitude: int, duration_ms: int,
                  max_torque_nm: float = 25.0,
                  settle_periods: int = 2,
                  num_periods: int = 5,
                  ramp_periods: int = 1) -> Optional[bytes]:
    """Pack effect parameters into binary format for MCU.

    Format: [0xAA][cmd:1B][T1:8B][freqHz:4B][mag:2B][durMs:2B]
            [maxTorqueNm_cH:2B][settle:1B][numPeriods:1B][ramp:1B][checksum:1B]
    max_torque_nm is encoded as hundredths of Nm (e.g. 25.0 Nm -> 2500).
    """
    cmd_byte = EFFECT_CMD_MAP.get(effect_type.lower())
    if cmd_byte is None:
        logger.error(f"Unknown effect type for serial: {effect_type}")
        return None

    # T1 timestamp: microseconds since epoch (8 bytes)
    t1_us = int(time.time() * 1_000_000)

    freq_hz_int = int(max(1, min(10000, freq_hz)))
    mag_clamped = max(0, min(32767, abs(magnitude)))
    dur_clamped = max(10, min(60000, duration_ms))
    torque_cH = int(max(1, min(10000, round(max_torque_nm * 100))))  # 0.01 Nm resolution
    settle_c = max(0, min(255, int(settle_periods)))
    num_c = max(1, min(255, int(num_periods)))
    ramp_c = max(0, min(255, int(ramp_periods)))

    data = struct.pack(
        "<BBQHHIHBBB",
        START_BYTE,
        cmd_byte,
        t1_us,
        freq_hz_int,
        mag_clamped,
        dur_clamped,
        torque_cH,
        settle_c,
        num_c,
        ramp_c,
    )

    # Simple XOR checksum
    checksum = 0
    for b in data:
        checksum ^= b
    data += struct.pack("<B", checksum)

    return data


def _parse_response(data: bytes) -> Optional[dict]:
    """Parse MCU response binary packet and return timing data."""
    if len(data) < 15:
        return None

    if data[0] != RESPONSE_BYTE:
        return None

    cmd_byte = data[1]
    t1_recv = struct.unpack("<Q", data[2:10])[0]  # T1 echoed back
    t2_mcu = struct.unpack("<Q", data[10:18])[0]  # T2 from MCU

    delta_us = t2_mcu - t1_recv

    return {
        "cmd": cmd_byte,
        "t1": t1_recv,
        "t2": t2_mcu,
        "delta_us": delta_us,
        "delta_ms": round(delta_us / 1000.0, 3),
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
        """Set callback for incoming MCU response data."""
        self._on_response = callback

    def connect(self, port: str, baud: int = 115200) -> bool:
        """Open serial connection to the specified port."""
        self.disconnect()
        try:
            self._port = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
            )
            self._running = True
            self._buffer.clear()
            self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()
            logger.info(f"Serial connected to {port} @ {baud} baud")
            return True
        except Exception as e:
            logger.error(f"Serial connect failed: {e}")
            self._port = None
            return False

    def disconnect(self):
        """Close serial connection."""
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
        logger.info("Serial disconnected")

    def send_effect(self, effect_type: str, freq_hz: float, magnitude: int, duration_ms: int,
                    max_torque_nm: float = 25.0,
                    settle_periods: int = 2,
                    num_periods: int = 5,
                    ramp_periods: int = 1) -> Optional[int]:
        """Send an effect command over serial. Returns timestamp T1 or None on failure."""
        if not self.is_connected:
            logger.warning("Serial not connected, cannot send effect")
            return None

        packet = _pack_command(effect_type, freq_hz, magnitude, duration_ms,
                               max_torque_nm, settle_periods, num_periods, ramp_periods)
        if packet is None:
            return None

        t1 = struct.unpack("<Q", packet[2:10])[0]

        with self._lock:
            try:
                self._port.write(packet)
                logger.info(f"Serial sent: type={effect_type} freq={freq_hz} mag={magnitude} dur={duration_ms} "
                            f"maxTorque={max_torque_nm:.1f}Nm settle={settle_periods} numPeriods={num_periods} "
                            f"ramp={ramp_periods} T1={t1}")
                return t1
            except Exception as e:
                logger.error(f"Serial write failed: {e}")
                return None

    def send_stop(self) -> bool:
        """Send stop command over serial."""
        if not self.is_connected:
            return False
        packet = struct.pack("<BB", START_BYTE, CMD_STOP)
        checksum = START_BYTE ^ CMD_STOP
        packet += struct.pack("<B", checksum)
        with self._lock:
            try:
                self._port.write(packet)
                return True
            except Exception:
                return False

    def _read_loop(self):
        """Background thread: read bytes from serial port."""
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
                    logger.error(f"Serial read error: {e}")
                    time.sleep(0.1)

    def _process_buffer(self):
        """Extract and process complete packets from the buffer."""
        while len(self._buffer) >= 19:
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

            if len(self._buffer) < 19:
                break

            packet = bytes(self._buffer[:19])
            parsed = _parse_response(packet)
            if parsed and self._on_response:
                try:
                    self._on_response(parsed)
                except Exception as e:
                    logger.error(f"Response callback error: {e}")

            self._buffer = self._buffer[19:]


# Global singleton instance
serial_manager = SerialManager()