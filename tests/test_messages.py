import struct

import pytest

from arena_interface import CommandError, ProtocolError
from arena_interface import messages as m
from arena_interface.framing import Response
from arena_interface.protocol import (
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
    stream_frame_size,
)

# -- request frames ---------------------------------------------------------
# Expected bytes are taken from docs/PROTOCOL.md and the firmware handlers.

FRAMES = [
    (m.all_off(), "01 00"),
    (m.all_on(), "01 ff"),
    (m.stop_display(), "01 30"),
    (m.system_reset(), "01 01"),
    (m.set_pattern_id(258), "03 03 02 01"),
    (m.set_frame_position(1), "03 70 01 00"),
    (m.get_frame_position(), "01 72"),
    (m.display_psram_index(7), "03 3a 07 00"),
    (m.psram_play(10, 5, 30), "07 3b 0a 00 05 00 1e 00"),
    (m.set_refresh_rate(500), "03 16 f4 01"),
    (m.get_refresh_rate(), "01 17"),
    (m.set_panel_display_mode(PanelDisplayMode.GATED), "02 1b 03"),
    (m.get_panel_display_mode(), "01 1c"),
    (m.get_frames_sent(), "01 33"),
    (m.reset_frames_sent(), "01 34"),
    (m.get_file_count(), "01 80"),
    (m.get_pattern_filename(1), "03 82 01 00"),
    (m.get_pattern_info(3), "03 88 03 00"),
    (m.get_pattern_file(2), "03 84 02 00"),
    (m.delete_pattern_file(0), "03 86 00 00"),
    (m.purge_memory(), "01 8f"),
    (m.get_sd_archive(), "01 8a"),
    (m.set_ao_voltage(5000), "03 a0 88 13"),
    (m.get_ao_voltage(), "01 a1"),
    (m.set_ao_mode(AoMode.FRAME_NUMBER), "02 a3 01"),
    (m.get_analog_in(), "01 a4"),
    (m.set_digital_out(2, True), "03 aa 02 01"),
    (m.get_digital_out(), "01 ab"),
    (m.set_dio_role(1, DioRole.IN_TRIGGER), "03 ac 01 01"),
    (m.get_dio_role(), "01 ad"),
    (m.get_controller_info(), "01 c2"),
    (m.get_firmware_version(), "01 cb"),
    (m.get_health(), "01 ca"),
    (m.get_crash_report(), "01 cc"),
    (m.set_telemetry(True), "02 a8 01"),
    (m.set_telemetry(False, synthetic=True, synthetic_rate=1000), "04 a8 80 e8 03"),
    (m.get_telemetry_block(), "08 a9 ff ff ff ff b2 00 00"),
    (
        m.get_telemetry_block(ack_seq=0x01020304, max_bytes=64),
        "08 a9 04 03 02 01 40 00 00",
    ),
    (m.get_sd_info(), "01 cd"),
    (m.set_sd_diag(legacy_seek=True, no_same_index_skip=True), "02 ce 03"),
    (m.set_diag_output(False), "02 c3 00"),
    (m.get_diag_output(), "01 c4"),
    (m.set_spi_clock(30), "03 c5 1e 00"),
    (m.get_spi_clock(), "01 c6"),
    (m.get_ethernet_ip_address(), "01 c1"),
    (m.get_firmware_info(), "01 e3"),
    (m.program_panel(12), "02 c8 0c"),
    (m.verify_panel(1), "02 c9 01"),
]


@pytest.mark.parametrize(
    ("request_", "expected"), FRAMES, ids=lambda x: getattr(x, "opcode", x)
)
def test_request_frames(request_: m.Request, expected: str) -> None:
    assert request_.frame.hex(" ") == expected
    assert request_.opcode == request_.frame[1]


def test_trial_params_without_duty() -> None:
    req = m.trial_params(
        TrialMode.OPEN_LOOP,
        pattern_id=4,
        frame_rate=-30,
        initial_frame=7,
        gain=-20,
        duration=2.5,
    )
    # [0C 08 mode pat rate init gain dur]; dur = 250 ticks of 10 ms
    assert req.frame.hex(" ") == "0c 08 02 04 00 e2 ff 07 00 ec ff fa 00"


def test_trial_params_with_duty() -> None:
    req = m.trial_params(3, 1, duty_cycle=200)
    assert req.frame.hex(" ") == "0d 08 03 01 00 00 00 00 00 00 00 00 00 c8"


def test_set_pattern_filename_is_opcode_first() -> None:
    name = "x" * 63
    req = m.set_pattern_filename(5, name)
    assert req.frame[:4] == bytes([0x83, 0x05, 0x00, 63])
    assert req.frame[4:] == name.encode()
    assert req.opcode is Opcode.SET_PATTERN_FILENAME


def test_set_pattern_file_is_bulk_write() -> None:
    data = bytes(1000)
    req = m.set_pattern_file(0, data)
    assert req.exchange is m.Exchange.BULK_WRITE
    assert req.frame == bytes([0x85, 0, 0]) + (1000).to_bytes(8, "little")
    assert req.data == data


def test_set_firmware_file_is_bulk_write() -> None:
    req = m.set_firmware_file(b"\x01\x02\x03")
    assert req.exchange is m.Exchange.BULK_WRITE
    assert req.frame == bytes([0xE0]) + (3).to_bytes(8, "little")


@pytest.mark.parametrize("req", [m.get_pattern_file(1), m.get_sd_archive()])
def test_bulk_reads(req: m.Request) -> None:
    assert req.exchange is m.Exchange.BULK_READ


def test_stream_frame() -> None:
    payload = bytes(stream_frame_size(20, GrayLevel.GS16))
    req = m.stream_frame(payload)
    assert req.opcode is Opcode.STREAM_FRAME
    assert req.frame[:3] == bytes([0x32]) + len(payload).to_bytes(2, "little")


def test_ao_lut_largest_fits_binary_framing() -> None:
    req = m.set_ao_lut([5000] * 21, AoLutMode.TIME_BASED, step_hz=1000)
    # firmware requires claimed_len == 6 + 2 * count
    assert req.frame[0] == 6 + 2 * 21
    assert req.frame[1:7] == bytes([0xA2, 1, 0xE8, 0x03, 21, 0])


def test_slow_commands_have_long_timeouts() -> None:
    assert m.purge_memory().timeout >= 120
    assert m.program_panel(1).timeout >= 30
    assert m.all_off().timeout == m.DEFAULT_TIMEOUT


@pytest.mark.parametrize(
    ("req", "requirement"),
    [
        (m.set_ao_mode(0), Capability.IO_EXT),
        (m.get_analog_in(), Capability.IO_EXT),
        (m.set_dio_role(1, 0), Capability.IO_EXT),
        (m.get_dio_role(), Capability.IO_EXT),
        (m.get_health(), Capability.HEALTH),
        (m.get_firmware_version(), Capability.HEALTH),
        (m.set_telemetry(), FirmwareFlag.TELEMETRY),
        (m.get_telemetry_block(), FirmwareFlag.TELEMETRY),
        (m.get_crash_report(), FirmwareFlag.CRASH_REPORT),
        (m.get_sd_info(), FirmwareFlag.SD_FAST_PATH),
        (m.set_sd_diag(), FirmwareFlag.SD_DIAG),
        (m.all_off(), None),
        (m.get_controller_info(), None),
    ],
)
def test_requirements(req: m.Request, requirement: object) -> None:
    assert req.requires == requirement


# -- argument validation ----------------------------------------------------

BAD_ARGS = [
    (lambda: m.trial_params(5, 1), ValueError),  # mode 5 is streaming, not selectable
    (lambda: m.trial_params(2, 0), ValueError),  # patterns are 1-based
    (lambda: m.trial_params(2, 1, frame_rate=40000), ValueError),
    (lambda: m.trial_params(2, 1, duty_cycle=0), ValueError),
    (lambda: m.trial_params(2, 1, duration=700), ValueError),
    (lambda: m.trial_params(2, 1, duration=-1), ValueError),
    (lambda: m.set_pattern_id(0), ValueError),
    (lambda: m.set_frame_position(-1), ValueError),
    (lambda: m.set_frame_position(1.5), TypeError),
    (lambda: m.psram_play(0, 0, 10), ValueError),
    (lambda: m.set_refresh_rate(0), ValueError),
    (lambda: m.set_panel_display_mode(4), ValueError),
    (lambda: m.get_pattern_filename(0), ValueError),
    (lambda: m.set_pattern_filename(1, ""), ValueError),
    (lambda: m.set_pattern_filename(1, "x" * 64), ValueError),
    (lambda: m.set_pattern_filename(1, "café.pat"), ValueError),
    (lambda: m.set_pattern_filename(1, "a/b.pat"), ValueError),
    (lambda: m.set_pattern_file(1, b""), ValueError),
    (lambda: m.set_ao_voltage(5001), ValueError),
    (lambda: m.set_ao_lut([]), ValueError),
    (lambda: m.set_ao_lut([0] * 22), ValueError),
    (lambda: m.set_ao_lut([6000]), ValueError),
    (lambda: m.set_ao_lut([0], AoLutMode.TIME_BASED, step_hz=0), ValueError),
    (lambda: m.set_ao_lut([0], AoLutMode.TIME_BASED, step_hz=1001), ValueError),
    (lambda: m.set_ao_mode(2), ValueError),
    (lambda: m.set_digital_out(3, True), ValueError),
    (lambda: m.set_dio_role(0, 0), ValueError),
    (lambda: m.set_dio_role(1, 4), ValueError),
    (lambda: m.set_spi_clock(0), ValueError),
    (lambda: m.set_spi_clock(31), ValueError),
    (lambda: m.program_panel(0), ValueError),
    (lambda: m.get_telemetry_block(ack_seq=-1), ValueError),
]


@pytest.mark.parametrize(("make", "exc"), BAD_ARGS)
def test_invalid_arguments(make: object, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        make()  # type: ignore[operator]


# -- check ------------------------------------------------------------------


def test_check_passes_ok_response() -> None:
    resp = Response(0, 0x30, b"Display has been stopped")
    assert m.check(resp) is resp


def test_check_raises_command_error() -> None:
    resp = Response(10, 0x84, b"Stop display first")
    with pytest.raises(CommandError) as excinfo:
        m.check(resp)
    err = excinfo.value
    assert (err.opcode, err.status, err.message) == (0x84, 10, "Stop display first")
    assert "0x84" in str(err) and "Stop display first" in str(err)


# -- decoders ---------------------------------------------------------------


def test_decode_scalars() -> None:
    assert m.decode_u16(b"\x2c\x01") == 300
    assert m.decode_u32(b"\x01\x00\x00\x01") == 0x01000001
    assert m.decode_u8(b"\x03") == 3
    assert m.decode_bulk_size((2**33 + 5).to_bytes(8, "little")) == 2**33 + 5


def test_short_payload_raises_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="frame position"):
        m.decode_frame_position(b"\x01\x00")


def test_decode_pattern_filename() -> None:
    assert m.decode_pattern_filename(b"\x08grat.pat") == "grat.pat"


def test_decode_frame_position() -> None:
    assert m.decode_frame_position(b"\x05\x00\x60\x00") == m.FramePosition(5, 96)


def test_decode_pattern_info() -> None:
    payload = struct.pack("<HBBBBBIB", 96, 2, 2, 10, 3, 7, 390_000, 128)
    info = m.decode_pattern_info(payload)
    assert info == m.PatternInfo(96, GrayLevel.GS16, 2, 10, 3, 7, 390_000, 128)


def test_decode_analog_in_signed() -> None:
    payload = struct.pack("<hh", -9876, 1234) + b"\x07"  # newer firmware adds flags
    assert m.decode_analog_in(payload) == m.AnalogInputs(-9876, 1234)


def test_decode_digital_out() -> None:
    assert m.decode_digital_out(b"\x00\x01") == (False, True)


def test_decode_dio_role() -> None:
    p1, p2 = m.decode_dio_role(bytes([2, 0, 1, 1]))
    assert p1 == m.DioPort(DioRole.OUT_PROGRAMMABLE, False)
    assert p2 == m.DioPort(DioRole.IN_TRIGGER, True)


def test_decode_controller_info() -> None:
    info = m.decode_controller_info(
        bytes([1, 0xA3, 0x04, 0xE9, 0xE5, 0x12, 0x34, 0x56])
    )
    assert info.protocol_version == 1
    assert info.capabilities == (
        Capability.G6_MODE
        | Capability.V2_LOCAL_STORAGE
        | Capability.IO_EXT
        | Capability.HEALTH
    )
    assert info.mac == "04:E9:E5:12:34:56"


def test_decode_controller_info_without_mac() -> None:
    assert m.decode_controller_info(bytes([1, 0x01])).mac is None


def test_decode_firmware_version() -> None:
    payload = (
        bytes([1, 2, 10, 0x7D])
        + b"06a6f25 "
        + b"2026-09-12"
        + b"feat/controller-health".ljust(24)
    )
    assert len(payload) == 46
    v = m.decode_firmware_version(payload)
    assert (v.rows, v.cols, v.panel_count) == (2, 10, 20)
    assert v.git_sha == "06a6f25"
    assert v.branch == "feat/controller-health"
    assert FirmwareFlag.TELEMETRY in v.flags
    assert FirmwareFlag.DEBUG_BUILD not in v.flags
    assert str(v) == "06a6f25* 2x10 2026-09-12 feat/controller-health"


def _health_payload(version: int, length: int) -> bytes:
    """Build a GET_HEALTH payload field by field, following the firmware offsets."""
    p = bytearray(length)
    p[0] = version
    p[1] = 0b1011  # sd_mounted, pattern_open, breadcrumb_valid
    struct.pack_into("<I", p, 2, 123_456)  # uptime_ms
    struct.pack_into("<I", p, 22, 31_000)  # sd_read_max_us
    p[26] = 0x17  # sd_err
    struct.pack_into("<I", p, 31, 9999)  # frames_sent
    struct.pack_into("<I", p, 39, 42)  # cmd70_count
    p[43] = 4  # state SHOW_FRAME
    struct.pack_into("<H", p, 44, 77)  # cur_frame
    p[50] = 4  # prev breadcrumb = command
    struct.pack_into("<I", p, 51, 555)
    if length >= 66:
        p[55] = 0x01  # previous command was SYSTEM_RESET
        p[56] = 1  # prev slow op = SD read
        struct.pack_into("<I", p, 57, 88_000)
        p[61] = 5
        struct.pack_into("<I", p, 62, 1_500)
    if length >= 89:
        p[66] = 1  # prev isr = refresh
        struct.pack_into("<I", p, 71, 0x212F4)  # prev wdog pc
        p[79] = 0b111  # armed, prev reset = watchdog, pc captured
        struct.pack_into("<I", p, 85, 314)  # wdog kicks
    return bytes(p)


def test_decode_health_v5() -> None:
    h = m.decode_health(_health_payload(5, 114))
    assert h.version == 5
    assert (
        h.sd_mounted and h.pattern_open and not h.display_active and h.breadcrumb_valid
    )
    assert h.uptime_ms == 123_456
    assert h.sd_read_max_us == 31_000
    assert h.sd_error == 0x17
    assert h.frames_sent == 9999
    assert h.frame_position_commands == 42
    assert h.state is ArenaState.SHOW_FRAME
    assert h.current_frame == 77
    assert h.previous_op is m.HealthOp.COMMAND
    assert h.previous_op_us == 555
    assert h.previous_op_arg == 0x01
    assert h.previous_slow_op is m.HealthOp.SD_READ
    assert h.previous_slow_us == 88_000
    assert h.slow_op is m.HealthOp.SD_OPEN
    assert h.slow_us == 1_500
    assert h.watchdog is not None
    assert h.watchdog.prev_isr_last is m.IsrId.REFRESH
    assert h.watchdog.prev_pc == 0x212F4
    assert h.watchdog.armed and h.watchdog.previous_reset_was_watchdog
    assert h.watchdog.previous_pc_captured
    assert h.watchdog.kicks == 314
    assert len(h.raw) == 114


def test_decode_health_v1_base_only() -> None:
    h = m.decode_health(_health_payload(1, 55))
    assert h.current_frame == 77
    assert h.previous_slow_op is None
    assert h.watchdog is None


def test_decode_health_too_short() -> None:
    with pytest.raises(ProtocolError):
        m.decode_health(bytes(54))


def test_decode_crash_report() -> None:
    words = struct.pack(
        "<8IfII",
        11,
        3,
        0x8200,
        0x40000000,
        0,
        0x2000,
        0x60001234,
        0x21000003,
        41.5,
        1_700_000_000,
        0xABCD,
    )
    report = m.decode_crash_report(words + bytes(128 - len(words)))
    assert report.present
    assert report.fault_name == "HardFault"
    assert report.return_address == 0x60001234
    assert report.temperature_c == pytest.approx(41.5)
    assert len(report.raw) == 128


def test_decode_crash_report_empty() -> None:
    report = m.decode_crash_report(bytes(128))
    assert not report.present
    assert report.fault_name is None


def test_decode_sd_info() -> None:
    cid = (
        bytes([0x03, 0x53, 0x44])
        + b"SC64G"
        + bytes([0x80, 0x12, 0x34, 0x56, 0x78, 0x01, 0x5A, 0x00])
    )
    payload = (
        bytes([1, 0x07, 3, 64])
        + struct.pack("<II", 124_735_488, 131_072)
        + cid
        + bytes([0xFF, 0x05])
    )
    assert len(payload) == 30
    info = m.decode_sd_info(payload)
    assert info.mounted and info.cid_valid and info.csd_valid
    assert info.fat_type == 64
    assert info.capacity_bytes == 124_735_488 * 512
    assert info.manufacturer_id == 0x03
    assert info.product_name == "SC64G"
    assert info.serial_number == 0x12345678
    assert info.legacy_seek and not info.no_same_index_skip and info.legacy_seek_applied


def test_decode_sd_diag() -> None:
    assert m.decode_sd_diag(b"\x02") == (False, True)


def test_decode_firmware_info() -> None:
    payload = (
        b"G6PANEL\0"
        + b"v1.4.2".ljust(16, b"\0")
        + struct.pack("<II", 0xDEADBEEF, 65536)
    )
    info = m.decode_firmware_info(payload)
    assert info == m.FirmwareImageInfo("G6PANEL", "v1.4.2", 0xDEADBEEF, 65536)
