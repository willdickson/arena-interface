"""Host-paced playback against the simulated controller.

Timing assertions are deliberately loose so the tests stay reliable on busy
machines; the hardware suite checks real timing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

import pytest

from arena_interface import (
    Arena,
    Device,
    DeviceDisconnected,
    GrayLevel,
    Pattern,
    PerformanceWarning,
)
from arena_interface.protocol import Opcode
from arena_interface.testing import FakeController


@pytest.fixture
def ctrl() -> FakeController:
    return FakeController()


@pytest.fixture
def dev(ctrl: FakeController) -> Iterator[Device]:
    device = Device.open(simulate=ctrl)
    yield device
    device.close()


def _positions(ctrl: FakeController) -> list[int]:
    """Frame indices sent with SET_FRAME_POSITION, in order."""
    return [
        m[2] | (m[3] << 8)
        for m in ctrl.received
        if m[:2] == bytes([3, Opcode.SET_FRAME_POSITION])
    ]


def _blank(h: int = 40, w: int = 200) -> list[list[int]]:
    return [[0] * w for _ in range(h)]


# -- play_frames ------------------------------------------------------------


def test_play_frames_sends_every_frame_on_schedule(
    dev: Device, ctrl: FakeController
) -> None:
    frames = [0, 5, 2, 15, 1, 1, 3, 4]
    report = dev.play_frames(frames, fps=100, pattern_id=1)
    assert _positions(ctrl) == frames
    assert (report.planned, report.sent, report.skipped) == (8, 8, 0)
    assert report.on_schedule
    assert 0.07 - 0.005 <= report.duration_s < 0.5  # 7 periods of 10 ms
    assert report.refresh_rate == 300
    assert "8/8 frames at 100/s" in str(report)


def test_play_frames_uses_loaded_pattern(dev: Device, ctrl: FakeController) -> None:
    dev.show_pattern(2)  # 2 frames
    assert dev.play_frames([1, 0, 1], fps=200).sent == 3


def test_play_frames_validates_before_sending(
    dev: Device, ctrl: FakeController
) -> None:
    with pytest.raises(ValueError, match="no pattern is loaded"):
        dev.play_frames([0], fps=10)
    dev.show_pattern(1)
    sent_before = len(ctrl.received)
    with pytest.raises(ValueError, match=r"0\.\.15"):
        dev.play_frames([0, 16], fps=10)
    with pytest.raises(ValueError, match="positive"):
        dev.play_frames([0], fps=0)
    with pytest.raises(ValueError, match="late"):
        dev.play_frames([0], fps=10, late="drop")  # type: ignore[arg-type]
    assert len(ctrl.received) == sent_before


def test_verify_counts_displayed_frames(dev: Device) -> None:
    report = dev.play_frames(range(16), fps=200, pattern_id=1, verify=True)
    # Loading the pattern already shows frame 0, so 15 changes remain.
    assert report.changes == 15
    assert report.displayed == 15
    assert report.superseded == 0
    report = dev.play_frames([3, 3, 4, 4, 3], fps=200, verify=True)
    assert report.changes == report.displayed == 3  # 15->3, 3->4, 4->3


def test_skip_keeps_schedule_when_device_is_slow(
    dev: Device, ctrl: FakeController
) -> None:
    dev.show_pattern(1)
    ctrl.response_delay = 0.012  # each command takes > 2 frame periods
    report = dev.play_frames(list(range(16)) * 2, fps=200, late="skip")
    assert report.skipped > 0
    assert report.sent + report.skipped == 32
    assert report.duration_s < 32 * 0.012  # did not wait for every frame
    assert not report.on_schedule


def test_send_delivers_every_frame_late(dev: Device, ctrl: FakeController) -> None:
    dev.show_pattern(1)
    ctrl.response_delay = 0.012
    report = dev.play_frames(range(16), fps=200, late="send")
    assert (report.sent, report.skipped) == (16, 0)
    assert report.late > 0
    assert report.max_late_s > 0.005
    assert _positions(ctrl)[-16:] == list(range(16))


def test_falling_behind_is_logged(
    dev: Device, ctrl: FakeController, caplog: pytest.LogCaptureFixture
) -> None:
    dev.show_pattern(1)
    ctrl.response_delay = 0.012
    with caplog.at_level(logging.WARNING, logger="arena_interface.playback"):
        dev.play_frames(range(16), fps=200)
    assert "falling behind" in caplog.text
    assert "skipped" in caplog.text and "late" in caplog.text
    assert caplog.text.count("falling behind") <= 2  # rate limited, not per frame


# -- refresh rate checks ----------------------------------------------------


def test_fps_above_refresh_rate_warns(dev: Device) -> None:
    with pytest.warns(PerformanceWarning, match="300 Hz"):
        dev.play_frames(range(4), fps=290, pattern_id=1)


def test_refresh_rate_argument(dev: Device, ctrl: FakeController) -> None:
    dev.show_pattern(1)
    report = dev.play_frames(range(4), fps=450, refresh_rate=600)
    assert report.refresh_rate == 600
    assert ctrl._refresh_hz == 600


def test_refresh_rate_above_panel_scan_warns(dev: Device) -> None:
    with pytest.warns(PerformanceWarning, match="1000 Hz scan"):
        dev.refresh_rate = 1500


def test_gs16_refresh_near_spi_limit_warns() -> None:
    with Device.open(simulate=FakeController(rows=4, cols=10)) as dev:
        dev.show_pattern(1)  # 40 panels: SPI saturates near 640 Hz for GS16
        with pytest.warns(PerformanceWarning, match="saturate"):
            dev.play_frames(range(2), fps=100, refresh_rate=600)


# -- play_images ------------------------------------------------------------


def test_play_images(dev: Device, ctrl: FakeController) -> None:
    images = [
        [[(x // 20 + f) % 2 for x in range(200)] for _ in range(40)] for f in range(6)
    ]
    report = dev.play_images(images, fps=100, gray_level=GrayLevel.GS2)
    assert report.sent == 6
    assert report.refresh_rate == 1000  # the GS2 default on 2x10
    streams = [m for m in ctrl.received if m[0] == Opcode.STREAM_FRAME]
    assert len(streams) == 7  # one preparation frame + 6 frames


def test_play_images_fits_rig_to_controller_grid() -> None:
    ctrl = FakeController(rows=4, cols=10)
    rig = Arena.preset("G6_3x10")
    pattern = Pattern.from_frames(
        [_blank(*rig.pixel_shape) for _ in range(3)], arena=rig
    )
    with Device.open(simulate=ctrl) as dev:
        assert dev.play_images(pattern, fps=100).sent == 3


# -- background playback -----------------------------------------------------


def test_start_frames_runs_in_background(dev: Device) -> None:
    player = dev.start_frames(list(range(16)) * 3, fps=100, pattern_id=1)
    assert player.running
    assert player.total == 48
    time.sleep(0.1)
    assert 0 < player.progress < 48
    assert dev.refresh_rate == 300  # the device stays usable meanwhile
    report = player.wait(timeout=5)
    assert not player.running
    assert report.sent == 48
    assert player.report == report


def test_stop_ends_playback_early(dev: Device) -> None:
    player = dev.start_frames(list(range(16)) * 10, fps=100, pattern_id=1)
    time.sleep(0.05)
    report = player.stop()
    assert report.stopped
    assert report.sent < 160


def test_context_manager_stops_playback(dev: Device) -> None:
    with dev.start_images([_blank()] * 100, fps=100) as player:
        time.sleep(0.03)
    assert not player.running


def test_background_error_is_raised_by_wait(dev: Device, ctrl: FakeController) -> None:
    player = dev.start_frames(list(range(16)) * 10, fps=100, pattern_id=1)
    time.sleep(0.03)
    ctrl.unplug()
    with pytest.raises(DeviceDisconnected):
        player.wait(timeout=5)


def test_wait_timeout(dev: Device) -> None:
    player = dev.start_frames(list(range(16)) * 10, fps=100, pattern_id=1)
    with pytest.raises(TimeoutError):
        player.wait(timeout=0.01)
    player.stop()


# -- pace ---------------------------------------------------------------------


def test_pace_count(dev: Device) -> None:
    dev.show_pattern(1)
    pacer = dev.pace(200, count=10)
    ticks = []
    for tick in pacer:
        dev.show_frame(tick.index % 16)
        ticks.append(tick)
    assert [t.index for t in ticks] == list(range(10))
    assert all(t.skipped == 0 for t in ticks)
    times = [t.time for t in ticks]
    assert times[-1] - times[0] == pytest.approx(9 / 200, abs=1e-6)  # scheduled slots
    assert pacer.report.sent == 10


def test_pace_duration(dev: Device) -> None:
    start = time.monotonic()
    n = sum(1 for _ in dev.pace(100, duration=0.2))
    assert n == 20
    assert 0.18 <= time.monotonic() - start < 1.0


def test_pace_skip_reports_skipped_ticks(dev: Device) -> None:
    pacer = dev.pace(200, duration=0.2, late="skip")
    ticks = []
    for tick in pacer:
        ticks.append(tick)
        time.sleep(0.012)  # slower than the 5 ms period
    assert sum(t.skipped for t in ticks) > 0
    assert pacer.report.skipped == sum(t.skipped for t in ticks)
    assert ticks[-1].index <= 39


def test_pace_send_delivers_every_tick(dev: Device) -> None:
    pacer = dev.pace(200, count=10, late="send")
    for _ in pacer:
        time.sleep(0.008)
    report = pacer.report
    assert (report.sent, report.skipped) == (10, 0)
    assert report.late > 0


def test_pace_stop_from_another_thread(dev: Device) -> None:
    pacer = dev.pace(100)
    threading.Timer(0.05, pacer.stop).start()
    n = sum(1 for _ in pacer)
    assert 2 <= n <= 20
    assert pacer.report.stopped


def test_pace_report_before_iteration(dev: Device) -> None:
    with pytest.raises(RuntimeError):
        dev.pace(100).report  # noqa: B018
