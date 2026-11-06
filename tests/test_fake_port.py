"""Behaviour of the simulated controller, driven through the real transport."""

from __future__ import annotations

import io
import logging
import struct
import time
import zipfile
from collections.abc import Iterator

import pytest

from arena_interface import DeviceDisconnected, DeviceTimeout
from arena_interface import messages as m
from arena_interface._crc import crc32
from arena_interface.framing import Response
from arena_interface.protocol import (
    AoMode,
    ArenaState,
    Capability,
    DioRole,
    ErrorCode,
    FirmwareFlag,
    GrayLevel,
    Opcode,
    stream_frame_size,
)
from arena_interface.telemetry import CommandRecord, StateRecord, decode_telemetry_block
from arena_interface.testing import FakeController, FakePort, make_test_pattern
from arena_interface.transport import Transport


@pytest.fixture
def ctrl() -> FakeController:
    return FakeController()


@pytest.fixture
def link(ctrl: FakeController) -> Iterator[Transport]:
    with Transport(FakePort(ctrl)) as t:
        yield t


def send(link: Transport, req: m.Request) -> Response:
    if req.exchange is m.Exchange.BULK_WRITE:
        return link.request_bulk_write(req.frame, req.data, expect=req.opcode)
    return link.request(req.frame, expect=req.opcode, timeout=req.timeout)


def ok(link: Transport, req: m.Request) -> bytes:
    return m.check(send(link, req)).payload


# -- identity ---------------------------------------------------------------


def test_controller_info_and_version(link: Transport) -> None:
    info = m.decode_controller_info(ok(link, m.get_controller_info()))
    assert info.capabilities & Capability.HEALTH
    assert info.mac == "04:E9:E5:00:00:01"
    version = m.decode_firmware_version(ok(link, m.get_firmware_version()))
    assert (version.rows, version.cols) == (2, 10)
    assert FirmwareFlag.TELEMETRY in version.flags


def test_custom_geometry() -> None:
    with Transport(FakePort(rows=4, cols=12)) as t:
        version = m.decode_firmware_version(ok(t, m.get_firmware_version()))
        assert version.panel_count == 48
        # default demo patterns match the arena, so they open fine
        ok(t, m.set_pattern_id(1))


def test_gated_command_on_older_firmware_shows_glyph() -> None:
    ctrl = FakeController(capabilities=Capability.G6_MODE)
    with Transport(FakePort(ctrl)) as t:
        reply = send(t, m.get_health())
    assert reply.status == 1 and reply.text == "Unknown command"
    assert ctrl.state is ArenaState.ERROR_DISPLAY


def test_unknown_opcode_glyph_then_all_off(
    link: Transport, ctrl: FakeController
) -> None:
    ok(link, m.all_on())
    reply = link.request(bytes([0x01, 0x71]), expect=0x71)  # reserved opcode
    assert (reply.status, reply.text) == (1, "Unknown command")
    assert ctrl.state is ArenaState.ERROR_DISPLAY
    time.sleep(0.8)
    assert ctrl.state is ArenaState.ALL_OFF


# -- display control --------------------------------------------------------


def test_all_on_all_off_texts(link: Transport, ctrl: FakeController) -> None:
    assert send(link, m.all_on()).text == "All-On Received"
    assert ctrl.state is ArenaState.ALL_ON
    assert send(link, m.stop_display()).text == "Display has been stopped"
    assert ctrl.state is ArenaState.ALL_OFF


def test_show_frame_trial(link: Transport, ctrl: FakeController) -> None:
    ok(link, m.trial_params(3, 1))
    assert ctrl.state is ArenaState.SHOW_FRAME
    ok(link, m.set_frame_position(7))
    pos = m.decode_frame_position(ok(link, m.get_frame_position()))
    assert pos == m.FramePosition(7, 16)


def test_frame_position_out_of_range_stops_display(
    link: Transport, ctrl: FakeController
) -> None:
    ok(link, m.set_pattern_id(1))
    reply = send(link, m.set_frame_position(16))
    assert reply.text == "SET_FRAME_POSITION: index out of range"
    assert ctrl.state is ArenaState.ERROR_DISPLAY


def test_frame_position_without_pattern(link: Transport) -> None:
    reply = send(link, m.set_frame_position(0))
    assert "no pattern selected" in reply.text


def test_open_loop_advances(link: Transport) -> None:
    ok(link, m.trial_params(2, 1, frame_rate=100))
    time.sleep(0.05)
    pos = m.decode_frame_position(ok(link, m.get_frame_position()))
    assert pos.index > 0


def test_trial_duration_auto_stops(link: Transport, ctrl: FakeController) -> None:
    ok(link, m.trial_params(3, 1, duration=0.05))
    assert ctrl.state is ArenaState.SHOW_FRAME
    time.sleep(0.08)
    assert ctrl.state is ArenaState.ALL_OFF


def test_trial_with_missing_pattern(link: Transport, ctrl: FakeController) -> None:
    reply = send(link, m.trial_params(2, 9))
    assert reply.text == "TRIAL_PARAMS: load failed"
    assert ctrl.state is ArenaState.ERROR_DISPLAY


def test_pattern_for_other_arena_is_rejected() -> None:
    ctrl = FakeController(patterns={"big.pat": make_test_pattern(4, 12, 2)})
    with Transport(FakePort(ctrl)) as t:
        assert send(t, m.set_pattern_id(1)).status == 1
    assert ctrl.state is ArenaState.ERROR_DISPLAY


def test_corrupt_frame_crc_is_rejected() -> None:
    data = bytearray(make_test_pattern(2, 10, 2))
    data[18 + 10] ^= 0xFF  # flip a pixel byte in frame 0
    ctrl = FakeController(patterns={"bad.pat": bytes(data)})
    with Transport(FakePort(ctrl)) as t:
        assert send(t, m.set_pattern_id(1)).status == 1


@pytest.mark.parametrize("gray", [GrayLevel.GS2, GrayLevel.GS16])
def test_stream_frame(link: Transport, ctrl: FakeController, gray: GrayLevel) -> None:
    ok(link, m.stream_frame(bytes(stream_frame_size(20, gray))))
    assert ctrl.state is ArenaState.STREAMING_FRAME


def test_stream_frame_wrong_size(link: Transport, ctrl: FakeController) -> None:
    ok(link, m.all_on())
    reply = send(link, m.stream_frame(bytes(100)))
    assert reply.text == "Bad stream-frame size"
    assert ctrl.state is ArenaState.ALL_OFF


def test_refresh_rate_defaults_and_override(link: Transport) -> None:
    assert m.decode_u16(ok(link, m.get_refresh_rate())) == 300  # 2x10 GS16
    ok(link, m.set_refresh_rate(500))
    ok(link, m.set_pattern_id(2))  # GS2 pattern would normally switch to 1000 Hz
    assert m.decode_u16(ok(link, m.get_refresh_rate())) == 500


def test_panel_display_mode_and_spi(link: Transport) -> None:
    assert ok(link, m.set_panel_display_mode(3)) == b"\x03"
    assert ok(link, m.get_panel_display_mode()) == b"\x03"
    assert m.decode_u16(ok(link, m.set_spi_clock(12))) == 12
    assert m.decode_u16(ok(link, m.get_spi_clock())) == 12


def test_frames_sent_counts_while_displaying(link: Transport) -> None:
    ok(link, m.all_on())
    time.sleep(0.05)
    assert m.decode_u32(ok(link, m.get_frames_sent())) > 0
    ok(link, m.reset_frames_sent())
    ok(link, m.all_off())
    assert m.decode_u32(ok(link, m.get_frames_sent())) < 5


# -- SD card ----------------------------------------------------------------


def test_list_and_info(link: Transport) -> None:
    assert m.decode_u16(ok(link, m.get_file_count())) == 2
    assert (
        m.decode_pattern_filename(ok(link, m.get_pattern_filename(1)))
        == "demo_gs16.pat"
    )
    info = m.decode_pattern_info(ok(link, m.get_pattern_info(2)))
    assert (info.frame_count, info.gray_level, info.rows, info.cols) == (
        2,
        GrayLevel.GS2,
        2,
        10,
    )
    assert info.duty_cycle == 0x80


def test_download_requires_all_off(link: Transport) -> None:
    ok(link, m.all_on())
    req = m.get_pattern_file(1)
    header, data = link.request_bulk_read(req.frame, expect=req.opcode)
    assert header.status == ErrorCode.DISPLAY_ACTIVE
    assert data == b""


def test_upload_rename_download_delete(link: Transport, ctrl: FakeController) -> None:
    pattern = make_test_pattern(2, 10, 3)
    ok(link, m.set_pattern_file(0, pattern))  # to pattern.temp
    new_idx = m.decode_u16(ok(link, m.set_pattern_filename(0, "aaa.pat")))
    assert new_idx == 1  # sorts before demo_*
    assert ctrl.pattern_names == ["aaa.pat", "demo_gs16.pat", "demo_gs2.pat"]
    req = m.get_pattern_file(1)
    header, data = link.request_bulk_read(req.frame, expect=req.opcode)
    assert m.decode_bulk_size(header.payload) == len(pattern)
    assert data == pattern
    ok(link, m.delete_pattern_file(1))
    assert (
        m.decode_pattern_filename(ok(link, m.get_pattern_filename(1)))
        == "demo_gs16.pat"
    )


def test_rename_conflict_gets_prefix(link: Transport, ctrl: FakeController) -> None:
    ok(link, m.set_pattern_file(0, make_test_pattern()))
    ok(link, m.set_pattern_filename(0, "demo_gs2.pat"))
    assert "X001_demo_gs2.pat" in ctrl.pattern_names


def test_upload_while_displaying_is_drained_and_refused(link: Transport) -> None:
    ok(link, m.all_on())
    reply = send(link, m.set_pattern_file(1, bytes(5000)))
    assert reply.status == ErrorCode.DISPLAY_ACTIVE
    # The link is still in sync afterwards.
    assert send(link, m.get_file_count()).ok


def test_delete_errors(link: Transport) -> None:
    assert send(link, m.delete_pattern_file(0)).status == ErrorCode.BAD_PARAM
    assert send(link, m.delete_pattern_file(9)).status == ErrorCode.BAD_PARAM


def test_no_sd_card() -> None:
    ctrl = FakeController(sd_card=False)
    with Transport(FakePort(ctrl)) as t:
        assert m.decode_u16(ok(t, m.get_file_count())) == 0
        reply = send(t, m.get_sd_info())
        assert reply.status == 1 and not m.decode_sd_info(reply.payload).mounted
        assert send(t, m.purge_memory()).status == ErrorCode.SD_NOT_PRESENT


def test_purge_and_archive(link: Transport, ctrl: FakeController) -> None:
    req = m.get_sd_archive()
    header, data = link.request_bulk_read(req.frame, expect=req.opcode)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert names == ["patterns/demo_gs16.pat", "patterns/demo_gs2.pat"]
    ok(link, m.purge_memory())
    assert ctrl.pattern_names == []


def test_sd_info_and_diag(link: Transport) -> None:
    assert m.decode_sd_diag(ok(link, m.set_sd_diag(no_same_index_skip=True))) == (
        False,
        True,
    )
    info = m.decode_sd_info(ok(link, m.get_sd_info()))
    assert info.mounted and info.no_same_index_skip


# -- analog / digital I/O ---------------------------------------------------


def test_analog_output_quantized_readback(link: Transport) -> None:
    ok(link, m.set_ao_voltage(1234))
    assert m.decode_u16(ok(link, m.get_ao_voltage())) == 1233  # 12-bit DAC


def test_ao_frame_number_mode_refuses_voltage(link: Transport) -> None:
    ok(link, m.set_ao_mode(AoMode.FRAME_NUMBER))
    reply = send(link, m.set_ao_voltage(100))
    assert "frame_number mode" in reply.text
    ok(link, m.set_pattern_id(1))
    ok(link, m.set_frame_position(15))
    assert m.decode_u16(ok(link, m.get_ao_voltage())) >= 4998


def test_ao_lut(link: Transport) -> None:
    assert m.decode_u16(ok(link, m.set_ao_lut([0, 2500, 5000]))) == 3


def test_analog_inputs(link: Transport, ctrl: FakeController) -> None:
    ctrl.analog_in = (-5000, 2500)
    assert m.decode_analog_in(ok(link, m.get_analog_in())) == m.AnalogInputs(
        -5000, 2500
    )


def test_analog_loopback(link: Transport, ctrl: FakeController) -> None:
    ctrl.analog_loopback = (2.0, -100.0)
    ok(link, m.set_ao_voltage(1000))
    ain1 = m.decode_analog_in(ok(link, m.get_analog_in())).ain1_mv
    assert ain1 == round(2.0 * (1000 * 4095 // 5000) * 5000 / 4095 - 100)


def test_dio_roles_and_outputs(link: Transport, ctrl: FakeController) -> None:
    p1, p2 = m.decode_dio_role(ok(link, m.get_dio_role()))
    assert p1.role is DioRole.OUT_PROGRAMMABLE and p2.role is DioRole.IN_TRIGGER
    ok(link, m.set_digital_out(1, True))
    reply = send(link, m.set_digital_out(2, True))
    assert "in_trigger" in reply.text
    ctrl.dio_inputs[1] = True
    assert m.decode_digital_out(ok(link, m.get_digital_out())) == (True, True)
    ok(link, m.set_dio_role(2, DioRole.OFF))
    ok(link, m.set_digital_out(2, False))  # "off" auto-promotes
    _, p2 = m.decode_dio_role(ok(link, m.get_dio_role()))
    assert p2.role is DioRole.OUT_PROGRAMMABLE


# -- health, telemetry, diagnostics -----------------------------------------


def test_health(link: Transport) -> None:
    ok(link, m.set_pattern_id(1))
    ok(link, m.set_frame_position(3))
    health = m.decode_health(ok(link, m.get_health()))
    assert health.state is ArenaState.SHOW_FRAME
    assert health.current_frame == 3
    assert health.frame_position_commands == 1
    assert health.sd_mounted and health.pattern_open and health.display_active


def test_crash_report_empty(link: Transport) -> None:
    assert not m.decode_crash_report(ok(link, m.get_crash_report())).present


def test_telemetry_drain_and_ack(link: Transport) -> None:
    ok(link, m.all_on())
    ok(link, m.all_off())
    records = []
    ack = None
    while True:
        block = decode_telemetry_block(ok(link, m.get_telemetry_block(ack)))
        records += block.records
        ack = block.last_seq if block.records else ack
        if not block.more:
            break
    cmds = [r.opcode for r in records if isinstance(r, CommandRecord)]
    assert cmds[-2:] == [Opcode.ALL_ON, Opcode.ALL_OFF]
    assert any(
        isinstance(r, StateRecord) and r.new_state is ArenaState.ALL_ON for r in records
    )
    # After acknowledging, the next block starts after the last record seen.
    block = decode_telemetry_block(ok(link, m.get_telemetry_block(ack)))
    assert all(r.seq > (ack or 0) for r in block.records)


def test_telemetry_block_respects_budget(link: Transport) -> None:
    for _ in range(20):
        ok(link, m.get_refresh_rate())
    block = decode_telemetry_block(ok(link, m.get_telemetry_block(max_bytes=40)))
    assert block.more
    assert (
        sum(len(r.params) + 13 for r in block.records if isinstance(r, CommandRecord))
        <= 40
    )


def test_diag_output_lines_on_debug_build(caplog: pytest.LogCaptureFixture) -> None:
    ctrl = FakeController(firmware_flags=FirmwareFlag.DEBUG_BUILD)
    with Transport(FakePort(ctrl)) as t:
        assert send(t, m.set_diag_output(True)).text == "diag on"
        with caplog.at_level(logging.INFO, logger="arena_interface.firmware"):
            ok(t, m.get_refresh_rate())
    assert "[cmd] reply status=0" in caplog.text


# -- panel firmware ---------------------------------------------------------


def test_firmware_upload_and_isp(link: Transport) -> None:
    image = bytes(range(256)) * 10 + b"G6PANEL\0" + b"v9".ljust(16, b"\0") + bytes(8)
    assert m.decode_u32(ok(link, m.set_firmware_file(image))) == crc32(image)
    info = m.decode_firmware_info(ok(link, m.get_firmware_info()))
    assert (info.magic, info.version) == ("G6PANEL", "v9")
    assert ok(link, m.program_panel(3))
    assert send(link, m.verify_panel(21)).status == 1  # no 21st panel on 2x10


def test_isp_requires_all_off(link: Transport) -> None:
    ok(link, m.all_on())
    assert send(link, m.program_panel(1)).status == ErrorCode.DISPLAY_ACTIVE


# -- faults -----------------------------------------------------------------


def test_unplug_raises_disconnected(link: Transport, ctrl: FakeController) -> None:
    ctrl.unplug()
    with pytest.raises(DeviceDisconnected):
        send(link, m.get_file_count())


def test_reconnect_keeps_controller_state(ctrl: FakeController) -> None:
    with Transport(FakePort(ctrl)) as t:
        ok(t, m.set_refresh_rate(777))
    ctrl.unplug()
    with Transport(FakePort(ctrl)) as t:
        assert m.decode_u16(ok(t, m.get_refresh_rate())) == 777


def test_system_reset_drops_link_and_resets_state(ctrl: FakeController) -> None:
    with Transport(FakePort(ctrl)) as t:
        ok(t, m.set_refresh_rate(777))
        assert send(t, m.system_reset()).text == "rebooting"
        with pytest.raises(DeviceDisconnected):
            send(t, m.get_file_count())
    with Transport(FakePort(ctrl)) as t:
        assert m.decode_u16(ok(t, m.get_refresh_rate())) == 300
        health = m.decode_health(ok(t, m.get_health()))
        assert health.breadcrumb_valid
        assert health.previous_op is m.HealthOp.COMMAND


def test_muted_controller_times_out(link: Transport, ctrl: FakeController) -> None:
    ctrl.muted = True
    with pytest.raises(DeviceTimeout):
        link.request(m.all_off().frame, expect=0x00, timeout=0.05)


def test_response_delay(link: Transport, ctrl: FakeController) -> None:
    ctrl.response_delay = 0.1
    with pytest.raises(DeviceTimeout):
        link.request(m.all_off().frame, expect=0x00, timeout=0.03)
    ctrl.response_delay = 0.02
    assert send(link, m.get_file_count()).ok


def test_injected_noise_is_skipped(link: Transport, ctrl: FakeController) -> None:
    ctrl.inject(bytes([0xC8]))  # an impossible length byte
    assert send(link, m.get_file_count()).ok


def test_garbage_from_host_resyncs(link: Transport, ctrl: FakeController) -> None:
    with pytest.raises(DeviceTimeout):
        link.request(bytes([0x55, 0x01, 0x02]), expect=0x55, timeout=0.05)
    assert send(link, m.get_file_count()).ok


def test_received_log(link: Transport, ctrl: FakeController) -> None:
    send(link, m.all_off())
    assert ctrl.received[-1] == bytes([0x01, 0x00])


def test_make_test_pattern_layout() -> None:
    data = make_test_pattern(2, 10, 3, GrayLevel.GS2)
    assert data[:4] == b"G6PT"
    assert len(data) == 18 + 3 * (4 + 20 * 53 + 2)
    assert struct.unpack_from("<H", data, 6)[0] == 3
