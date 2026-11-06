"""Protocol constants and enumerations for the G6 arena controller.

Values come from the firmware sources (``commands.h``, ``constants.h``,
``Version.h``, ``CommandProcessor.h``); see ``docs/PROTOCOL.md``.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag

__all__ = [
    "PANEL_SIZE",
    "AoLutMode",
    "AoMode",
    "ArenaState",
    "Capability",
    "DioRole",
    "ErrorCode",
    "FirmwareFlag",
    "GrayLevel",
    "Opcode",
    "PanelDisplayMode",
    "TrialMode",
    "panel_block_size",
    "stream_frame_size",
]

#: Pixels along each side of a G6 panel.
PANEL_SIZE = 20
#: Bytes in the "FR" + frame-index prefix of every frame.
FRAME_PREFIX_SIZE = 4
#: Unit of the TRIAL_PARAMS duration field, in seconds.
DURATION_TICK_S = 0.010
#: Largest pattern name (excluding the terminating NUL the firmware adds).
MAX_PATTERN_NAME = 63
#: Most entries a single SET_AO_LUT command can carry (binary framing limit).
MAX_AO_LUT_ENTRIES = 21
#: Highest analog-output level in millivolts.
MAX_AO_MV = 5000
#: Payload limit of GET_TELEMETRY_BLOCK record bytes.
MAX_TELEMETRY_BYTES = 178


class Opcode(IntEnum):
    """Command opcodes implemented by this firmware (``commands.h``)."""

    ALL_OFF = 0x00
    SYSTEM_RESET = 0x01
    SET_PATTERN_ID = 0x03
    TRIAL_PARAMS = 0x08
    SET_REFRESH_RATE = 0x16
    GET_REFRESH_RATE = 0x17
    SET_PANEL_DISPLAY_MODE = 0x1B
    GET_PANEL_DISPLAY_MODE = 0x1C
    STOP_DISPLAY = 0x30
    STREAM_FRAME = 0x32
    GET_FRAMES_SENT = 0x33
    RESET_FRAMES_SENT = 0x34
    DISPLAY_PSRAM_INDEX = 0x3A
    PSRAM_PLAY = 0x3B
    SET_FRAME_POSITION = 0x70
    GET_FRAME_POSITION = 0x72
    GET_FILE_COUNT = 0x80
    GET_PATTERN_FILENAME = 0x82
    SET_PATTERN_FILENAME = 0x83
    GET_PATTERN_FILE = 0x84
    SET_PATTERN_FILE = 0x85
    DELETE_PATTERN_FILE = 0x86
    GET_PATTERN_INFO = 0x88
    GET_SD_ARCHIVE = 0x8A
    PURGE_MEMORY = 0x8F
    SET_AO_VOLTAGE = 0xA0
    GET_AO_VOLTAGE = 0xA1
    SET_AO_LUT = 0xA2
    SET_AO_MODE = 0xA3
    GET_ANALOG_IN = 0xA4
    SET_TELEMETRY = 0xA8
    GET_TELEMETRY_BLOCK = 0xA9
    SET_DIGITAL_OUT = 0xAA
    GET_DIGITAL_OUT = 0xAB
    SET_DIO_ROLE = 0xAC
    GET_DIO_ROLE = 0xAD
    GET_ETHERNET_IP_ADDRESS = 0xC1
    GET_CONTROLLER_INFO = 0xC2
    SET_DIAG_OUTPUT = 0xC3
    GET_DIAG_OUTPUT = 0xC4
    SET_SPI_CLOCK = 0xC5
    GET_SPI_CLOCK = 0xC6
    PROGRAM_PANEL = 0xC8
    VERIFY_PANEL = 0xC9
    GET_HEALTH = 0xCA
    GET_FIRMWARE_VERSION = 0xCB
    GET_CRASHREPORT = 0xCC
    GET_SD_INFO = 0xCD
    SET_SD_DIAG = 0xCE
    SET_FIRMWARE_FILE = 0xE0
    GET_FIRMWARE_INFO = 0xE3
    ALL_ON = 0xFF


class ErrorCode(IntEnum):
    """Controller error codes (``CE_*``), used as status bytes and error glyphs.

    Status 1 is also used for generic validation failures, so a status of 1
    does not necessarily mean an unknown command.
    """

    UNKNOWN_CMD = 1
    BAD_PAYLOAD_LEN = 2
    BAD_PARAM = 3
    SD_NOT_PRESENT = 4
    SD_FILE_ERROR = 5
    HEADER_CRC = 6
    FRAME_CRC = 7
    ARENA_MISMATCH = 8
    MANIFEST_WRITE_ERROR = 9
    DISPLAY_ACTIVE = 10
    SD_FORMAT_ERROR = 11


class ArenaState(IntEnum):
    """What the controller is doing (``GET_HEALTH`` state field)."""

    ALL_OFF = 0
    ALL_ON = 1
    STREAMING_FRAME = 2
    OPEN_LOOP = 3
    SHOW_FRAME = 4
    CLOSED_LOOP = 5
    PSRAM_PLAY = 6
    ERROR_DISPLAY = 7


class TrialMode(IntEnum):
    """Display modes selectable with TRIAL_PARAMS."""

    OPEN_LOOP = 2
    """Frames advance automatically at the frame rate."""
    SHOW_FRAME = 3
    """The host chooses the frame (SET_FRAME_POSITION)."""
    CLOSED_LOOP = 4
    """Frame rate follows Analog In 1 times the gain."""


class PanelDisplayMode(IntEnum):
    """How panels present each frame (SET_PANEL_DISPLAY_MODE)."""

    ONESHOT = 0
    PERSIST = 1
    TRIGGERED = 2
    GATED = 3


class DioRole(IntEnum):
    """Role of a "Digital IO (5V)" BNC port."""

    OFF = 0
    IN_TRIGGER = 1
    OUT_PROGRAMMABLE = 2
    OUT_DEBUG_FRAMESCAN = 3


class AoMode(IntEnum):
    """What drives the analog output."""

    PROGRAMMABLE = 0
    """Set by SET_AO_VOLTAGE or a lookup table."""
    FRAME_NUMBER = 1
    """Tracks the pattern frame index, 0 V (first) to 5 V (last)."""


class AoLutMode(IntEnum):
    """How an analog-output lookup table advances."""

    FRAME_LOCKED = 0
    """One entry per displayed frame index."""
    TIME_BASED = 1
    """Steps at a fixed rate."""


class GrayLevel(IntEnum):
    """Brightness levels per pixel; the value is the number of levels."""

    GS2 = 2
    GS16 = 16


class Capability(IntFlag):
    """GET_CONTROLLER_INFO capability bitmap."""

    G6_MODE = 0x01
    V2_LOCAL_STORAGE = 0x02
    MODE_1_TSI = 0x04
    V3_TRIGGERED = 0x08
    V3_GATED = 0x10
    IO_EXT = 0x20
    """SET_AO_MODE, GET_ANALOG_IN, SET_DIO_ROLE, GET_DIO_ROLE."""
    AI_CAL = 0x40
    HEALTH = 0x80
    """GET_HEALTH and GET_FIRMWARE_VERSION."""


class FirmwareFlag(IntFlag):
    """GET_FIRMWARE_VERSION flags byte."""

    DIRTY = 0x01
    DEBUG_BUILD = 0x02
    TELEMETRY = 0x04
    """SET_TELEMETRY and GET_TELEMETRY_BLOCK."""
    CRASH_REPORT = 0x08
    """GET_CRASHREPORT."""
    FREE_RUNNING_REFRESH = 0x10
    SD_FAST_PATH = 0x20
    """GET_SD_INFO."""
    SD_DIAG = 0x40
    """SET_SD_DIAG."""


#: Panel-block size in bytes for each gray level (header, opcode, pixels, duty).
_BLOCK_SIZE = {GrayLevel.GS2: 53, GrayLevel.GS16: 203}


def panel_block_size(gray_level: GrayLevel) -> int:
    """Bytes in one panel block at the given gray level (53 or 203)."""
    return _BLOCK_SIZE[GrayLevel(gray_level)]


def stream_frame_size(panel_count: int, gray_level: GrayLevel) -> int:
    """Bytes in one frame payload for an arena of ``panel_count`` panels.

    Example:
        >>> stream_frame_size(20, GrayLevel.GS2)
        1064
    """
    return FRAME_PREFIX_SIZE + panel_count * panel_block_size(gray_level)
