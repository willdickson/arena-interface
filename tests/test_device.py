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
    DeviceTimeout,
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


@pytest.mark.parametrize(
    "name", ["demo_gs2.pat", "demo_gs2", "DEMO_GS2.PAT", "Demo_GS2", " demo_gs2 "]
)
def test_pattern_find_is_forgiving(dev: Device, name: str) -> None:
    assert dev.patterns.find(name) == 2


def test_patterns_selected_by_name_or_id(dev: Device) -> None:
    # demo_gs16.pat (id 1) has 16 frames, demo_gs2.pat (id 2) has 2.
    dev.start_trial("demo_gs2", frame_rate=10)
    assert dev.frame_position.frame_count == 2
    dev.start_trial(1, frame_rate=10)
    assert dev.frame_position.frame_count == 16
    dev.show_pattern("Demo_GS2.pat")
    assert dev.frame_position.frame_count == 2
    report = dev.play_frames([0, 15, 3], fps=200, pattern="demo_gs16")
    assert report.sent == 3
    assert dev.frame_position.frame_count == 16


def test_unknown_pattern_name_lists_the_card(dev: Device) -> None:
    with pytest.raises(KeyError, match="demo_gs16.pat, demo_gs2.pat"):
        dev.start_trial("grating", frame_rate=10)
    with pytest.raises(KeyError, match="no pattern named 'missing'"):
        dev.start_frames(range(2), fps=100, pattern="missing")


def test_library_methods_take_names(dev: Device) -> None:
    assert dev.patterns.name("DEMO_GS2") == "demo_gs2.pat"
    assert dev.patterns.info("demo_gs16").frame_count == 16
    assert dev.patterns.download("demo_gs2.pat") == dev.patterns.download(2)
    new_id = dev.patterns.rename("demo_gs2", "a_renamed.pat")
    assert (new_id, dev.patterns.name(new_id)) == (1, "a_renamed.pat")
    dev.patterns.delete("A_Renamed")
    assert [e.name for e in dev.patterns] == ["demo_gs16.pat"]
    for call in (
        lambda: dev.patterns.delete("missing"),
        lambda: dev.patterns.rename("missing", "x.pat"),
        lambda: dev.patterns.info("missing"),
    ):
        with pytest.raises(KeyError, match="patterns: demo_gs16.pat"):
            call()
    assert len(dev.patterns) == 1  # nothing was deleted or renamed


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


# -- pattern-name cache -------------------------------------------------------


@pytest.fixture
def requests(dev: Device, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the requests the device sends (one list entry per request)."""
    sent: list[int] = []
    original = dev._send

    def counting(req, **kwargs):  # type: ignore[no-untyped-def]
        sent.append(1)
        return original(req, **kwargs)

    monkeypatch.setattr(dev, "_send", counting)
    return sent


def test_cache_off_reads_the_card_for_each_lookup(
    dev: Device, requests: list[int]
) -> None:
    assert not dev.patterns.cache
    assert dev.patterns.find("demo_gs2") == 2
    assert len(requests) == 3  # count + two names


def test_cache_on_makes_lookups_one_request(dev: Device, requests: list[int]) -> None:
    dev.patterns.cache = True  # reads the names now
    assert len(requests) == 3
    requests.clear()
    assert dev.patterns.find("DEMO_GS2") == 2
    assert len(requests) == 1  # just the check of the cached id
    dev.patterns.cache = False
    assert not dev.patterns.cache


def test_cache_follows_library_changes(dev: Device, requests: list[int]) -> None:
    dev.patterns.cache = True
    dev.patterns.upload("a_first.pat", make_test_pattern(2, 10, 3))  # ids shift
    requests.clear()
    assert dev.patterns.find("demo_gs2") == 3
    assert dev.patterns.find("a_first") == 1
    assert len(requests) == 2
    dev.patterns.rename(1, "z_last.pat")
    assert dev.patterns.find("z_last") == 3
    dev.patterns.delete(3)
    assert dev.patterns.find("z_last") is None
    dev.patterns.format_card()
    assert dev.patterns.find("demo_gs2") is None


def test_cache_survives_changes_made_elsewhere(
    dev: Device, ctrl: FakeController
) -> None:
    dev.patterns.cache = True
    # Changed behind the library's back: ids of both demo patterns shift.
    ctrl.files["a_new.pat"] = make_test_pattern(2, 10, 3)
    assert dev.patterns.find("demo_gs2") == 3  # check fails, names re-read
    ctrl.files["b_other.pat"] = make_test_pattern(2, 10, 4)
    assert dev.patterns.find("b_other") == 2  # not cached: re-read
    del ctrl.files["demo_gs2.pat"]
    del ctrl.files["demo_gs16.pat"]
    assert dev.patterns.find("b_other") == 2
    dev.start_trial("a_new", frame_rate=10)
    assert dev.frame_position.frame_count == 3


def test_cache_from_open_and_after_reconnect(ctrl: FakeController) -> None:
    with Device.open(simulate=ctrl, cache_patterns=True) as dev:
        assert dev.patterns.cache
        dev.reset()
        ctrl.files["a_new.pat"] = make_test_pattern(2, 10, 3)
        dev.reconnect(timeout=2)
        assert dev.patterns._names == ["a_new.pat", "demo_gs16.pat", "demo_gs2.pat"]


# -- stray input held by the controller --------------------------------------


@pytest.fixture
def quick_resync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorter waits: the simulator answers at once."""
    from arena_interface import messages

    monkeypatch.setattr(messages, "DEFAULT_TIMEOUT", 0.1)
    monkeypatch.setattr(device_module, "RESYNC_TIMEOUT", 0.01)


@pytest.mark.parametrize(
    "junk",
    [
        b" G6 arena controller fw 1",  # echoed start-up text (seen on hardware)
        b"\x31x",  # the longest binary command: 48 more bytes needed
        b"2\x10\x00",  # a stream-frame header waiting for 16 bytes
        b"+GCAP\r",  # a modem probe
    ],
)
@pytest.mark.usefixtures("quick_resync")
def test_open_clears_stray_input_held_by_the_controller(
    ctrl: FakeController, junk: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    ctrl.feed(junk)  # received before we connect; the controller waits for more
    caplog.set_level("INFO", logger="arena_interface.device")
    with Device.open(simulate=ctrl) as dev:
        assert Capability.G6_MODE in dev.capabilities
        assert dev.patterns.find("demo_gs2") == 2  # later exchanges are in step
    assert "extra request" in caplog.text


def test_open_without_stray_input_needs_no_retries(
    ctrl: FakeController, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="arena_interface.device")
    with Device.open(simulate=ctrl):
        pass
    assert "extra request" not in caplog.text


@pytest.mark.usefixtures("quick_resync")
def test_open_gives_up_on_a_silent_controller(ctrl: FakeController) -> None:
    ctrl.muted = True
    with pytest.raises(DeviceTimeout, match="27 tries"):
        Device.open(simulate=ctrl)
