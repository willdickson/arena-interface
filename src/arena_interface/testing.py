"""A simulated G6 arena controller for tests, examples and development.

[`FakePort`][arena_interface.testing.FakePort] behaves like a pyserial port
connected to a controller. Behind it, a
[`FakeController`][arena_interface.testing.FakeController] parses the bytes
written to the port the way the firmware does and answers with the same
replies: status codes, messages, payload layouts and side effects (error
glyphs that stop the display, commands refused while the display runs, index
shifts after deleting a pattern, and so on). It keeps an in-memory SD card,
the telemetry ring and the I/O state.

```python
from arena_interface.testing import FakePort
from arena_interface.transport import Transport

with Transport(FakePort()) as link:
    ...
```

The controller can also inject faults: unplugging the device, going silent,
delaying replies, or emitting debug text. A controller outlives its ports, so
a test can unplug, then connect a new ``FakePort(controller)`` to simulate a
reconnect.

What is not emulated: panel pixels (frames are accepted, not rendered), the
precise wording of panel-programming messages, and refresh timing beyond a
simple frames-sent estimate.
"""

from __future__ import annotations

import io
import struct
import threading
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import serial

from ._crc import crc8_autosar, crc16_ccitt_false, crc32
from .protocol import (
    DURATION_TICK_S,
    FRAME_PREFIX_SIZE,
    ArenaState,
    Capability,
    ErrorCode,
    FirmwareFlag,
    GrayLevel,
    Opcode,
    panel_block_size,
    stream_frame_size,
)

__all__ = ["FakeController", "FakePort", "make_test_pattern"]

_HEADER_SIZE = 18
_FOOTER_SIZE = 32
_MAX_PATTERNS = 256
_ERROR_HOLD_S = 0.750
_TELEMETRY_RECORD_BUDGET = 178
_DEFAULT_FLAGS = (
    FirmwareFlag.TELEMETRY
    | FirmwareFlag.CRASH_REPORT
    | FirmwareFlag.FREE_RUNNING_REFRESH
    | FirmwareFlag.SD_FAST_PATH
    | FirmwareFlag.SD_DIAG
)
_DEFAULT_CAPS = (
    Capability.G6_MODE
    | Capability.V2_LOCAL_STORAGE
    | Capability.IO_EXT
    | Capability.HEALTH
)
#: Commands the firmware only implements in builds advertising these bits.
_GATES: dict[int, Capability | FirmwareFlag] = {
    Opcode.SET_AO_MODE: Capability.IO_EXT,
    Opcode.GET_ANALOG_IN: Capability.IO_EXT,
    Opcode.SET_DIO_ROLE: Capability.IO_EXT,
    Opcode.GET_DIO_ROLE: Capability.IO_EXT,
    Opcode.GET_HEALTH: Capability.HEALTH,
    Opcode.GET_FIRMWARE_VERSION: Capability.HEALTH,
    Opcode.SET_TELEMETRY: FirmwareFlag.TELEMETRY,
    Opcode.GET_TELEMETRY_BLOCK: FirmwareFlag.TELEMETRY,
    Opcode.GET_CRASHREPORT: FirmwareFlag.CRASH_REPORT,
    Opcode.GET_SD_INFO: FirmwareFlag.SD_FAST_PATH,
    Opcode.SET_SD_DIAG: FirmwareFlag.SD_DIAG,
}


# -- test pattern builder ----------------------------------------------------


def _panel_block(gray_level: GrayLevel, value: int, duty_cycle: int) -> bytes:
    """One panel block with every pixel at ``value`` and a valid parity bit."""
    if gray_level == GrayLevel.GS16:
        pixels = bytes([(value & 0x0F) * 0x11]) * 200
        opcode = 0x30
    else:
        pixels = (b"\xff" if value else b"\x00") * 50
        opcode = 0x10
    body = bytes([opcode]) + pixels + bytes([duty_cycle])
    ones = 1 + sum(bin(b).count("1") for b in body)  # version bits 0x01
    return bytes([0x01 | ((ones & 1) << 7)]) + body


def make_test_pattern(
    rows: int = 2,
    cols: int = 10,
    frame_count: int = 4,
    gray_level: GrayLevel = GrayLevel.GS16,
    *,
    duty_cycle: int = 0x80,
    arena_id: int = 0,
) -> bytes:
    """Build a valid ``.pat`` file whose panels light up uniformly.

    Frame *f*, panel *p* has every pixel at ``(f + p) % levels``. The pattern is
    meant for tests: it exercises the file format, not the pixel layout.

    Args:
        rows: Panel rows.
        cols: Panel columns.
        frame_count: Number of frames (at least 1).
        gray_level: GS2 or GS16.
        duty_cycle: Brightness byte of every panel block.
        arena_id: 6-bit arena id for the header.

    Returns:
        The file contents.
    """
    if frame_count < 1:
        raise ValueError("frame_count must be at least 1")
    gray_level = GrayLevel(gray_level)
    panels = rows * cols
    header = bytearray(_HEADER_SIZE)
    header[0:4] = b"G6PT"
    header[4] = 0x20 | ((arena_id >> 2) & 0x0F)
    header[5] = (arena_id & 0x03) << 6
    struct.pack_into("<H", header, 6, frame_count)
    header[8], header[9] = rows, cols
    header[10] = 2 if gray_level == GrayLevel.GS16 else 1
    header[11:17] = ((1 << panels) - 1).to_bytes(6, "little")
    header[17] = crc8_autosar(bytes(header[:17]))
    out = bytearray(header)
    levels = int(gray_level)
    for f in range(frame_count):
        frame = bytearray(b"FR" + struct.pack("<H", f))
        for p in range(panels):
            frame += _panel_block(gray_level, (f + p) % levels, duty_cycle)
        out += frame + struct.pack("<H", crc16_ccitt_false(bytes(frame)))
    return bytes(out)


# -- controller --------------------------------------------------------------


def _resp(opcode: int, status: int = 0, payload: bytes | str = b"") -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("ascii")
    if 3 + len(payload) >= 200:  # firmware drops oversized payloads
        payload = b""
    return bytes([2 + len(payload), status, opcode]) + payload


@dataclass
class _Upload:
    opcode: int
    pattern_id: int
    remaining: int
    data: bytearray = field(default_factory=bytearray)


@dataclass
class _OpenPattern:
    name: str
    frame_count: int
    block_size: int
    frame_size: int


class FakeController:
    """Simulated controller state and command handling.

    Args:
        rows: Panel rows of the arena this "firmware build" drives.
        cols: Panel columns.
        patterns: Initial SD-card patterns, ``{file name: contents}``. By
            default two small demo patterns matching the arena are created.
        sd_card: Whether an SD card is inserted.
        capabilities: GET_CONTROLLER_INFO capability bits. Commands gated by a
            missing bit behave as unknown commands, like older firmware.
        firmware_flags: GET_FIRMWARE_VERSION flags (gating as above).
        mac: Controller MAC address bytes.
        git_sha: Reported firmware commit.
        branch: Reported firmware branch.
        build_date: Reported build date.

    Attributes:
        analog_in: Millivolts reported on Analog In 1 and 2.
        analog_loopback: ``(gain, offset_mv)`` to simulate a cable from the
            analog output to Analog In 1 (``ain1 = gain * out + offset``), or
            None.
        dio_inputs: BNC levels seen on Digital IO ports in an input role.
        response_delay: Seconds before each reply becomes readable.
        muted: When True, commands are processed but no reply is sent.
        received: Every complete message received, in order (for assertions).
    """

    def __init__(
        self,
        *,
        rows: int = 2,
        cols: int = 10,
        patterns: Mapping[str, bytes] | None = None,
        sd_card: bool = True,
        capabilities: Capability = _DEFAULT_CAPS,
        firmware_flags: FirmwareFlag = _DEFAULT_FLAGS,
        mac: bytes = bytes.fromhex("04e9e5000001"),
        git_sha: str = "fake0001",
        branch: str = "simulated",
        build_date: str = "2026-01-01",
    ) -> None:
        """Create a controller in its power-on state."""
        self.rows, self.cols = rows, cols
        self.capabilities = Capability(capabilities)
        self.firmware_flags = FirmwareFlag(firmware_flags)
        self.mac = bytes(mac)
        self.git_sha, self.branch, self.build_date = git_sha, branch, build_date
        self.sd_card = sd_card
        if patterns is None:
            patterns = {
                "demo_gs16.pat": make_test_pattern(rows, cols, 16, GrayLevel.GS16),
                "demo_gs2.pat": make_test_pattern(rows, cols, 2, GrayLevel.GS2),
            }
        self.files: dict[str, bytes] = dict(patterns)
        self.pattern_temp: bytes | None = None
        self.firmware_image: bytes | None = None
        self.analog_in: tuple[int, int] = (0, 0)
        self.analog_loopback: tuple[float, float] | None = None
        self.dio_inputs: list[bool] = [False, False]
        self.response_delay = 0.0
        self.muted = False
        self.received: list[bytes] = []
        self.lock = threading.RLock()
        self._ports: list[FakePort] = []
        self._telemetry: list[bytes] = []
        self._boot_count = 0
        self._boot()

    # -- public helpers ---------------------------------------------------

    @property
    def panel_count(self) -> int:
        """Panels in the arena."""
        return self.rows * self.cols

    @property
    def state(self) -> ArenaState:
        """Current controller state (time-based transitions applied)."""
        with self.lock:
            self._tick()
            return self._state

    @property
    def pattern_names(self) -> list[str]:
        """Pattern file names in index order (index = position + 1)."""
        return sorted(n for n in self.files if n.lower().endswith(".pat"))

    def unplug(self) -> None:
        """Disconnect every open port, as if the USB cable were pulled."""
        with self.lock:
            for port in self._ports:
                port._dead = True
            self._ports.clear()

    def inject(self, data: bytes) -> None:
        """Send unsolicited bytes to every connected port (e.g. line noise)."""
        with self.lock:
            for port in self._ports:
                port._push(bytes(data), 0.0)

    # -- lifecycle --------------------------------------------------------

    def _boot(self) -> None:
        now = time.monotonic()
        self._boot_time = now
        self._state = ArenaState.ALL_OFF
        self._open: _OpenPattern | None = None
        self._pattern_id = 0
        self._cur_frame = 0
        self._frame_rate = 0
        self._trial_start = now
        self._trial_start_frame = 0
        self._trial_end: float | None = None
        self._trial_duty = 0
        self._error_until = 0.0
        self._refresh_hz = self._default_refresh(GrayLevel.GS16)
        self._refresh_explicit = False
        self._panel_mode = 1
        self._frames_sent = 0.0
        self._frames_clock = now
        self._spi_mhz = 25
        self._diag_on = False
        self._ao_mv = 0
        self._ao_mode = 0
        self._ao_lut: list[int] = []
        self._dio_role = [2, 1]  # port 1 out_programmable (low), port 2 in_trigger
        self._dio_out = [False, False]
        self._legacy_seek = False
        self._no_skip = False
        self._cmd70 = 0
        self._tel_last_frame: tuple[int, int] | None = None
        self._sd_reads = 0
        self._telemetry_events = True
        self._synthetic = False
        self._synthetic_rate = 0
        self._synthetic_clock = now
        self._synthetic_counter = 0
        self._next_seq = (
            struct.unpack_from("<I", self._telemetry[-1], 2)[0] + 1
            if self._telemetry
            else 1
        )
        self._dropped = 0
        self._parse_buf = bytearray()
        self._upload: _Upload | None = None
        self._record_state(1, 0, 0)  # STATE(boot)

    def _default_refresh(self, gray: GrayLevel) -> int:
        small = (self.rows, self.cols) == (2, 10)
        if gray == GrayLevel.GS2:
            return 1000 if small else 1200
        return 300 if small else 400

    def _micros(self) -> int:
        return int((time.monotonic() - self._boot_time) * 1e6) & 0xFFFFFFFF

    # -- time-based behaviour ---------------------------------------------

    def _tick(self) -> None:
        now = time.monotonic()
        if self._state != ArenaState.ALL_OFF:
            self._frames_sent += (now - self._frames_clock) * self._refresh_hz
        self._frames_clock = now
        if self._state == ArenaState.ERROR_DISPLAY and now >= self._error_until:
            self._enter_all_off()
        if self._trial_end is not None and now >= self._trial_end:
            self._trial_end = None
            self._enter_all_off()
        if (
            self._state == ArenaState.OPEN_LOOP
            and self._frame_rate
            and self._open
            and self._open.frame_count > 1
        ):
            steps = int((now - self._trial_start) * abs(self._frame_rate))
            sign = 1 if self._frame_rate > 0 else -1
            new = (self._trial_start_frame + sign * steps) % self._open.frame_count
            if new != self._cur_frame:
                self._load_frame(new)
        if self._synthetic and self._synthetic_rate:
            n = int((now - self._synthetic_clock) * self._synthetic_rate)
            for _ in range(min(n, 1000)):
                self._synthetic_counter += 1
                self._record(
                    1,
                    struct.pack("<BBB", 0xFE, 0, 4)
                    + struct.pack("<I", self._synthetic_counter),
                )
            if n:
                self._synthetic_clock = now

    # -- telemetry ring ---------------------------------------------------

    def _record(self, rtype: int, body: bytes, t_us: int | None = None) -> None:
        if not self._telemetry_events:
            return
        stamp = self._micros() if t_us is None else t_us
        rec = struct.pack("<BBII", 10 + len(body), rtype, self._next_seq, stamp) + body
        self._next_seq += 1
        self._telemetry.append(rec)
        if len(self._telemetry) > 4000:  # bounded like the 64 KiB ring
            self._telemetry.pop(0)
            self._dropped += 1

    def _record_state(self, kind: int, code: int, arg: int) -> None:
        self._record(3, struct.pack("<BBH", kind, code, arg & 0xFFFF))

    # -- state transitions ------------------------------------------------

    def _set_state(self, state: ArenaState, arg: int = 0) -> None:
        if state != self._state:
            self._state = state
            self._record_state(2, int(state), arg)

    def _enter_all_off(self) -> None:
        self._set_state(ArenaState.ALL_OFF, self._pattern_id)
        self._trial_duty = 0
        self._trial_end = None

    def _show_error(self, code: int) -> None:
        self._state = ArenaState.ERROR_DISPLAY
        self._error_until = time.monotonic() + _ERROR_HOLD_S
        self._trial_end = None
        self._record_state(3, code, 0)

    def _validate_header(self, data: bytes, *, for_arena: bool) -> int:
        if len(data) < _HEADER_SIZE:
            return ErrorCode.SD_FILE_ERROR
        hdr = data[:_HEADER_SIZE]
        if (
            hdr[:4] != b"G6PT"
            or (hdr[4] >> 4) != 2
            or crc8_autosar(hdr[:17]) != hdr[17]
        ):
            return ErrorCode.HEADER_CRC
        if struct.unpack_from("<H", hdr, 6)[0] == 0 or hdr[10] not in (1, 2):
            return ErrorCode.HEADER_CRC
        if for_arena:
            panels = hdr[8] * hdr[9]
            mask_bits = bin(int.from_bytes(hdr[11:17], "little")).count("1")
            if not 0 < panels <= 48 or mask_bits > panels:
                return ErrorCode.ARENA_MISMATCH
            if (hdr[8], hdr[9]) != (self.rows, self.cols):
                return ErrorCode.ARENA_MISMATCH
        return 0

    def _open_pattern(self, pattern_id: int) -> int:
        if not self.sd_card:
            return ErrorCode.SD_NOT_PRESENT
        names = self.pattern_names
        if not 1 <= pattern_id <= len(names):
            return ErrorCode.BAD_PARAM
        name = names[pattern_id - 1]
        data = self.files[name]
        err = self._validate_header(data, for_arena=True)
        if err:
            return err
        gray = GrayLevel.GS16 if data[10] == 2 else GrayLevel.GS2
        block = panel_block_size(gray)
        self._open = _OpenPattern(
            name=name,
            frame_count=struct.unpack_from("<H", data, 6)[0],
            block_size=block,
            frame_size=FRAME_PREFIX_SIZE + self.panel_count * block + 2,
        )
        self._refresh_hz = (
            self._refresh_hz if self._refresh_explicit else self._default_refresh(gray)
        )
        return 0

    def _load_frame(self, index: int) -> bool:
        assert self._open is not None
        self._sd_reads += 1
        data = self.files.get(self._open.name, b"")
        start = _HEADER_SIZE + index * self._open.frame_size
        frame = data[start : start + self._open.frame_size]
        err = 0
        if len(frame) < self._open.frame_size:
            err = ErrorCode.SD_FILE_ERROR
        elif (
            frame[:2] != b"FR"
            or crc16_ccitt_false(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]
        ):
            err = ErrorCode.FRAME_CRC
        if err:
            self._show_error(err)
            return False
        self._cur_frame = index
        if self._ao_mode == 1:
            n = self._open.frame_count
            self._ao_mv = index * 5000 // (n - 1) if n > 1 else 0
        elif self._ao_lut:
            self._ao_mv = self._ao_lut[index % len(self._ao_lut)]
        # Like the firmware, log a FRAME only when the displayed frame changes.
        if (index, self._pattern_id) != self._tel_last_frame:
            self._tel_last_frame = (index, self._pattern_id)
            self._record(
                2,
                struct.pack(
                    "<HHIHIBB", index, self._pattern_id, 1500, 900, 2000, 0, 0x03
                ),
            )
        return True

    def _enter_pattern_mode(
        self, state: ArenaState, pattern_id: int, rate: int, init: int, ticks: int
    ) -> bool:
        err = self._open_pattern(pattern_id)
        self._record_state(7, err, pattern_id)
        if err:
            self._show_error(err)
            return False
        assert self._open is not None
        self._pattern_id = pattern_id
        self._frame_rate = rate
        self._tel_last_frame = None  # a new trial logs its first frame
        if not self._load_frame(init % self._open.frame_count):
            return False
        now = time.monotonic()
        self._trial_start = now
        self._trial_start_frame = self._cur_frame
        self._trial_end = now + ticks * DURATION_TICK_S if ticks else None
        self._set_state(state, pattern_id)
        return True

    # -- byte stream parsing (SerialManager::parseIncoming) ----------------

    def feed(self, data: bytes) -> bytes:
        """Process bytes from the host and return the bytes sent back."""
        with self.lock:
            self._tick()
            self._parse_buf += data
            out = bytearray()
            while True:
                if self._upload is not None:
                    take = min(self._upload.remaining, len(self._parse_buf))
                    self._upload.data += self._parse_buf[:take]
                    del self._parse_buf[:take]
                    self._upload.remaining -= take
                    if self._upload.remaining:
                        break
                    upload, self._upload = self._upload, None
                    out += self._finish_upload(upload)
                    continue
                message = self._next_message()
                if message is None:
                    break
                out += message
            return bytes(out)

    def _next_message(self) -> bytes | None:
        buf = self._parse_buf
        if not buf:
            return None
        first = buf[0]
        if first == Opcode.STREAM_FRAME:
            if len(buf) < 3:
                return None
            length = struct.unpack_from("<H", buf, 1)[0]
            if (
                3 + length
                > 3 + stream_frame_size(self.panel_count, GrayLevel.GS16) + 16
            ):
                buf.clear()
                return b""
            if len(buf) < 3 + length:
                return None
            payload = bytes(buf[3 : 3 + length])
            self.received.append(bytes(buf[: 3 + length]))
            del buf[: 3 + length]
            return self._reply(self._stream_frame(payload))
        if first <= 0x31:
            total = 1 + first
            if len(buf) < total:
                return None
            msg = bytes(buf[:total])
            del buf[:total]
            self.received.append(msg)
            if first == 0:
                return b""
            return self._reply(self._binary(msg[1], msg[2:]))
        if first == Opcode.SET_PATTERN_FILENAME:
            if len(buf) < 4:
                return None
            name_len = buf[3]
            if name_len == 0 or name_len >= 64:
                buf.clear()
                return b""
            total = 4 + name_len
            if len(buf) < total:
                return None
            msg = bytes(buf[:total])
            del buf[:total]
            self.received.append(msg)
            return self._reply(self._binary(msg[0], msg[1:]))
        if first in (Opcode.SET_PATTERN_FILE, Opcode.SET_FIRMWARE_FILE):
            header = 11 if first == Opcode.SET_PATTERN_FILE else 9
            if len(buf) < header:
                return None
            if first == Opcode.SET_PATTERN_FILE:
                pattern_id = struct.unpack_from("<H", buf, 1)[0]
                size = struct.unpack_from("<I", buf, 3)[0]
            else:
                pattern_id, size = 0, struct.unpack_from("<I", buf, 1)[0]
            self.received.append(bytes(buf[:header]))
            del buf[:header]
            if first == Opcode.SET_PATTERN_FILE and size == 0:
                return self._reply(_resp(first, 1, "Empty upload not supported"))
            self._upload = _Upload(first, pattern_id, size)
            return b""
        buf.clear()  # unknown leading byte: resync
        return b""

    def _reply(self, response: bytes) -> bytes:
        if self.muted:
            return b""
        out = bytearray()
        if FirmwareFlag.DEBUG_BUILD in self.firmware_flags and self._diag_on:
            ms = int((time.monotonic() - self._boot_time) * 1000)
            out += b"\xff" + f"[{ms}] [cmd] reply status={response[1]}\n".encode()
        return bytes(out + response)

    # -- handlers ---------------------------------------------------------

    def _stream_frame(self, payload: bytes) -> bytes:
        sizes = {
            stream_frame_size(self.panel_count, GrayLevel.GS2): GrayLevel.GS2,
            stream_frame_size(self.panel_count, GrayLevel.GS16): GrayLevel.GS16,
        }
        gray = sizes.get(len(payload))
        if gray is None:
            self._enter_all_off()
            return _resp(Opcode.STREAM_FRAME, 1, "Bad stream-frame size")
        if not self._refresh_explicit:
            self._refresh_hz = self._default_refresh(gray)
        self._set_state(ArenaState.STREAMING_FRAME)
        return _resp(Opcode.STREAM_FRAME)

    def _binary(self, op: int, p: bytes) -> bytes:
        # Like the firmware, the CMD record carries the receipt time but is
        # appended after the handler, so records it causes (state changes,
        # frames) precede it in the log with later timestamps.
        t_rx = self._micros()
        if not self._implements(op):
            op = -1  # treated as unknown by this "build"
        handler = _HANDLERS.get(op)
        if handler is None:
            if op == 0x06:
                return _resp(
                    op,
                    1,
                    "SWITCH_GRAYSCALE dropped for G6; mode inferred from stream size",
                )
            echo = op if op >= 0 else self.received[-1][1]
            self._show_error(ErrorCode.UNKNOWN_CMD)
            response = _resp(echo, 1, "Unknown command")
        else:
            response = handler(self, p)
        if op != Opcode.GET_TELEMETRY_BLOCK:
            echo_op = response[2]
            plen = min(len(p), 8)
            self._record(
                1, struct.pack("<BBB", echo_op, response[1], plen) + p[:plen], t_rx
            )
        return response

    def _implements(self, op: int) -> bool:
        gate = _GATES.get(op)
        if isinstance(gate, Capability):
            return gate in self.capabilities
        if isinstance(gate, FirmwareFlag):
            return gate in self.firmware_flags
        return True

    def _display_running(self) -> bool:
        return self._state != ArenaState.ALL_OFF

    # Display control

    def _all_off(self, p: bytes) -> bytes:
        self._enter_all_off()
        return _resp(Opcode.ALL_OFF, 0, "All-Off Received")

    def _all_on(self, p: bytes) -> bytes:
        if not self._refresh_explicit:
            self._refresh_hz = self._default_refresh(GrayLevel.GS16)
        self._set_state(ArenaState.ALL_ON)
        return _resp(Opcode.ALL_ON, 0, "All-On Received")

    def _stop(self, p: bytes) -> bytes:
        self._enter_all_off()
        return _resp(Opcode.STOP_DISPLAY, 0, "Display has been stopped")

    def _system_reset(self, p: bytes) -> bytes:
        response = _resp(Opcode.SYSTEM_RESET, 0, "rebooting")
        for port in self._ports:
            port._dead_after_drain = True
        self._ports.clear()
        self._boot_count += 1
        self._boot()
        return response

    def _trial_params(self, p: bytes) -> bytes:
        op = Opcode.TRIAL_PARAMS
        if len(p) < 11:
            self._show_error(ErrorCode.BAD_PAYLOAD_LEN)
            return _resp(op, 1, "TRIAL_PARAMS too short")
        mode = p[0]
        pattern_id, rate, init, _gain, ticks = struct.unpack_from("<HhHhH", p, 1)
        states = {
            2: ArenaState.OPEN_LOOP,
            3: ArenaState.SHOW_FRAME,
            4: ArenaState.CLOSED_LOOP,
        }
        if mode not in states:
            self._show_error(ErrorCode.BAD_PARAM)
            return _resp(op, 1, "TRIAL_PARAMS: mode must be 2/3/4")
        self._trial_duty = p[11] if len(p) >= 12 else 0
        if self._enter_pattern_mode(states[mode], pattern_id, rate, init, ticks):
            return _resp(op)
        self._trial_duty = 0
        return _resp(op, 1, "TRIAL_PARAMS: load failed")

    def _set_pattern_id(self, p: bytes) -> bytes:
        op = Opcode.SET_PATTERN_ID
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 03 id_lo id_hi]")
        pattern_id = struct.unpack("<H", p)[0]
        self._trial_duty = 0
        if not self._enter_pattern_mode(ArenaState.SHOW_FRAME, pattern_id, 0, 0, 0):
            return _resp(op, 1, "SET_PATTERN_ID: load failed")
        return _resp(op, 0, p)

    def _set_frame_position(self, p: bytes) -> bytes:
        op = Opcode.SET_FRAME_POSITION
        self._cmd70 += 1
        if len(p) < 2:
            self._show_error(ErrorCode.BAD_PAYLOAD_LEN)
            return _resp(op, 1, "SET_FRAME_POSITION too short")
        index = struct.unpack_from("<H", p)[0]
        if self._open is None:
            self._show_error(ErrorCode.BAD_PARAM)
            return _resp(
                op,
                1,
                "SET_FRAME_POSITION: no pattern selected (send trial-params first)",
            )
        if index >= self._open.frame_count:
            self._show_error(ErrorCode.BAD_PARAM)
            return _resp(op, 1, "SET_FRAME_POSITION: index out of range")
        if not self._load_frame(index):
            return _resp(op, 1, "SET_FRAME_POSITION: frame read failed")
        self._set_state(ArenaState.SHOW_FRAME, self._pattern_id)
        return _resp(op)

    def _get_frame_position(self, p: bytes) -> bytes:
        count = self._open.frame_count if self._open else 0
        return _resp(
            Opcode.GET_FRAME_POSITION, 0, struct.pack("<HH", self._cur_frame, count)
        )

    def _psram_index(self, p: bytes) -> bytes:
        if len(p) < 2:
            self._show_error(ErrorCode.BAD_PAYLOAD_LEN)
            return _resp(Opcode.DISPLAY_PSRAM_INDEX, 1, "DISPLAY_PSRAM too short")
        self._set_state(ArenaState.PSRAM_PLAY)
        return _resp(Opcode.DISPLAY_PSRAM_INDEX)

    def _psram_play(self, p: bytes) -> bytes:
        if len(p) < 6:
            self._show_error(ErrorCode.BAD_PAYLOAD_LEN)
            return _resp(Opcode.PSRAM_PLAY, 1, "PSRAM_PLAY too short")
        self._set_state(ArenaState.PSRAM_PLAY)
        return _resp(Opcode.PSRAM_PLAY)

    def _set_refresh(self, p: bytes) -> bytes:
        if len(p) != 2:
            return _resp(Opcode.SET_REFRESH_RATE, 1, "Expected [03 16 hz_lo hz_hi]")
        hz = struct.unpack("<H", p)[0]
        if hz > 0:
            self._refresh_hz, self._refresh_explicit = hz, True
        return _resp(Opcode.SET_REFRESH_RATE)

    def _get_refresh(self, p: bytes) -> bytes:
        return _resp(
            Opcode.GET_REFRESH_RATE, 0, struct.pack("<H", self._refresh_hz & 0xFFFF)
        )

    def _set_panel_mode(self, p: bytes) -> bytes:
        op = Opcode.SET_PANEL_DISPLAY_MODE
        if len(p) != 1:
            return _resp(op, 1, "Expected [02 1B mode]")
        if p[0] > 3:
            return _resp(op, 1, "mode must be 0-3 (oneshot/persist/triggered/gated)")
        self._panel_mode = p[0]
        return _resp(op, 0, p)

    def _get_panel_mode(self, p: bytes) -> bytes:
        return _resp(Opcode.GET_PANEL_DISPLAY_MODE, 0, bytes([self._panel_mode]))

    def _frames_sent_get(self, p: bytes) -> bytes:
        return _resp(
            Opcode.GET_FRAMES_SENT,
            0,
            struct.pack("<I", int(self._frames_sent) & 0xFFFFFFFF),
        )

    def _frames_sent_reset(self, p: bytes) -> bytes:
        self._frames_sent = 0.0
        return _resp(Opcode.RESET_FRAMES_SENT)

    # SD card

    def _file_count(self, p: bytes) -> bytes:
        count = len(self.pattern_names) if self.sd_card else 0
        return _resp(Opcode.GET_FILE_COUNT, 0, struct.pack("<H", count))

    def _pattern_filename(self, p: bytes) -> bytes:
        op = Opcode.GET_PATTERN_FILENAME
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 82 idx_lo idx_hi]")
        idx = struct.unpack("<H", p)[0]
        names = self.pattern_names if self.sd_card else []
        if not 1 <= idx <= len(names):
            return _resp(op, 1, "Index out of range")
        raw = names[idx - 1].encode()
        return _resp(op, 0, bytes([len(raw)]) + raw)

    def _pattern_info(self, p: bytes) -> bytes:
        op = Opcode.GET_PATTERN_INFO
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 88 idx_lo idx_hi]")
        idx = struct.unpack("<H", p)[0]
        if not self.sd_card:
            return _resp(op, ErrorCode.SD_NOT_PRESENT, "pattern info read failed")
        names = self.pattern_names
        if not 1 <= idx <= len(names):
            return _resp(op, ErrorCode.BAD_PARAM, "pattern info read failed")
        data = self.files[names[idx - 1]]
        err = self._validate_header(data, for_arena=False)
        gray = GrayLevel.GS16 if len(data) > 10 and data[10] == 2 else GrayLevel.GS2
        duty_offset = _HEADER_SIZE + FRAME_PREFIX_SIZE + panel_block_size(gray) - 1
        if not err and len(data) <= duty_offset:
            err = ErrorCode.SD_FILE_ERROR
        if err:
            return _resp(op, err, "pattern info read failed")
        arena_id = ((data[4] & 0x0F) << 2) | (data[5] >> 6)
        payload = struct.pack(
            "<HBBBBBIB",
            struct.unpack_from("<H", data, 6)[0],
            data[10],
            data[8],
            data[9],
            arena_id,
            data[5] & 0x3F,
            len(data),
            data[duty_offset],
        )
        return _resp(op, 0, payload)

    def _rename(self, p: bytes) -> bytes:
        op = Opcode.SET_PATTERN_FILENAME
        if len(p) < 3:
            return _resp(op, 1, "Too short")
        idx, name_len = struct.unpack_from("<HB", p)
        if name_len == 0 or name_len >= 64 or len(p) < 3 + name_len:
            return _resp(op, 1, "Bad name length")
        names = self.pattern_names if self.sd_card else []
        if idx > len(names):
            return _resp(op, 1, "Index out of range")
        new_name = p[3 : 3 + name_len].decode("ascii", errors="replace")
        if not self.sd_card:
            return _resp(op, ErrorCode.SD_NOT_PRESENT, "Rename failed")
        if idx == 0:
            if self.pattern_temp is None:
                return _resp(op, ErrorCode.SD_FILE_ERROR, "Rename failed")
            data, self.pattern_temp = self.pattern_temp, None
        else:
            data = self.files.pop(names[idx - 1])
            if self._open and self._open.name == names[idx - 1]:
                self._open = None
        candidate, n = new_name, 1
        while candidate in self.files and n <= 999:
            candidate, n = f"X{n:03d}_{new_name}", n + 1
        self.files[candidate] = data
        new_names = self.pattern_names
        new_idx = new_names.index(candidate) + 1 if candidate in new_names else 0
        return _resp(op, 0, struct.pack("<H", new_idx))

    def _get_pattern_file(self, p: bytes) -> bytes:
        op = Opcode.GET_PATTERN_FILE
        if self._display_running():
            return _resp(op, ErrorCode.DISPLAY_ACTIVE, "Stop display first")
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 84 idx_lo idx_hi]")
        idx = struct.unpack("<H", p)[0]
        names = self.pattern_names if self.sd_card else []
        if not 1 <= idx <= len(names):
            return _resp(op, 1, "Index out of range")
        data = self.files[names[idx - 1]]
        return _resp(op, 0, struct.pack("<Q", len(data))) + data

    def _finish_upload(self, upload: _Upload) -> bytes:
        op = upload.opcode
        data = bytes(upload.data)
        if self._display_running():
            return self._reply(
                _resp(op, ErrorCode.DISPLAY_ACTIVE, "Stop display before writing to SD")
            )
        if op == Opcode.SET_FIRMWARE_FILE:
            if not self.sd_card:
                return self._reply(_resp(op, 1, "SD open failed"))
            self.firmware_image = data
            return self._reply(_resp(op, 0, struct.pack("<I", crc32(data))))
        names = self.pattern_names if self.sd_card else []
        if upload.pattern_id > len(names):
            return self._reply(_resp(op, 1, "Index out of range"))
        if not self.sd_card:
            return self._reply(_resp(op, 1, "SD open failed"))
        if upload.pattern_id == 0:
            self.pattern_temp = data
        else:
            name = names[upload.pattern_id - 1]
            self.files[name] = data
            if self._open and self._open.name == name:
                self._open = None
        return self._reply(_resp(op))

    def _delete(self, p: bytes) -> bytes:
        op = Opcode.DELETE_PATTERN_FILE
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 86 idx_lo idx_hi]")
        idx = struct.unpack("<H", p)[0]
        if not self.sd_card:
            return _resp(op, ErrorCode.SD_NOT_PRESENT, "Delete failed")
        if idx == 0:
            if self.pattern_temp is None:
                return _resp(op, ErrorCode.BAD_PARAM, "Delete failed")
            self.pattern_temp = None
            return _resp(op)
        names = self.pattern_names
        if idx > len(names):
            return _resp(op, ErrorCode.BAD_PARAM, "Delete failed")
        del self.files[names[idx - 1]]
        if self._open and self._open.name == names[idx - 1]:
            self._open = None
        return _resp(op)

    def _purge(self, p: bytes) -> bytes:
        op = Opcode.PURGE_MEMORY
        if self._display_running():
            return _resp(op, ErrorCode.DISPLAY_ACTIVE, "Stop display first")
        if not self.sd_card:
            return _resp(op, ErrorCode.SD_NOT_PRESENT, "Purge-memory failed")
        self.files.clear()
        self.pattern_temp = self.firmware_image = None
        self._open = None
        return _resp(op)

    def _archive(self, p: bytes) -> bytes:
        op = Opcode.GET_SD_ARCHIVE
        if self._display_running():
            return _resp(op, ErrorCode.DISPLAY_ACTIVE, "Stop display first")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            if self.sd_card:
                for name in self.pattern_names[:_MAX_PATTERNS]:
                    zf.writestr(f"patterns/{name}", self.files[name])
        data = buf.getvalue()
        return _resp(op, 0, struct.pack("<Q", len(data))) + data

    # Analog / digital I/O

    def _set_ao(self, p: bytes) -> bytes:
        op = Opcode.SET_AO_VOLTAGE
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 A0 mv_lo mv_hi]")
        mv = struct.unpack("<H", p)[0]
        if mv > 5000:
            return _resp(op, 1, "mv out of range (0-5000)")
        if self._ao_mode != 0:
            return _resp(
                op, 1, "AO is in frame_number mode - SET_AO_MODE (0xA3) 0 first"
            )
        self._ao_mv, self._ao_lut = mv, []
        return _resp(op, 0, p)

    def _get_ao(self, p: bytes) -> bytes:
        code = self._ao_mv * 4095 // 5000
        return _resp(Opcode.GET_AO_VOLTAGE, 0, struct.pack("<H", code * 5000 // 4095))

    def _set_ao_lut(self, p: bytes) -> bytes:
        op = Opcode.SET_AO_LUT
        if len(p) < 5:
            return _resp(op, 1, "Too short (need mode+step_hz+count)")
        if self._ao_mode != 0:
            return _resp(
                op, 1, "AO is in frame_number mode - SET_AO_MODE (0xA3) 0 first"
            )
        mode, _step, count = struct.unpack_from("<BHH", p)
        if mode > 1:
            return _resp(op, 1, "mode must be 0 (frame-locked) or 1 (time-based)")
        if not 1 <= count <= 4096:
            return _resp(op, 1, "count out of range (1-4096)")
        if 6 + 2 * count != len(p) + 1:
            return _resp(op, 1, "count/length mismatch")
        values = list(struct.unpack_from(f"<{count}H", p, 5))
        if any(v > 5000 for v in values):
            return _resp(op, 1, "mv out of range (0-5000)")
        self._ao_lut, self._ao_mv = values, values[0]
        return _resp(op, 0, struct.pack("<H", count))

    def _set_ao_mode(self, p: bytes) -> bytes:
        op = Opcode.SET_AO_MODE
        if len(p) != 1:
            return _resp(op, 1, "Expected [02 A3 mode]")
        if p[0] > 1:
            return _resp(op, 1, "mode must be 0 (programmable) or 1 (frame_number)")
        self._ao_mode = p[0]
        if self._ao_mode == 1:
            self._ao_lut = []
            if self._open:
                n = self._open.frame_count
                self._ao_mv = self._cur_frame * 5000 // (n - 1) if n > 1 else 0
        return _resp(op)

    def _analog_in(self, p: bytes) -> bytes:
        ain1, ain2 = self.analog_in
        if self.analog_loopback is not None:
            gain, offset = self.analog_loopback
            code = self._ao_mv * 4095 // 5000  # what the 12-bit DAC really outputs
            ain1 = max(-10000, min(10000, round(gain * code * 5000 / 4095 + offset)))
        return _resp(Opcode.GET_ANALOG_IN, 0, struct.pack("<hh", ain1, ain2))

    def _dio_level(self, port: int) -> bool:
        if self._dio_role[port] in (2, 3):
            return self._dio_out[port]
        return self.dio_inputs[port]

    def _set_dout(self, p: bytes) -> bytes:
        op = Opcode.SET_DIGITAL_OUT
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 AA channel state]")
        channel, state = p
        if channel not in (1, 2):
            return _resp(op, 1, "channel must be 1 or 2")
        i = channel - 1
        if self._dio_role[i] == 0:
            self._dio_role[i], self._dio_out[i] = 2, False
        if self._dio_role[i] != 2:
            names = ["off", "in_trigger", "out_programmable", "out_debug_framescan"]
            return _resp(
                op,
                1,
                f"Digital IO {channel} role is {names[self._dio_role[i]]} - "
                "SET_DIO_ROLE (0xAC) it to out_programmable first",
            )
        self._dio_out[i] = bool(state)
        return _resp(op)

    def _get_dout(self, p: bytes) -> bytes:
        return _resp(
            Opcode.GET_DIGITAL_OUT, 0, bytes([self._dio_level(0), self._dio_level(1)])
        )

    def _set_dio_role(self, p: bytes) -> bytes:
        op = Opcode.SET_DIO_ROLE
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 AC port role]")
        port, role = p
        if port not in (1, 2):
            return _resp(op, 1, "port must be 1 or 2")
        if role > 3:
            return _resp(
                op,
                1,
                "role must be 0=off 1=in_trigger 2=out_programmable "
                "3=out_debug_framescan",
            )
        self._dio_role[port - 1] = role
        self._dio_out[port - 1] = False
        return _resp(op)

    def _get_dio_role(self, p: bytes) -> bytes:
        payload = bytes(
            [
                self._dio_role[0],
                self._dio_level(0),
                self._dio_role[1],
                self._dio_level(1),
            ]
        )
        return _resp(Opcode.GET_DIO_ROLE, 0, payload)

    # Identity, health, diagnostics

    def _controller_info(self, p: bytes) -> bytes:
        return _resp(
            Opcode.GET_CONTROLLER_INFO, 0, bytes([1, self.capabilities]) + self.mac
        )

    def _firmware_version(self, p: bytes) -> bytes:
        payload = (
            bytes([1, self.rows, self.cols, self.firmware_flags])
            + self.git_sha.encode()[:8].ljust(8)
            + self.build_date.encode()[:10].ljust(10)
            + self.branch.encode()[:24].ljust(24)
        )
        return _resp(Opcode.GET_FIRMWARE_VERSION, 0, payload)

    def _health(self, p: bytes) -> bytes:
        uptime_ms = int((time.monotonic() - self._boot_time) * 1000)
        flags = (
            (0x01 if self.sd_card else 0)
            | (0x02 if self._open else 0)
            | (0x04 if self._display_running() else 0)
            | (0x08 if self._boot_count else 0)
        )
        payload = bytearray(114)
        struct.pack_into(
            "<BBIIIIIIBIIIIBHIBI",
            payload,
            0,
            5,
            flags,
            uptime_ms & 0xFFFFFFFF,
            uptime_ms * 50 & 0xFFFFFFFF,  # loop count
            850,  # loop max µs
            600,  # loop max in last second
            self._sd_reads,
            1800,  # SD read max µs
            0,
            0,
            int(self._frames_sent) & 0xFFFFFFFF,
            int(self._frames_sent) & 0xFFFFFFFF,
            self._cmd70,
            int(self._state),
            self._cur_frame,
            0,
            4 if self._boot_count else 0,
            0,
        )
        return _resp(Opcode.GET_HEALTH, 0, bytes(payload))

    def _crash_report(self, p: bytes) -> bytes:
        return _resp(Opcode.GET_CRASHREPORT, 0, bytes(128))

    def _set_telemetry(self, p: bytes) -> bytes:
        op = Opcode.SET_TELEMETRY
        if len(p) not in (1, 3):
            return _resp(
                op, 1, "Expected [04 A8 flags rate_lo rate_hi] or [02 A8 flags]"
            )
        flags = p[0]
        if len(p) == 3:
            self._synthetic_rate = struct.unpack_from("<H", p, 1)[0]
        self._telemetry_events = bool(flags & 0x01)
        self._synthetic = bool(flags & 0x80)
        self._synthetic_clock = time.monotonic()
        self._record_state(6, flags, self._synthetic_rate)
        return _resp(op)

    def _telemetry_block(self, p: bytes) -> bytes:
        op = Opcode.GET_TELEMETRY_BLOCK
        if len(p) < 6:
            return _resp(op, 1, "Expected [08 A9 ack_seq(4) max_bytes(2) flags]")
        ack, max_bytes = struct.unpack_from("<IH", p)
        if ack != 0xFFFFFFFF:
            self._telemetry = [
                r for r in self._telemetry if struct.unpack_from("<I", r, 2)[0] > ack
            ]
        budget = min(max_bytes, _TELEMETRY_RECORD_BUDGET)
        chosen: list[bytes] = []
        used = 0
        for rec in self._telemetry:
            if used + len(rec) > budget:
                break
            chosen.append(rec)
            used += len(rec)
        first = struct.unpack_from("<I", chosen[0], 2)[0] if chosen else 0
        flags = (0x01 if self._telemetry_events else 0) | (
            0x02 if self._boot_count else 0
        )
        flags |= 0x08 if self._synthetic else 0
        header = struct.pack(
            "<IIHIBBH",
            self._micros(),
            first,
            len(chosen),
            self._dropped,
            len(chosen) < len(self._telemetry),
            flags,
            min(self._boot_count, 0xFFFF),
        )
        return _resp(op, 0, header + b"".join(chosen))

    def _sd_info(self, p: bytes) -> bytes:
        if not self.sd_card:
            return _resp(Opcode.GET_SD_INFO, 1, bytes([1, 0, 0, 0]) + bytes(26))
        cid = (
            bytes([0x03, 0x53, 0x44])
            + b"FAKE0"
            + bytes([0x10, 0, 0, 0, 1, 0x01, 0x9A, 0])
        )
        diag = (0x01 if self._legacy_seek else 0) | (0x02 if self._no_skip else 0)
        payload = (
            bytes([1, 0x07, 3, 32])
            + struct.pack("<II", 62_333_952, 32_768)
            + cid
            + bytes([0xFF, diag])
        )
        return _resp(Opcode.GET_SD_INFO, 0, payload)

    def _set_sd_diag(self, p: bytes) -> bytes:
        op = Opcode.SET_SD_DIAG
        if len(p) != 1:
            return _resp(op, 1, "Expected [02 CE flags]")
        if p[0] & ~0x03:
            return _resp(op, 1, "SET_SD_DIAG: bits 2-7 reserved")
        self._legacy_seek, self._no_skip = bool(p[0] & 1), bool(p[0] & 2)
        return _resp(op, 0, p)

    def _set_diag(self, p: bytes) -> bytes:
        self._diag_on = p[0] != 0 if p else True
        return _resp(
            Opcode.SET_DIAG_OUTPUT, 0, "diag on" if self._diag_on else "diag off"
        )

    def _get_diag(self, p: bytes) -> bytes:
        return _resp(Opcode.GET_DIAG_OUTPUT, 0, bytes([self._diag_on]))

    def _set_spi(self, p: bytes) -> bytes:
        op = Opcode.SET_SPI_CLOCK
        if len(p) != 2:
            return _resp(op, 1, "Expected [03 C5 mhz_lo mhz_hi]")
        self._spi_mhz = min(30, max(1, struct.unpack("<H", p)[0]))
        return _resp(op, 0, struct.pack("<H", self._spi_mhz))

    def _get_spi(self, p: bytes) -> bytes:
        return _resp(Opcode.GET_SPI_CLOCK, 0, struct.pack("<H", self._spi_mhz))

    def _get_ip(self, p: bytes) -> bytes:
        return _resp(Opcode.GET_ETHERNET_IP_ADDRESS, 0, "")

    # Panel firmware

    def _firmware_info(self, p: bytes) -> bytes:
        op = Opcode.GET_FIRMWARE_INFO
        if self.firmware_image is None:
            return _resp(op, 1, "No firmware image present")
        if len(self.firmware_image) < _FOOTER_SIZE:
            return _resp(op, 1, "Firmware image too small")
        return _resp(op, 0, self.firmware_image[-_FOOTER_SIZE:])

    def _panel_isp(self, op: int, p: bytes, verb: str) -> bytes:
        if self._display_running():
            return _resp(
                op, ErrorCode.DISPLAY_ACTIVE, f"Stop display before {verb} a panel"
            )
        if not p:
            return _resp(op, 1, f"Expected [02 {op:02X} panel_number]")
        number = p[0]
        if number < 1:
            return _resp(op, 1, "panel numbers are 1-based (1..N)")
        if number > self.panel_count:
            return _resp(op, 1, f"panel {number}: no such panel (simulated)")
        if self.firmware_image is None:
            return _resp(op, 1, "no /firmware/panel.bin (simulated)")
        return _resp(op, 0, f"panel {number}: {verb} OK (simulated)")

    def _program(self, p: bytes) -> bytes:
        return self._panel_isp(Opcode.PROGRAM_PANEL, p, "flashing")

    def _verify(self, p: bytes) -> bytes:
        return self._panel_isp(Opcode.VERIFY_PANEL, p, "verifying")


_HANDLERS: dict[int, Callable[[FakeController, bytes], bytes]] = {
    Opcode.ALL_OFF: FakeController._all_off,
    Opcode.ALL_ON: FakeController._all_on,
    Opcode.STOP_DISPLAY: FakeController._stop,
    Opcode.SYSTEM_RESET: FakeController._system_reset,
    Opcode.TRIAL_PARAMS: FakeController._trial_params,
    Opcode.SET_PATTERN_ID: FakeController._set_pattern_id,
    Opcode.SET_FRAME_POSITION: FakeController._set_frame_position,
    Opcode.GET_FRAME_POSITION: FakeController._get_frame_position,
    Opcode.DISPLAY_PSRAM_INDEX: FakeController._psram_index,
    Opcode.PSRAM_PLAY: FakeController._psram_play,
    Opcode.SET_REFRESH_RATE: FakeController._set_refresh,
    Opcode.GET_REFRESH_RATE: FakeController._get_refresh,
    Opcode.SET_PANEL_DISPLAY_MODE: FakeController._set_panel_mode,
    Opcode.GET_PANEL_DISPLAY_MODE: FakeController._get_panel_mode,
    Opcode.GET_FRAMES_SENT: FakeController._frames_sent_get,
    Opcode.RESET_FRAMES_SENT: FakeController._frames_sent_reset,
    Opcode.GET_FILE_COUNT: FakeController._file_count,
    Opcode.GET_PATTERN_FILENAME: FakeController._pattern_filename,
    Opcode.GET_PATTERN_INFO: FakeController._pattern_info,
    Opcode.SET_PATTERN_FILENAME: FakeController._rename,
    Opcode.GET_PATTERN_FILE: FakeController._get_pattern_file,
    Opcode.DELETE_PATTERN_FILE: FakeController._delete,
    Opcode.PURGE_MEMORY: FakeController._purge,
    Opcode.GET_SD_ARCHIVE: FakeController._archive,
    Opcode.SET_AO_VOLTAGE: FakeController._set_ao,
    Opcode.GET_AO_VOLTAGE: FakeController._get_ao,
    Opcode.SET_AO_LUT: FakeController._set_ao_lut,
    Opcode.SET_AO_MODE: FakeController._set_ao_mode,
    Opcode.GET_ANALOG_IN: FakeController._analog_in,
    Opcode.SET_DIGITAL_OUT: FakeController._set_dout,
    Opcode.GET_DIGITAL_OUT: FakeController._get_dout,
    Opcode.SET_DIO_ROLE: FakeController._set_dio_role,
    Opcode.GET_DIO_ROLE: FakeController._get_dio_role,
    Opcode.GET_CONTROLLER_INFO: FakeController._controller_info,
    Opcode.GET_FIRMWARE_VERSION: FakeController._firmware_version,
    Opcode.GET_HEALTH: FakeController._health,
    Opcode.GET_CRASHREPORT: FakeController._crash_report,
    Opcode.SET_TELEMETRY: FakeController._set_telemetry,
    Opcode.GET_TELEMETRY_BLOCK: FakeController._telemetry_block,
    Opcode.GET_SD_INFO: FakeController._sd_info,
    Opcode.SET_SD_DIAG: FakeController._set_sd_diag,
    Opcode.SET_DIAG_OUTPUT: FakeController._set_diag,
    Opcode.GET_DIAG_OUTPUT: FakeController._get_diag,
    Opcode.SET_SPI_CLOCK: FakeController._set_spi,
    Opcode.GET_SPI_CLOCK: FakeController._get_spi,
    Opcode.GET_ETHERNET_IP_ADDRESS: FakeController._get_ip,
    Opcode.GET_FIRMWARE_INFO: FakeController._firmware_info,
    Opcode.PROGRAM_PANEL: FakeController._program,
    Opcode.VERIFY_PANEL: FakeController._verify,
}


# -- port --------------------------------------------------------------------


class FakePort:
    """A pyserial-compatible port connected to a simulated controller.

    Args:
        controller: Controller to connect to; a new default one if omitted.
        **controller_options: Passed to
            [`FakeController`][arena_interface.testing.FakeController] when no
            controller is given (e.g. ``rows=4, cols=12``).

    Example:
        >>> from arena_interface.transport import Transport
        >>> from arena_interface import messages
        >>> port = FakePort()
        >>> with Transport(port) as link:
        ...     reply = link.request(messages.get_file_count().frame, expect=0x80)
        >>> messages.decode_u16(reply.payload)
        2
    """

    def __init__(
        self, controller: FakeController | None = None, **controller_options: object
    ) -> None:
        """Connect a new port to ``controller``."""
        if controller is None:
            controller = FakeController(**controller_options)  # type: ignore[arg-type]
        elif controller_options:
            raise TypeError("pass controller options or a controller, not both")
        self.controller = controller
        self.timeout: float | None = 0.01
        self.port = "fake://arena"
        self.is_open = True
        self._rx: list[tuple[float, bytes]] = []
        self._cond = threading.Condition()
        self._dead = False
        self._dead_after_drain = False
        with controller.lock:
            controller._ports.append(self)

    def _check(self) -> None:
        if self._dead or not self.is_open:
            raise serial.SerialException("device disconnected (simulated)")
        if self._dead_after_drain and not self._rx:
            self._dead = True
            raise serial.SerialException("device rebooted (simulated)")

    def _push(self, data: bytes, delay: float) -> None:
        if data:
            with self._cond:
                self._rx.append((time.monotonic() + delay, data))
                self._cond.notify_all()

    def _ready_bytes(self) -> int:
        now = time.monotonic()
        return sum(len(d) for t, d in self._rx if t <= now)

    @property
    def in_waiting(self) -> int:
        """Bytes ready to read."""
        with self._cond:
            self._check()
            return self._ready_bytes()

    def read(self, size: int = 1) -> bytes:
        """Read up to ``size`` bytes, waiting up to ``timeout`` for the first."""
        deadline = time.monotonic() + (self.timeout or 0)
        with self._cond:
            while True:
                self._check()
                if self._ready_bytes():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                pending = [t for t, _ in self._rx]
                wait = min(
                    [remaining] + [max(0.0, t - time.monotonic()) for t in pending]
                )
                self._cond.wait(max(wait, 0.0005))
            out = bytearray()
            now = time.monotonic()
            while self._rx and self._rx[0][0] <= now and len(out) < size:
                ready_at, chunk = self._rx.pop(0)
                take = size - len(out)
                out += chunk[:take]
                if take < len(chunk):
                    self._rx.insert(0, (ready_at, chunk[take:]))
            return bytes(out)

    def write(self, data: bytes, /) -> int:
        """Send bytes to the simulated controller."""
        with self._cond:
            self._check()
        reply = self.controller.feed(bytes(data))
        self._push(reply, self.controller.response_delay)
        return len(data)

    def reset_input_buffer(self) -> None:
        """Discard unread bytes."""
        with self._cond:
            self._check()
            self._rx.clear()

    def close(self) -> None:
        """Close the port; the controller keeps its state."""
        self.is_open = False
        with self.controller.lock:
            if self in self.controller._ports:
                self.controller._ports.remove(self)
