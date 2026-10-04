"""One ESC over bidirectional DShot600 (AM32 in 3D mode).

    esc = Esc(gpio=18)
    esc.throttle = 0.2   # -1..1, 0 = stop
    esc.rpm              # from telemetry, None if no valid reply
    esc.close()
"""
import ctypes
import logging
import os
import threading
import time

log = logging.getLogger("dshot")

try:
    _lib = ctypes.CDLL(os.path.join(os.path.dirname(os.path.abspath(__file__)), "libdshot.so"))
    _lib.dshot_open.argtypes = [ctypes.c_uint]
    _lib.dshot_send.argtypes = [ctypes.c_int, ctypes.c_uint16]
    _lib.dshot_send.restype = ctypes.c_uint32
    _lib.dshot_close.argtypes = [ctypes.c_int]
except OSError:
    _lib = None
SIMULATED = _lib is None  # no libdshot.so (not built, or not a Pi): nothing is sent
_lock = threading.Lock()  # one piolib call at a time

# 5-bit GCR code -> 4-bit nibble (telemetry reply encoding)
GCR = {0x19: 0x0, 0x1B: 0x1, 0x12: 0x2, 0x13: 0x3, 0x1D: 0x4, 0x15: 0x5, 0x16: 0x6, 0x17: 0x7,
       0x1A: 0x8, 0x09: 0x9, 0x0A: 0xA, 0x0B: 0xB, 0x1E: 0xC, 0x0D: 0xD, 0x0E: 0xE, 0x0F: 0xF}


def throttle_to_value(throttle):
    """-1..1 -> DShot 3D value: 0 = stop, 48..1047 = reverse, 1048..2047 = forward."""
    throttle = max(-1.0, min(1.0, throttle))
    if throttle > 0:
        return 1048 + round(throttle * 999)
    if throttle < 0:
        return 48 + round(-throttle * 999)
    return 0


def make_frame(value):
    """11-bit value -> 16-bit frame. Inverted CRC tells the ESC to reply with telemetry."""
    data = value << 1  # telemetry request bit = 0
    crc = ~(data ^ (data >> 4) ^ (data >> 8)) & 0xF
    return (data << 4) | crc


def decode_erpm(raw):
    """Raw 21-bit reply -> eRPM, or None if there was no reply or it was corrupt."""
    if not raw:
        return None
    gcr = raw ^ (raw >> 1)  # level changes -> GCR bits
    value = 0
    for shift in (15, 10, 5, 0):
        nibble = GCR.get((gcr >> shift) & 0x1F)
        if nibble is None:
            return None
        value = (value << 4) | nibble
    if ((value >> 12) ^ (value >> 8) ^ (value >> 4) ^ value) & 0xF != 0xF:
        return None
    value >>= 4  # eee mmmmmmmmm: period in us = m << e
    if value == 0xFFF:
        return 0  # motor stopped
    period_us = (value & 0x1FF) << (value >> 9)
    return 60_000_000 // period_us if period_us else None


class Esc:
    """One ESC on one GPIO. A background thread sends the current throttle ~1000 times/s."""

    def __init__(self, gpio, poles=14, reverse=False):
        self.gpio = gpio
        self.poles = poles
        self.reverse = reverse  # flip if the motor spins the wrong way
        self.throttle = 0.0
        self.rpm = None
        if not SIMULATED:
            self._sm = _lib.dshot_open(gpio)
            if self._sm < 0:
                raise RuntimeError(f"dshot_open(GPIO{gpio}) failed with {self._sm} "
                                   "(-1: /dev/pio0 missing/no access, -2: no PIO memory, -3: no free state machine)")
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("GPIO%d: started%s", gpio, " (SIMULATED, no signal)" if SIMULATED else "")

    def close(self):
        self._running = False
        self._thread.join()
        log.info("GPIO%d: stopped", self.gpio)

    def _loop(self):
        self._send_zero(0.5)  # AM32 arms after receiving 0 for a while
        last_value = 0
        while self._running:
            value = throttle_to_value(-self.throttle if self.reverse else self.throttle)
            if value != last_value:
                log.debug("GPIO%d: throttle %.2f -> value %d", self.gpio, self.throttle, value)
                last_value = value
            erpm = decode_erpm(self._send(make_frame(value)))
            self.rpm = None if erpm is None else erpm // (self.poles // 2)
            time.sleep(0.001)
        self._send_zero(0.1)
        if not SIMULATED:
            with _lock:
                _lib.dshot_close(self._sm)

    def _send_zero(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._send(make_frame(0))
            time.sleep(0.001)

    def _send(self, frame):
        if SIMULATED:
            return 0
        with _lock:
            return _lib.dshot_send(self._sm, frame)
