"""Request builders and response decoders for every controller command.

Each command has a builder that validates its arguments and returns a
``Request``: the bytes to send, the opcode
the reply will echo, a suitable timeout, the kind of exchange, and the
capability or firmware flag the device must advertise. Replies are turned
into dataclasses by the ``decode_*`` functions.

Arguments are validated against the ranges the firmware accepts, because
several invalid commands make the firmware show an error glyph and stop the
display rather than just returning an error.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import Enum, IntEnum

from .errors import CommandError, ProtocolError
from .framing import Response, encode_binary, encode_stream
from .protocol import (
    DURATION_TICK_S,
    MAX_AO_LUT_ENTRIES,
    MAX_AO_MV,
    MAX_PATTERN_NAME,
    MAX_TELEMETRY_BYTES,
    AoLutMode,
    AoMode,
    ArenaState,
    Capability,
    DioRole,
    FirmwareFlag,
    GrayLevel,
    Opcode,
    PanelDisplayMode,
    TrialMode,
)

#: Default reply timeout (s) for ordinary commands.
DEFAULT_TIMEOUT = 1.0
#: Timeout (s) for commands that touch many SD files or wait for a transfer.
SD_TIMEOUT = 30.0
#: Timeout (s) for formatting the SD card.
PURGE_TIMEOUT = 180.0
#: Timeout (s) for programming or verifying one panel over SPI ISP.
PANEL_ISP_TIMEOUT = 60.0
#: Value of ``ack_seq`` meaning "acknowledge nothing".
TELEMETRY_NO_ACK = 0xFFFFFFFF


class Exchange(Enum):
    """How a request is carried out on the wire."""

    COMMAND = "command"
    """Send a frame, receive one response."""
    BULK_READ = "bulk_read"
    """Send a frame, receive a size header then raw bytes."""
    BULK_WRITE = "bulk_write"
    """Send a header and raw bytes, then receive one response."""


Requirement = Capability | FirmwareFlag


@dataclass(frozen=True)
class Request:
    """A command ready to send.

    Attributes:
        opcode: Opcode the reply echoes.
        frame: Bytes to write (for bulk writes: the header only).
        timeout: Seconds to wait for the reply.
        exchange: How the request is carried out.
        data: Raw payload sent after ``frame`` for bulk writes.
        requires: Capability or firmware flag the device must advertise.
    """

    opcode: Opcode
    frame: bytes
    timeout: float = DEFAULT_TIMEOUT
    exchange: Exchange = Exchange.COMMAND
    data: bytes = field(default=b"", repr=False)
    requires: Requirement | None = None


# -- validation helpers ------------------------------------------------------


def _int(value: int, name: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be {lo}..{hi}, got {value}")
    return value


def _u8(value: int, name: str, lo: int = 0, hi: int = 0xFF) -> bytes:
    return bytes([_int(value, name, lo, hi)])


def _u16(value: int, name: str, lo: int = 0, hi: int = 0xFFFF) -> bytes:
    return _int(value, name, lo, hi).to_bytes(2, "little")


def _i16(value: int, name: str) -> bytes:
    return _int(value, name, -0x8000, 0x7FFF).to_bytes(2, "little", signed=True)


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def _cmd(
    opcode: Opcode,
    params: bytes = b"",
    *,
    timeout: float = DEFAULT_TIMEOUT,
    requires: Requirement | None = None,
) -> Request:
    return Request(
        opcode, encode_binary(opcode, params), timeout=timeout, requires=requires
    )


def _enum(cls: type[IntEnum], value: int, name: str) -> IntEnum:
    try:
        return cls(value)
    except ValueError:
        choices = ", ".join(f"{m.name}={m.value}" for m in cls)
        raise ValueError(f"{name} must be one of {choices}; got {value!r}") from None


# -- display control ---------------------------------------------------------


def all_off() -> Request:
    """Blank the arena and stop any display (ALL_OFF)."""
    return _cmd(Opcode.ALL_OFF)


def all_on() -> Request:
    """Light every pixel at full brightness (ALL_ON)."""
    return _cmd(Opcode.ALL_ON)


def stop_display() -> Request:
    """Stop the display and blank the arena (STOP_DISPLAY)."""
    return _cmd(Opcode.STOP_DISPLAY)


def system_reset() -> Request:
    """Reboot the controller (SYSTEM_RESET); the USB link drops afterwards."""
    return _cmd(Opcode.SYSTEM_RESET)


def trial_params(
    mode: TrialMode | int,
    pattern_id: int,
    *,
    frame_rate: int = 0,
    initial_frame: int = 0,
    gain: int = 0,
    duration: float = 0.0,
    duty_cycle: int | None = None,
) -> Request:
    """Start a trial from an SD-card pattern (TRIAL_PARAMS).

    Args:
        mode: Open loop, show frame or closed loop.
        pattern_id: 1-based pattern index on the SD card.
        frame_rate: Frames per second in open loop; negative plays backwards.
        initial_frame: 0-based frame shown first.
        gain: Closed-loop gain, in tenths of frames per second per volt.
        duration: Seconds until the controller stops the trial by itself
            (0 = run until stopped). Resolution 10 ms, maximum 655.35 s.
        duty_cycle: Brightness 1-255 for every frame of this trial, or None
            to use the brightness stored in the pattern.

    Returns:
        The request.
    """
    mode = TrialMode(_enum(TrialMode, mode, "mode"))
    if duration < 0:
        raise ValueError(f"duration must be >= 0, got {duration}")
    ticks = round(duration / DURATION_TICK_S)
    params = (
        bytes([mode])
        + _u16(pattern_id, "pattern_id", 1)
        + _i16(frame_rate, "frame_rate")
        + _u16(initial_frame, "initial_frame")
        + _i16(gain, "gain")
        + _u16(ticks, "duration (in 10 ms ticks)")
    )
    if duty_cycle is not None:
        params += _u8(duty_cycle, "duty_cycle", 1)
    return _cmd(Opcode.TRIAL_PARAMS, params)


def set_pattern_id(pattern_id: int) -> Request:
    """Open a pattern in show-frame mode at frame 0 (SET_PATTERN_ID)."""
    return _cmd(Opcode.SET_PATTERN_ID, _u16(pattern_id, "pattern_id", 1))


def set_frame_position(index: int) -> Request:
    """Show a 0-based frame of the open pattern (SET_FRAME_POSITION)."""
    return _cmd(Opcode.SET_FRAME_POSITION, _u16(index, "index"))


def get_frame_position() -> Request:
    """Read the current frame index and frame count (GET_FRAME_POSITION)."""
    return _cmd(Opcode.GET_FRAME_POSITION)


def stream_frame(payload: bytes) -> Request:
    """Display a host-supplied frame payload (STREAM_FRAME).

    The payload size must match the arena; the caller checks it.
    """
    return Request(Opcode.STREAM_FRAME, encode_stream(payload))


def display_psram_index(index: int) -> Request:
    """Show a frame stored in the panels' own memory (DISPLAY_PSRAM_INDEX)."""
    return _cmd(Opcode.DISPLAY_PSRAM_INDEX, _u16(index, "index"))


def psram_play(start: int, count: int, fps: int) -> Request:
    """Play panel-stored frames ``start .. start+count-1`` at ``fps`` (PSRAM_PLAY)."""
    params = _u16(start, "start") + _u16(count, "count", 1) + _u16(fps, "fps")
    return _cmd(Opcode.PSRAM_PLAY, params)


def set_refresh_rate(hz: int) -> Request:
    """Set how often the current frame is re-sent to the panels (SET_REFRESH_RATE)."""
    return _cmd(Opcode.SET_REFRESH_RATE, _u16(hz, "hz", 1))


def get_refresh_rate() -> Request:
    """Read the refresh rate in Hz (GET_REFRESH_RATE)."""
    return _cmd(Opcode.GET_REFRESH_RATE)


def set_panel_display_mode(mode: PanelDisplayMode | int) -> Request:
    """Choose oneshot / persist / triggered / gated (SET_PANEL_DISPLAY_MODE)."""
    mode = _enum(PanelDisplayMode, mode, "mode")
    return _cmd(Opcode.SET_PANEL_DISPLAY_MODE, bytes([mode]))


def get_panel_display_mode() -> Request:
    """Read the panel display mode (GET_PANEL_DISPLAY_MODE)."""
    return _cmd(Opcode.GET_PANEL_DISPLAY_MODE)


def get_frames_sent() -> Request:
    """Read how many frames were pushed to the panels (GET_FRAMES_SENT)."""
    return _cmd(Opcode.GET_FRAMES_SENT)


def reset_frames_sent() -> Request:
    """Zero the frames-sent counter (RESET_FRAMES_SENT)."""
    return _cmd(Opcode.RESET_FRAMES_SENT)


# -- SD card / pattern library -----------------------------------------------


def get_file_count() -> Request:
    """Read the number of patterns on the SD card (GET_FILE_COUNT)."""
    return _cmd(Opcode.GET_FILE_COUNT)


def get_pattern_filename(pattern_id: int) -> Request:
    """Read the file name of a 1-based pattern (GET_PATTERN_FILENAME)."""
    return _cmd(Opcode.GET_PATTERN_FILENAME, _u16(pattern_id, "pattern_id", 1))


def get_pattern_info(pattern_id: int) -> Request:
    """Read a pattern's header metadata (GET_PATTERN_INFO)."""
    return _cmd(
        Opcode.GET_PATTERN_INFO, _u16(pattern_id, "pattern_id", 1), timeout=SD_TIMEOUT
    )


def set_pattern_filename(pattern_id: int, name: str) -> Request:
    """Rename a pattern (SET_PATTERN_FILENAME).

    ``pattern_id`` 0 renames (promotes) ``pattern.temp``. Uses opcode-first
    framing so names up to 63 characters fit.
    """
    raw = _pattern_name(name)
    frame = (
        bytes([Opcode.SET_PATTERN_FILENAME])
        + _u16(pattern_id, "pattern_id")
        + bytes([len(raw)])
        + raw
    )
    return Request(Opcode.SET_PATTERN_FILENAME, frame, timeout=SD_TIMEOUT)


def _pattern_name(name: str) -> bytes:
    if not isinstance(name, str):
        raise TypeError(f"name must be a str, got {type(name).__name__}")
    try:
        raw = name.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(f"pattern names must be ASCII, got {name!r}") from None
    if not 1 <= len(raw) <= MAX_PATTERN_NAME:
        raise ValueError(f"pattern names must be 1..{MAX_PATTERN_NAME} characters")
    if "/" in name or "\\" in name or "\0" in name:
        raise ValueError(f"pattern names cannot contain path separators: {name!r}")
    return raw


def get_pattern_file(pattern_id: int) -> Request:
    """Download a pattern file (GET_PATTERN_FILE); display must be off."""
    return Request(
        Opcode.GET_PATTERN_FILE,
        encode_binary(Opcode.GET_PATTERN_FILE, _u16(pattern_id, "pattern_id", 1)),
        timeout=SD_TIMEOUT,
        exchange=Exchange.BULK_READ,
    )


def set_pattern_file(pattern_id: int, data: bytes) -> Request:
    """Upload a pattern file (SET_PATTERN_FILE); display must be off.

    Args:
        pattern_id: Pattern to overwrite (1-based), or 0 for ``pattern.temp``.
        data: Complete ``.pat`` file contents.
    """
    data = bytes(data)
    if not 0 < len(data) < 2**32:
        raise ValueError("pattern data must be 1 byte .. 4 GiB")
    header = (
        bytes([Opcode.SET_PATTERN_FILE])
        + _u16(pattern_id, "pattern_id")
        + _u64(len(data))
    )
    return Request(
        Opcode.SET_PATTERN_FILE,
        header,
        timeout=SD_TIMEOUT,
        exchange=Exchange.BULK_WRITE,
        data=data,
    )


def delete_pattern_file(pattern_id: int) -> Request:
    """Delete a pattern; 0 deletes ``pattern.temp`` (DELETE_PATTERN_FILE)."""
    return _cmd(
        Opcode.DELETE_PATTERN_FILE, _u16(pattern_id, "pattern_id"), timeout=SD_TIMEOUT
    )


def purge_memory() -> Request:
    """Format the whole SD card (PURGE_MEMORY); display must be off."""
    return _cmd(Opcode.PURGE_MEMORY, timeout=PURGE_TIMEOUT)


def get_sd_archive() -> Request:
    """Download the SD card contents as a ZIP (GET_SD_ARCHIVE); display must be off."""
    return Request(
        Opcode.GET_SD_ARCHIVE,
        encode_binary(Opcode.GET_SD_ARCHIVE),
        timeout=SD_TIMEOUT,
        exchange=Exchange.BULK_READ,
    )


# -- analog / digital I/O ----------------------------------------------------


def set_ao_voltage(mv: int) -> Request:
    """Set the analog output to 0-5000 mV (SET_AO_VOLTAGE)."""
    return _cmd(Opcode.SET_AO_VOLTAGE, _u16(mv, "mv", 0, MAX_AO_MV))


def get_ao_voltage() -> Request:
    """Read the analog output back from the DAC (GET_AO_VOLTAGE)."""
    return _cmd(Opcode.GET_AO_VOLTAGE)


def set_ao_lut(
    values_mv: list[int] | tuple[int, ...],
    mode: AoLutMode | int = AoLutMode.FRAME_LOCKED,
    step_hz: int = 0,
) -> Request:
    """Load and start an analog-output lookup table (SET_AO_LUT).

    Args:
        values_mv: 1-21 levels in millivolts (0-5000).
        mode: Frame-locked (one entry per frame index) or time-based.
        step_hz: Steps per second for time-based mode (1-1000).
    """
    lut_mode = _enum(AoLutMode, mode, "mode")
    values = list(values_mv)
    if not 1 <= len(values) <= MAX_AO_LUT_ENTRIES:
        raise ValueError(
            f"a lookup table holds 1..{MAX_AO_LUT_ENTRIES} entries, got {len(values)}"
        )
    if lut_mode == AoLutMode.TIME_BASED:
        step = _u16(step_hz, "step_hz", 1, 1000)
    else:
        step = _u16(step_hz, "step_hz")
    params = bytes([lut_mode]) + step + _u16(len(values), "count")
    params += b"".join(
        _u16(v, f"values_mv[{i}]", 0, MAX_AO_MV) for i, v in enumerate(values)
    )
    return _cmd(Opcode.SET_AO_LUT, params)


def set_ao_mode(mode: AoMode | int) -> Request:
    """Choose what drives the analog output (SET_AO_MODE)."""
    return _cmd(
        Opcode.SET_AO_MODE,
        bytes([_enum(AoMode, mode, "mode")]),
        requires=Capability.IO_EXT,
    )


def get_analog_in() -> Request:
    """Read both analog inputs (GET_ANALOG_IN)."""
    return _cmd(Opcode.GET_ANALOG_IN, requires=Capability.IO_EXT)


def set_digital_out(channel: int, high: bool) -> Request:
    """Drive a Digital IO BNC high or low (SET_DIGITAL_OUT)."""
    return _cmd(
        Opcode.SET_DIGITAL_OUT, _u8(channel, "channel", 1, 2) + bytes([bool(high)])
    )


def get_digital_out() -> Request:
    """Read the level of both Digital IO pins (GET_DIGITAL_OUT)."""
    return _cmd(Opcode.GET_DIGITAL_OUT)


def set_dio_role(port: int, role: DioRole | int) -> Request:
    """Configure a Digital IO port's role (SET_DIO_ROLE)."""
    params = _u8(port, "port", 1, 2) + bytes([_enum(DioRole, role, "role")])
    return _cmd(Opcode.SET_DIO_ROLE, params, requires=Capability.IO_EXT)


def get_dio_role() -> Request:
    """Read both Digital IO roles and levels (GET_DIO_ROLE)."""
    return _cmd(Opcode.GET_DIO_ROLE, requires=Capability.IO_EXT)


# -- identity, health and diagnostics ---------------------------------------


def get_controller_info() -> Request:
    """Read protocol version, capabilities and MAC (GET_CONTROLLER_INFO)."""
    return _cmd(Opcode.GET_CONTROLLER_INFO)


def get_firmware_version() -> Request:
    """Read the firmware build identity (GET_FIRMWARE_VERSION)."""
    return _cmd(Opcode.GET_FIRMWARE_VERSION, requires=Capability.HEALTH)


def get_health() -> Request:
    """Read controller health counters (GET_HEALTH)."""
    return _cmd(Opcode.GET_HEALTH, requires=Capability.HEALTH)


def get_crash_report() -> Request:
    """Read the last recorded CPU fault (GET_CRASHREPORT)."""
    return _cmd(Opcode.GET_CRASHREPORT, requires=FirmwareFlag.CRASH_REPORT)


def set_telemetry(
    events: bool = True, *, synthetic: bool = False, synthetic_rate: int | None = None
) -> Request:
    """Configure the telemetry ring (SET_TELEMETRY).

    The watchdog control bits are never set.

    Args:
        events: Record commands, frame changes and state changes.
        synthetic: Run the synthetic test producer.
        synthetic_rate: Records per second for the synthetic producer, or
            None to keep the current rate.
    """
    flags = (0x01 if events else 0) | (0x80 if synthetic else 0)
    params = bytes([flags])
    if synthetic_rate is not None:
        params += _u16(synthetic_rate, "synthetic_rate")
    return _cmd(Opcode.SET_TELEMETRY, params, requires=FirmwareFlag.TELEMETRY)


def get_telemetry_block(
    ack_seq: int | None = None, max_bytes: int = MAX_TELEMETRY_BYTES
) -> Request:
    """Acknowledge stored records and fetch the next block (GET_TELEMETRY_BLOCK).

    Args:
        ack_seq: Highest record sequence number already stored by the host,
            or None to acknowledge nothing.
        max_bytes: Cap on record bytes returned (the firmware caps at 178).
    """
    ack = (
        TELEMETRY_NO_ACK if ack_seq is None else _int(ack_seq, "ack_seq", 0, 2**32 - 1)
    )
    params = ack.to_bytes(4, "little") + _u16(max_bytes, "max_bytes") + b"\x00"
    return _cmd(Opcode.GET_TELEMETRY_BLOCK, params, requires=FirmwareFlag.TELEMETRY)


def get_sd_info() -> Request:
    """Read SD card identity and geometry (GET_SD_INFO)."""
    return _cmd(Opcode.GET_SD_INFO, requires=FirmwareFlag.SD_FAST_PATH)


def set_sd_diag(
    *, legacy_seek: bool = False, no_same_index_skip: bool = False
) -> Request:
    """Set the SD bench switches (SET_SD_DIAG)."""
    flags = (0x01 if legacy_seek else 0) | (0x02 if no_same_index_skip else 0)
    return _cmd(Opcode.SET_SD_DIAG, bytes([flags]), requires=FirmwareFlag.SD_DIAG)


def set_diag_output(on: bool) -> Request:
    """Turn debug text output on or off (SET_DIAG_OUTPUT); debug builds only."""
    return _cmd(Opcode.SET_DIAG_OUTPUT, bytes([bool(on)]))


def get_diag_output() -> Request:
    """Read whether debug text output is on (GET_DIAG_OUTPUT)."""
    return _cmd(Opcode.GET_DIAG_OUTPUT)


def set_spi_clock(mhz: int) -> Request:
    """Set the panel SPI clock, 1-30 MHz (SET_SPI_CLOCK)."""
    return _cmd(Opcode.SET_SPI_CLOCK, _u16(mhz, "mhz", 1, 30))


def get_spi_clock() -> Request:
    """Read the panel SPI clock in MHz (GET_SPI_CLOCK)."""
    return _cmd(Opcode.GET_SPI_CLOCK)


def get_ethernet_ip_address() -> Request:
    """Read the Ethernet IP address (GET_ETHERNET_IP_ADDRESS)."""
    return _cmd(Opcode.GET_ETHERNET_IP_ADDRESS)


# -- panel firmware ----------------------------------------------------------


def set_firmware_file(data: bytes) -> Request:
    """Upload a panel firmware image to the SD card (SET_FIRMWARE_FILE)."""
    data = bytes(data)
    if not 0 < len(data) < 2**32:
        raise ValueError("firmware image must be 1 byte .. 4 GiB")
    return Request(
        Opcode.SET_FIRMWARE_FILE,
        bytes([Opcode.SET_FIRMWARE_FILE]) + _u64(len(data)),
        timeout=SD_TIMEOUT,
        exchange=Exchange.BULK_WRITE,
        data=data,
    )


def get_firmware_info() -> Request:
    """Read the footer of the stored panel firmware image (GET_FIRMWARE_INFO)."""
    return _cmd(Opcode.GET_FIRMWARE_INFO, timeout=SD_TIMEOUT)


def program_panel(panel_number: int) -> Request:
    """Flash one panel (1-based) from the stored image (PROGRAM_PANEL)."""
    return _cmd(
        Opcode.PROGRAM_PANEL,
        _u8(panel_number, "panel_number", 1),
        timeout=PANEL_ISP_TIMEOUT,
    )


def verify_panel(panel_number: int) -> Request:
    """Compare one panel's firmware with the stored image (VERIFY_PANEL)."""
    return _cmd(
        Opcode.VERIFY_PANEL,
        _u8(panel_number, "panel_number", 1),
        timeout=PANEL_ISP_TIMEOUT,
    )


# -- response checking -------------------------------------------------------


def check(response: Response) -> Response:
    """Return the response if it succeeded, otherwise raise.

    Raises:
        CommandError: If the status is non-zero.
    """
    if not response.ok:
        raise CommandError(response.opcode, response.status, response.text)
    return response


class _Cursor:
    """Sequential little-endian reader over a payload."""

    def __init__(self, data: bytes, what: str) -> None:
        self.data = data
        self.pos = 0
        self.what = what

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, fmt: str) -> tuple[int | float, ...]:
        size = struct.calcsize("<" + fmt)
        if self.remaining < size:
            raise ProtocolError(
                f"{self.what}: payload too short ({len(self.data)} bytes)"
            )
        values = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += size
        return values

    def u8(self) -> int:
        return int(self.take("B")[0])

    def u16(self) -> int:
        return int(self.take("H")[0])

    def i16(self) -> int:
        return int(self.take("h")[0])

    def u32(self) -> int:
        return int(self.take("I")[0])

    def raw(self, n: int) -> bytes:
        if self.remaining < n:
            raise ProtocolError(
                f"{self.what}: payload too short ({len(self.data)} bytes)"
            )
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def text(self, n: int) -> str:
        return self.raw(n).decode("ascii", errors="replace").rstrip(" \0")


# -- simple decoders ---------------------------------------------------------


def decode_u16(payload: bytes, what: str = "reply") -> int:
    """Decode a payload holding one little-endian u16."""
    return _Cursor(payload, what).u16()


def decode_u32(payload: bytes, what: str = "reply") -> int:
    """Decode a payload holding one little-endian u32."""
    return _Cursor(payload, what).u32()


def decode_u8(payload: bytes, what: str = "reply") -> int:
    """Decode a payload holding one byte."""
    return _Cursor(payload, what).u8()


def decode_bulk_size(payload: bytes) -> int:
    """Decode the u64 size header of a bulk read."""
    c = _Cursor(payload, "bulk size header")
    lo, hi = c.u32(), c.u32()
    return lo | (hi << 32)


def decode_pattern_filename(payload: bytes) -> str:
    """Decode a GET_PATTERN_FILENAME reply (length-prefixed name)."""
    c = _Cursor(payload, "pattern filename")
    return c.raw(c.u8()).decode("ascii", errors="replace")


@dataclass(frozen=True)
class FramePosition:
    """Current frame of the open pattern.

    Attributes:
        index: 0-based frame index.
        frame_count: Frames in the open pattern (0 if none is open).
    """

    index: int
    frame_count: int


def decode_frame_position(payload: bytes) -> FramePosition:
    """Decode a GET_FRAME_POSITION reply."""
    c = _Cursor(payload, "frame position")
    return FramePosition(index=c.u16(), frame_count=c.u16())


@dataclass(frozen=True)
class PatternInfo:
    """Header metadata of a pattern on the SD card.

    Attributes:
        frame_count: Number of frames.
        gray_level: GS2 (on/off) or GS16 (16 levels).
        rows: Panel rows the pattern was made for.
        cols: Panel columns the pattern was made for.
        arena_id: Arena id stored in the header (host metadata).
        observer_id: Observer id stored in the header (host metadata).
        file_size: File size in bytes.
        duty_cycle: Brightness byte of frame 0, panel 0.
    """

    frame_count: int
    gray_level: GrayLevel
    rows: int
    cols: int
    arena_id: int
    observer_id: int
    file_size: int
    duty_cycle: int


def decode_pattern_info(payload: bytes) -> PatternInfo:
    """Decode a GET_PATTERN_INFO reply."""
    c = _Cursor(payload, "pattern info")
    frame_count, gs = c.u16(), c.u8()
    return PatternInfo(
        frame_count=frame_count,
        gray_level=GrayLevel.GS16 if gs == 2 else GrayLevel.GS2,
        rows=c.u8(),
        cols=c.u8(),
        arena_id=c.u8(),
        observer_id=c.u8(),
        file_size=c.u32(),
        duty_cycle=c.u8(),
    )


@dataclass(frozen=True)
class AnalogInputs:
    """Analog input readings (±10 V inputs, uncalibrated).

    Attributes:
        ain1_mv: Analog In 1 in millivolts.
        ain2_mv: Analog In 2 in millivolts.
    """

    ain1_mv: int
    ain2_mv: int


def decode_analog_in(payload: bytes) -> AnalogInputs:
    """Decode a GET_ANALOG_IN reply (extra trailing bytes are ignored)."""
    c = _Cursor(payload, "analog inputs")
    return AnalogInputs(ain1_mv=c.i16(), ain2_mv=c.i16())


def decode_digital_out(payload: bytes) -> tuple[bool, bool]:
    """Decode a GET_DIGITAL_OUT reply into (port 1 high, port 2 high)."""
    c = _Cursor(payload, "digital outputs")
    return bool(c.u8()), bool(c.u8())


@dataclass(frozen=True)
class DioPort:
    """Role and live level of one Digital IO port.

    Attributes:
        role: Configured role.
        high: Pin level (the BNC level when the port is an input).
    """

    role: DioRole
    high: bool


def decode_dio_role(payload: bytes) -> tuple[DioPort, DioPort]:
    """Decode a GET_DIO_ROLE reply into (port 1, port 2)."""
    c = _Cursor(payload, "DIO roles")
    ports = []
    for _ in range(2):
        role, level = c.u8(), c.u8()
        try:
            role_enum = DioRole(role)
        except ValueError:
            raise ProtocolError(f"unknown DIO role {role}") from None
        ports.append(DioPort(role_enum, bool(level)))
    return ports[0], ports[1]


# -- identity ----------------------------------------------------------------


@dataclass(frozen=True)
class ControllerInfo:
    """Reply to GET_CONTROLLER_INFO.

    Attributes:
        protocol_version: Controller protocol version (1).
        capabilities: Advertised capability bits.
        mac: Controller MAC address ``"AA:BB:..."``, or None if not reported.
    """

    protocol_version: int
    capabilities: Capability
    mac: str | None


def decode_controller_info(payload: bytes) -> ControllerInfo:
    """Decode a GET_CONTROLLER_INFO reply."""
    c = _Cursor(payload, "controller info")
    version, caps = c.u8(), c.u8()
    mac = ":".join(f"{b:02X}" for b in c.raw(6)) if c.remaining >= 6 else None
    return ControllerInfo(version, Capability(caps), mac)


@dataclass(frozen=True)
class FirmwareVersion:
    """Reply to GET_FIRMWARE_VERSION: which firmware build is running.

    Attributes:
        rows: Panel rows of the arena this build drives.
        cols: Panel columns of the arena this build drives.
        flags: Build flags (features present, debug build, dirty tree).
        git_sha: Short git commit of the build.
        build_date: Build date ``"YYYY-MM-DD"``.
        branch: Git branch of the build.
    """

    rows: int
    cols: int
    flags: FirmwareFlag
    git_sha: str
    build_date: str
    branch: str

    @property
    def panel_count(self) -> int:
        """Number of panels in the arena."""
        return self.rows * self.cols

    def __str__(self) -> str:
        """One-line description, e.g. ``"06a6f25 2x10 2026-09-12 main"``."""
        dirty = "*" if FirmwareFlag.DIRTY in self.flags else ""
        debug = " (debug)" if FirmwareFlag.DEBUG_BUILD in self.flags else ""
        return (
            f"{self.git_sha}{dirty} {self.rows}x{self.cols} {self.build_date} "
            f"{self.branch}{debug}"
        )


def decode_firmware_version(payload: bytes) -> FirmwareVersion:
    """Decode a GET_FIRMWARE_VERSION reply."""
    c = _Cursor(payload, "firmware version")
    c.u8()  # payload schema version
    rows, cols, flags = c.u8(), c.u8(), c.u8()
    return FirmwareVersion(
        rows=rows,
        cols=cols,
        flags=FirmwareFlag(flags),
        git_sha=c.text(8),
        build_date=c.text(10),
        branch=c.text(24),
    )


@dataclass(frozen=True)
class FirmwareImageInfo:
    """Footer of the panel firmware image stored on the SD card.

    Attributes:
        magic: Image magic string.
        version: Panel firmware version string.
        crc32: CRC-32 of the image.
        size: Image size in bytes.
    """

    magic: str
    version: str
    crc32: int
    size: int


def decode_firmware_info(payload: bytes) -> FirmwareImageInfo:
    """Decode a GET_FIRMWARE_INFO reply."""
    c = _Cursor(payload, "firmware image info")
    return FirmwareImageInfo(
        magic=c.text(8), version=c.text(16), crc32=c.u32(), size=c.u32()
    )


# -- health ------------------------------------------------------------------


class HealthOp(IntEnum):
    """What the controller was doing (GET_HEALTH breadcrumb / slow-op codes)."""

    IDLE = 0
    SD_READ = 1
    SPI_FRAME = 2
    USB_WRITE = 3
    COMMAND = 4
    SD_OPEN = 5
    CMD_DISARM = 6
    CMD_PRELOAD = 7
    CMD_ARM = 8
    CMD_RESPOND = 9


class IsrId(IntEnum):
    """Interrupt handlers tracked by GET_HEALTH."""

    NONE = 0
    REFRESH = 1
    DMA = 2
    WATCHDOG = 3
    USB = 4
    SDHC = 5
    LPSPI = 6
    PIT = 7


def _op(value: int) -> HealthOp | int:
    try:
        return HealthOp(value)
    except ValueError:
        return value


def _isr(value: int) -> IsrId | int:
    try:
        return IsrId(value)
    except ValueError:
        return value


@dataclass(frozen=True)
class WatchdogStatus:
    """Watchdog and interrupt section of GET_HEALTH (payload version 2 and later).

    Attributes:
        prev_isr_last: Interrupt the previous boot was inside when it died.
        prev_isr_count: Interrupt entries in the previous boot.
        prev_pc: Program counter captured by the watchdog (if captured).
        prev_lr: Link register captured by the watchdog (if captured).
        flags: Raw watchdog flag bits.
        isr_last: Interrupt currently executing.
        isr_count: Interrupt entries this boot.
        kicks: Watchdog refreshes this boot.
    """

    prev_isr_last: IsrId | int
    prev_isr_count: int
    prev_pc: int
    prev_lr: int
    flags: int
    isr_last: IsrId | int
    isr_count: int
    kicks: int

    @property
    def armed(self) -> bool:
        """The watchdog is running."""
        return bool(self.flags & 0x01)

    @property
    def previous_reset_was_watchdog(self) -> bool:
        """The last reset was caused by the watchdog."""
        return bool(self.flags & 0x02)

    @property
    def previous_pc_captured(self) -> bool:
        """``prev_pc`` and ``prev_lr`` are valid."""
        return bool(self.flags & 0x04)


@dataclass(frozen=True)
class Health:
    """Controller health counters (GET_HEALTH).

    Counters are cumulative since boot; nothing is cleared by reading. The
    "previous" fields describe the boot before the last reset (they survive a
    software or watchdog reset, not a power cycle).

    Attributes:
        version: Payload schema version.
        sd_mounted: An SD card is mounted.
        pattern_open: A pattern file is open.
        display_active: The display is running (state is not ALL_OFF).
        breadcrumb_valid: The ``previous_*`` fields are meaningful.
        uptime_ms: Milliseconds since boot (wraps after ~49.7 days).
        loop_count: Main-loop iterations since boot.
        loop_max_us: Longest main-loop iteration since boot.
        loop_max_1s_us: Longest iteration in the last full second.
        sd_reads: Frame reads from the SD card since boot.
        sd_read_max_us: Longest frame read since boot.
        sd_error: SD driver error code (0 = none).
        sd_error_data: SD driver error data.
        frames_sent: Frames pushed to the panels (same as GET_FRAMES_SENT).
        refresh_isr_count: Refresh-timer interrupts since boot.
        frame_position_commands: SET_FRAME_POSITION commands received.
        state: Current controller state.
        current_frame: Current frame index.
        reset_cause: Raw reset-cause register captured at boot.
        previous_op: What the previous boot was doing last.
        previous_op_us: Timestamp of that operation (µs).
        previous_op_arg: Opcode, when ``previous_op`` is a command.
        previous_slow_op: Slowest operation of the previous boot.
        previous_slow_us: Its duration (µs).
        slow_op: Slowest operation of this boot.
        slow_us: Its duration (µs).
        watchdog: Watchdog section (payload version 2+), else None.
        raw: The complete payload, for fields added in later versions.
    """

    version: int
    sd_mounted: bool
    pattern_open: bool
    display_active: bool
    breadcrumb_valid: bool
    uptime_ms: int
    loop_count: int
    loop_max_us: int
    loop_max_1s_us: int
    sd_reads: int
    sd_read_max_us: int
    sd_error: int
    sd_error_data: int
    frames_sent: int
    refresh_isr_count: int
    frame_position_commands: int
    state: ArenaState | int
    current_frame: int
    reset_cause: int
    previous_op: HealthOp | int
    previous_op_us: int
    previous_op_arg: int | None = None
    previous_slow_op: HealthOp | int | None = None
    previous_slow_us: int | None = None
    slow_op: HealthOp | int | None = None
    slow_us: int | None = None
    watchdog: WatchdogStatus | None = None
    raw: bytes = field(default=b"", repr=False)


def decode_health(payload: bytes) -> Health:
    """Decode a GET_HEALTH reply of any payload version (55 bytes or more)."""
    c = _Cursor(payload, "health")
    version, flags = c.u8(), c.u8()
    base: dict[str, object] = {
        "version": version,
        "sd_mounted": bool(flags & 0x01),
        "pattern_open": bool(flags & 0x02),
        "display_active": bool(flags & 0x04),
        "breadcrumb_valid": bool(flags & 0x08),
        "uptime_ms": c.u32(),
        "loop_count": c.u32(),
        "loop_max_us": c.u32(),
        "loop_max_1s_us": c.u32(),
        "sd_reads": c.u32(),
        "sd_read_max_us": c.u32(),
        "sd_error": c.u8(),
        "sd_error_data": c.u32(),
        "frames_sent": c.u32(),
        "refresh_isr_count": c.u32(),
        "frame_position_commands": c.u32(),
    }
    state = c.u8()
    try:
        base["state"] = ArenaState(state)
    except ValueError:
        base["state"] = state
    base["current_frame"] = c.u16()
    base["reset_cause"] = c.u32()
    base["previous_op"] = _op(c.u8())
    base["previous_op_us"] = c.u32()
    if c.remaining >= 11:
        base["previous_op_arg"] = c.u8()
        base["previous_slow_op"] = _op(c.u8())
        base["previous_slow_us"] = c.u32()
        base["slow_op"] = _op(c.u8())
        base["slow_us"] = c.u32()
    if c.remaining >= 23:
        base["watchdog"] = WatchdogStatus(
            prev_isr_last=_isr(c.u8()),
            prev_isr_count=c.u32(),
            prev_pc=c.u32(),
            prev_lr=c.u32(),
            flags=c.u8(),
            isr_last=_isr(c.u8()),
            isr_count=c.u32(),
            kicks=c.u32(),
        )
    return Health(raw=bytes(payload), **base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CrashReport:
    """Last CPU fault recorded by the Teensy core (GET_CRASHREPORT).

    Attributes:
        raw: The full 128-byte record.
        length: Record length word (11 words when a fault was recorded).
        ipsr: Exception number of the fault.
        cfsr: Configurable fault status register.
        hfsr: Hard fault status register.
        mmfar: MemManage fault address.
        bfar: Bus fault address.
        return_address: Address of the faulting instruction.
        xpsr: Program status register.
        temperature_c: CPU temperature at the fault.
        time: RTC time of the fault.
    """

    raw: bytes = field(repr=False)
    length: int
    ipsr: int
    cfsr: int
    hfsr: int
    mmfar: int
    bfar: int
    return_address: int
    xpsr: int
    temperature_c: float
    time: int

    @property
    def present(self) -> bool:
        """A fault has been recorded."""
        return self.length in (11, 44) and self.ipsr != 0

    @property
    def fault_name(self) -> str | None:
        """Name of the fault type, or None if no fault is recorded."""
        if not self.present:
            return None
        names = {3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault"}
        return names.get(self.ipsr, f"exception {self.ipsr}")


def decode_crash_report(payload: bytes) -> CrashReport:
    """Decode a GET_CRASHREPORT reply."""
    c = _Cursor(payload, "crash report")
    length, ipsr, cfsr, hfsr, mmfar, bfar, ret, xpsr = (c.u32() for _ in range(8))
    temperature = float(c.take("f")[0])
    return CrashReport(
        raw=bytes(payload),
        length=length,
        ipsr=ipsr,
        cfsr=cfsr,
        hfsr=hfsr,
        mmfar=mmfar,
        bfar=bfar,
        return_address=ret,
        xpsr=xpsr,
        temperature_c=temperature,
        time=c.u32(),
    )


@dataclass(frozen=True)
class SdInfo:
    """SD card identity and geometry (GET_SD_INFO).

    Attributes:
        mounted: A card is mounted (other fields are zero otherwise).
        cid_valid: ``cid`` was read.
        csd_valid: ``sectors`` was read.
        card_type: 0 none, 1 SD1, 2 SD2, 3 SDHC/SDXC.
        fat_type: 12, 16 or 32 for FAT, 64 for exFAT, 0 unknown.
        sectors: Capacity in 512-byte sectors.
        bytes_per_cluster: Cluster size.
        cid: Raw 16-byte card identification register.
        legacy_seek: SD diagnostic switch: legacy seek requested.
        no_same_index_skip: SD diagnostic switch: same-index skip disabled.
        legacy_seek_applied: Legacy seek is in force for the open file.
    """

    mounted: bool
    cid_valid: bool
    csd_valid: bool
    card_type: int
    fat_type: int
    sectors: int
    bytes_per_cluster: int
    cid: bytes
    legacy_seek: bool
    no_same_index_skip: bool
    legacy_seek_applied: bool

    @property
    def capacity_bytes(self) -> int:
        """Card capacity in bytes."""
        return self.sectors * 512

    @property
    def manufacturer_id(self) -> int:
        """Manufacturer id from the CID register."""
        return self.cid[0]

    @property
    def product_name(self) -> str:
        """Product name from the CID register."""
        return self.cid[3:8].decode("ascii", errors="replace").strip()

    @property
    def serial_number(self) -> int:
        """Product serial number from the CID register."""
        return int.from_bytes(self.cid[9:13], "big")


def decode_sd_info(payload: bytes) -> SdInfo:
    """Decode a GET_SD_INFO reply (also valid when the status reports no card)."""
    c = _Cursor(payload, "SD info")
    c.u8()  # schema version
    flags = c.u8()
    card_type, fat_type = c.u8(), c.u8()
    sectors, cluster = c.u32(), c.u32()
    cid = c.raw(16)
    c.u8()  # SD status maintenance bits (not read by this firmware)
    diag = c.u8()
    return SdInfo(
        mounted=bool(flags & 0x01),
        cid_valid=bool(flags & 0x02),
        csd_valid=bool(flags & 0x04),
        card_type=card_type,
        fat_type=fat_type,
        sectors=sectors,
        bytes_per_cluster=cluster,
        cid=cid,
        legacy_seek=bool(diag & 0x01),
        no_same_index_skip=bool(diag & 0x02),
        legacy_seek_applied=bool(diag & 0x04),
    )


def decode_sd_diag(payload: bytes) -> tuple[bool, bool]:
    """Decode a SET_SD_DIAG reply into (legacy_seek, no_same_index_skip)."""
    flags = decode_u8(payload, "SD diag flags")
    return bool(flags & 0x01), bool(flags & 0x02)
