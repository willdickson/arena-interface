"""High-level Device API, against the simulated controller."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from arena_interface import (
    AoMode,
    ArenaState,
    Capability,
    CommandError,
    Device,
    DeviceDisconnected,
    DeviceError,
    DeviceNotFound,
    DioRole,
    ErrorCode,
    FirmwareFlag,
    PanelDisplayMode,
    Pattern,
    PortInfo,
    ProtocolError,
    TrialMode,
    UnsupportedCommand,
)
from arena_interface import device as device_module
from arena_interface.protocol import GrayLevel, stream_frame_size
from arena_interface.testing import FakeController, FakePort, make_test_pattern
from arena_interface.transport import Transport


@pytest.fixture
def ctrl() -> FakeController:
    return FakeController()


@pytest.fixture
def dev(ctrl: FakeController) -> Iterator[Device]:
    dev = Device.open(simulate=ctrl)
    yield dev
    dev.close()


# -- connecting -------------------------------------------------------------


def test_open_reads_identity(dev: Device) -> None:
    assert dev.is_open
    assert dev.port_name == "fake://arena"
    assert dev.arena_shape == (2, 10)
    assert dev.panel_count == 20
    assert dev.info.mac == "04:E9:E5:00:00:01"
    assert dev.firmware_version == "fake0001 2x10 2026-01-01 simulated"
    assert dev.supports(Capability.IO_EXT)
    assert dev.supports(FirmwareFlag.TELEMETRY)
    assert dev.simulator is not None


def test_context_manager_sends_all_off(ctrl: FakeController) -> None:
    with Device.open(simulate=ctrl) as dev:
        dev.all_on()
    assert ctrl.state is ArenaState.ALL_OFF
    assert ctrl.received[-1] == bytes([0x01, 0x00])
    assert not dev.is_open


def test_all_off_on_close_can_be_disabled(ctrl: FakeController) -> None:
    with Device.open(simulate=ctrl, all_off_on_close=False) as dev:
        dev.all_on()
    assert ctrl.state is ArenaState.ALL_ON


def test_close_after_unplug_does_not_raise(dev: Device, ctrl: FakeController) -> None:
    ctrl.unplug()
    dev.close()
    dev.close()
    assert not dev.is_open


def test_old_firmware_without_build_info() -> None:
    ctrl = FakeController(capabilities=Capability.G6_MODE)
    with Device.open(simulate=ctrl) as dev:
        assert dev.firmware is None
        assert dev.arena_shape is None
        assert dev.firmware_version.startswith("unknown")
    with Device.open(simulate=ctrl, arena_shape=(2, 10)) as dev:
        assert dev.panel_count == 20


def test_non_g6_device_is_rejected() -> None:
    with pytest.raises(ProtocolError):
        Device.open(simulate=FakeController(capabilities=Capability(0)))


# -- auto-detect ------------------------------------------------------------


def _port(name: str) -> PortInfo:
    return PortInfo(name, "Teensyduino Serial", 0x16C0, 0x0483, None)


@pytest.fixture
def fake_ports(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeController | None]:
    """Map port names to controllers (None = a silent non-controller Teensy)."""
    ports: dict[str, FakeController | None] = {}

    def fake_find_ports() -> list[PortInfo]:
        return [_port(n) for n in sorted(ports)]

    def fake_open(cls: type[Transport], name: str, **_: object) -> Transport:
        if name not in ports:
            raise DeviceNotFound(f"cannot open {name}")
        target = ports[name]
        if target is None:
            target = FakeController()
            target.muted = True
        return Transport(FakePort(target), name=name)

    monkeypatch.setattr(device_module, "find_ports", fake_find_ports)
    monkeypatch.setattr(Transport, "open", classmethod(fake_open))
    return ports


def test_detect_none(fake_ports: dict[str, FakeController | None]) -> None:
    with pytest.raises(DeviceNotFound, match="no G6 arena controller"):
        Device.open()


def test_detect_single(fake_ports: dict[str, FakeController | None]) -> None:
    fake_ports["/dev/ttyACM0"] = FakeController()
    with Device.open() as dev:
        assert dev.port_name == "/dev/ttyACM0"


def test_detect_probes_to_find_controller(
    fake_ports: dict[str, FakeController | None],
) -> None:
    fake_ports["/dev/ttyACM0"] = None  # some other Teensy
    fake_ports["/dev/ttyACM1"] = FakeController()
    with Device.open() as dev:
        assert dev.port_name == "/dev/ttyACM1"


def test_detect_without_probe_is_ambiguous(
    fake_ports: dict[str, FakeController | None],
) -> None:
    fake_ports["/dev/ttyACM0"] = None
    fake_ports["/dev/ttyACM1"] = FakeController()
    with pytest.raises(DeviceNotFound, match="several candidate ports"):
        Device.open(probe=False)


def test_detect_two_controllers(fake_ports: dict[str, FakeController | None]) -> None:
    fake_ports["/dev/ttyACM0"] = FakeController()
    fake_ports["/dev/ttyACM1"] = FakeController()
    with pytest.raises(DeviceNotFound, match="several controllers"):
        Device.open()


def test_explicit_port_skips_detection(
    fake_ports: dict[str, FakeController | None],
) -> None:
    fake_ports["/dev/ttyACM0"] = None  # would be probed if detection ran
    fake_ports["/dev/ttyACM1"] = FakeController()
    with Device.open("/dev/ttyACM1") as dev:
        assert dev.port_name == "/dev/ttyACM1"


# -- gating -----------------------------------------------------------------


def test_unsupported_command_is_not_sent() -> None:
    ctrl = FakeController(
        capabilities=Capability.G6_MODE | Capability.HEALTH,
        firmware_flags=FirmwareFlag(0),
    )
    with Device.open(simulate=ctrl) as dev:
        sent = len(ctrl.received)
        with pytest.raises(UnsupportedCommand, match="TELEMETRY"):
            dev.telemetry.read_block()
        with pytest.raises(UnsupportedCommand, match="IO_EXT"):
            dev.io.read_analog_in()
        assert len(ctrl.received) == sent
        assert ctrl.state is ArenaState.ALL_OFF  # no error glyph


# -- display ----------------------------------------------------------------


def test_trial_and_frames(dev: Device, ctrl: FakeController) -> None:
    dev.start_trial(1, TrialMode.SHOW_FRAME)
    dev.show_frame(5)
    assert dev.frame_position.index == 5
    with pytest.raises(ValueError, match="0..15"):
        dev.show_frame(16)
    assert ctrl.state is ArenaState.SHOW_FRAME  # nothing sent, display still running


def test_show_pattern(dev: Device, ctrl: FakeController) -> None:
    dev.show_pattern(2)
    assert dev.frame_position.frame_count == 2
    assert ctrl.state is ArenaState.SHOW_FRAME


def test_start_trial_failure_raises(dev: Device) -> None:
    with pytest.raises(CommandError, match="load failed"):
        dev.start_trial(9)


def test_stream_frame_size_checked(dev: Device, ctrl: FakeController) -> None:
    dev.stream_frame(bytes(stream_frame_size(20, GrayLevel.GS2)))
    assert ctrl.state is ArenaState.STREAMING_FRAME
    with pytest.raises(ValueError, match="1064, 4064"):
        dev.stream_frame(bytes(100))


def test_settings(dev: Device) -> None:
    dev.refresh_rate = 450
    assert dev.refresh_rate == 450
    dev.panel_display_mode = PanelDisplayMode.ONESHOT
    assert dev.panel_display_mode is PanelDisplayMode.ONESHOT
    dev.all_on()
    time.sleep(0.02)
    assert dev.frames_sent > 0
    dev.reset_frames_sent()
    dev.stop()


def test_panel_memory_playback(dev: Device, ctrl: FakeController) -> None:
    dev.play_panel_frames(0, 10, 30)
    assert ctrl.state is ArenaState.PSRAM_PLAY
    dev.show_panel_frame(3)


# -- reset / reconnect / disconnect -----------------------------------------


def test_reset_and_reconnect(dev: Device, ctrl: FakeController) -> None:
    dev.refresh_rate = 777
    dev.reset()
    assert not dev.is_open
    with pytest.raises(DeviceDisconnected):
        dev.all_on()
    dev.reconnect(timeout=1)
    assert dev.is_open
    assert dev.refresh_rate == 300  # controller rebooted


def test_reconnect_after_unplug(dev: Device, ctrl: FakeController) -> None:
    ctrl.unplug()
    with pytest.raises(DeviceDisconnected):
        dev.all_off()
    dev.reconnect(timeout=1)
    dev.all_off()


def test_reconnect_rejects_other_controller(dev: Device) -> None:
    other = FakeController(mac=bytes(6))
    dev._simulator = other  # the "port" now leads to a different unit
    with pytest.raises(DeviceNotFound, match="different controller"):
        dev.reconnect(timeout=0.5)


# -- patterns ---------------------------------------------------------------


def test_pattern_listing(dev: Device) -> None:
    assert len(dev.patterns) == 2
    assert [e.name for e in dev.patterns] == ["demo_gs16.pat", "demo_gs2.pat"]
    assert dev.patterns.find("demo_gs2.pat") == 2
    assert dev.patterns.find("missing.pat") is None
    assert dev.patterns.info(1).frame_count == 16


def test_pattern_upload_download_delete(dev: Device) -> None:
    data = make_test_pattern(2, 10, 3)
    new_id = dev.patterns.upload("b_stripes.pat", data)
    assert dev.patterns.name(new_id) == "b_stripes.pat"
    assert dev.patterns.download(new_id) == data
    with pytest.raises(FileExistsError):
        dev.patterns.upload("b_stripes.pat", data)
    replacement = make_test_pattern(2, 10, 5)
    assert dev.patterns.upload("b_stripes.pat", replacement, overwrite=True) == new_id
    assert dev.patterns.info(new_id).frame_count == 5
    dev.patterns.delete(new_id)
    assert dev.patterns.find("b_stripes.pat") is None


def test_pattern_upload_checks_arena_size(dev: Device) -> None:
    with pytest.raises(ValueError, match="4x12"):
        dev.patterns.upload("big.pat", make_test_pattern(4, 12, 1))
    with pytest.raises(ValueError, match=".pat"):
        dev.patterns.upload("notes.txt", make_test_pattern())


def test_download_while_displaying(dev: Device) -> None:
    dev.all_on()
    with pytest.raises(CommandError) as excinfo:
        dev.patterns.download(1)
    assert excinfo.value.status == ErrorCode.DISPLAY_ACTIVE


def test_rename_archive_format(dev: Device) -> None:
    new_id = dev.patterns.rename(2, "a_first.pat")
    assert new_id == 1
    assert dev.patterns.archive()[:2] == b"PK"
    dev.patterns.format_card()
    assert len(dev.patterns) == 0


# -- I/O --------------------------------------------------------------------


def test_io(dev: Device, ctrl: FakeController) -> None:
    dev.io.analog_out = 2500
    assert abs(dev.io.analog_out - 2500) <= 2
    dev.io.load_analog_table([0, 5000])
    dev.io.set_analog_out_mode(AoMode.FRAME_NUMBER)
    ctrl.analog_in = (100, -200)
    assert dev.io.read_analog_in().ain2_mv == -200
    dev.io.set_digital_out(1, True)
    assert dev.io.read_digital()[0] is True
    dev.io.set_dio_role(2, DioRole.OUT_PROGRAMMABLE)
    assert dev.io.dio_roles()[1].role is DioRole.OUT_PROGRAMMABLE


# -- diagnostics ------------------------------------------------------------


def test_diagnostics(dev: Device) -> None:
    assert dev.diagnostics.health().state is ArenaState.ALL_OFF
    assert not dev.diagnostics.crash_report().present
    assert dev.diagnostics.sd_info().mounted
    assert dev.diagnostics.set_sd_diag(legacy_seek=True) == (True, False)
    dev.diagnostics.debug_output = True
    assert dev.diagnostics.debug_output
    dev.diagnostics.spi_clock_mhz = 12
    assert dev.diagnostics.spi_clock_mhz == 12
    assert dev.diagnostics.ip_address() == ""


def test_sd_info_without_card() -> None:
    with Device.open(simulate=FakeController(sd_card=False)) as dev:
        assert not dev.diagnostics.sd_info().mounted


# -- panel firmware ---------------------------------------------------------


def test_panel_firmware(dev: Device) -> None:
    assert dev.panel_firmware.info() is None
    image = bytes(4096) + b"G6PANEL\0" + b"v2".ljust(16, b"\0") + bytes(8)
    dev.panel_firmware.upload(image)
    assert dev.panel_firmware.info().version == "v2"
    assert "OK" in dev.panel_firmware.program(1)
    assert dev.panel_firmware.verify(1).matches
    assert not dev.panel_firmware.verify(20 + 1).matches


# -- threads ----------------------------------------------------------------


def test_device_shared_between_threads(dev: Device) -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(20):
                assert dev.refresh_rate > 0
                assert len(dev.patterns) == 2
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# -- dev.arena ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "cols", "name", "arena_id"),
    [(2, 10, "G6_2x10", 1), (4, 10, "G6_4x10", 6), (4, 12, "", 0)],
)
def test_arena_property(rows: int, cols: int, name: str, arena_id: int) -> None:
    with Device.open(simulate=FakeController(rows=rows, cols=cols)) as dev:
        assert dev.arena.name == name
        assert dev.arena.arena_id == arena_id
        assert dev.arena.pixel_shape == (rows * 20, cols * 20)
        assert not dev.arena.is_partial


def test_arena_id_reaches_pattern_header() -> None:
    with Device.open(simulate=True) as dev:
        h, w = dev.arena.pixel_shape
        p = Pattern.from_frames([[[0] * w for _ in range(h)]], arena=dev.arena)
        pid = dev.patterns.upload("ided.pat", p)
        assert dev.patterns.info(pid).arena_id == 1


def test_arena_property_on_old_firmware() -> None:
    ctrl = FakeController(capabilities=Capability.G6_MODE)
    with (
        Device.open(simulate=ctrl) as dev,
        pytest.raises(DeviceError, match="arena_shape"),
    ):
        dev.arena  # noqa: B018
    with Device.open(simulate=ctrl, arena_shape=(2, 10)) as dev:
        assert dev.arena.name == "G6_2x10"
