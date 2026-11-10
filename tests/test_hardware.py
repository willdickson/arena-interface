"""Tests against a real G6 arena controller.

Skipped by default; run with ``uv run pytest -m hardware``. Environment:

* ``ARENA_PORT``: serial port (default: auto-detect).
* ``ARENA_NO_DAC=1``: skip analog-output tests (unit without a working DAC).
* ``ARENA_LOOPBACK=1``: a BNC cable connects Analog Out to Analog In 1.
* ``ARENA_ALLOW_RESET=1``: allow the test that reboots the controller.

The tests leave the arena dark, do not change the refresh rate (it stays in
force until a reboot) or the Digital IO roles, and remove the temporary
pattern they upload (``zz_hwtest.pat``, briefly renamed ``00_hwtest.pat``)
even if a test fails.
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Iterator

import numpy as np
import pytest

from arena_interface import (
    AoMode,
    ArenaState,
    Capability,
    CommandError,
    CommandRecord,
    Device,
    ErrorCode,
    FirmwareFlag,
    GrayLevel,
    PanelDisplayMode,
    Pattern,
    PlaybackReport,
    StateRecord,
    TrialMode,
)
from arena_interface import messages as m
from arena_interface.protocol import Opcode

pytestmark = pytest.mark.hardware

TEMP_NAME = "zz_hwtest.pat"
TEMP_FIRST = "00_hwtest.pat"  # sorts before TEMP_NAME, shifting ids between


def _flag(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0")


@pytest.fixture(scope="module")
def dev() -> Iterator[Device]:
    dev = Device.open(os.environ.get("ARENA_PORT") or None)
    try:
        yield dev
    finally:
        if dev.is_open:
            _remove_temp_patterns(dev)
        dev.close()


def _remove_temp_patterns(dev: Device) -> None:
    dev.patterns.cache = False
    for name in (TEMP_NAME, TEMP_FIRST):
        stale = dev.patterns.find(name)
        if stale is not None:
            dev.stop()
            dev.patterns.delete(stale)


@pytest.fixture(autouse=True)
def dark_afterwards(dev: Device) -> Iterator[None]:
    yield
    if dev.is_open:
        dev.all_off()


def _state(dev: Device) -> ArenaState:
    return dev.diagnostics.health().state  # type: ignore[return-value]


def _blank_image(dev: Device) -> list[list[int]]:
    height, width = dev.arena.pixel_shape
    return [[0] * width for _ in range(height)]


# -- identity ---------------------------------------------------------------


def test_identity(dev: Device) -> None:
    assert Capability.G6_MODE in dev.capabilities
    assert dev.info.mac is not None and len(dev.info.mac) == 17
    assert dev.firmware is not None
    rows, cols = dev.arena_shape  # type: ignore[misc]
    assert rows * cols in (20, 40, 48)
    assert len(dev.firmware.build_date) == 10


def test_health_counters_advance(dev: Device) -> None:
    first = dev.diagnostics.health()
    time.sleep(0.2)
    second = dev.diagnostics.health()
    assert second.uptime_ms >= first.uptime_ms + 150
    assert second.loop_count > first.loop_count
    assert second.sd_mounted


def test_diagnostics_read(dev: Device) -> None:
    if dev.supports(FirmwareFlag.CRASH_REPORT):
        assert len(dev.diagnostics.crash_report().raw) == 128
    if dev.supports(FirmwareFlag.SD_FAST_PATH):
        assert dev.diagnostics.sd_info().mounted
    assert 1 <= dev.diagnostics.spi_clock_mhz <= 30


# -- display ----------------------------------------------------------------


def test_settings_read(dev: Device) -> None:
    assert dev.refresh_rate > 0
    mode = dev.panel_display_mode
    dev.panel_display_mode = PanelDisplayMode.PERSIST
    assert dev.panel_display_mode is PanelDisplayMode.PERSIST
    dev.panel_display_mode = mode


def test_all_on_sends_frames(dev: Device) -> None:
    dev.reset_frames_sent()
    dev.all_on()
    assert _state(dev) is ArenaState.ALL_ON
    time.sleep(0.3)
    assert dev.frames_sent > 10
    dev.stop()
    assert _state(dev) is ArenaState.ALL_OFF


@pytest.mark.parametrize("gray", [GrayLevel.GS2, GrayLevel.GS16])
def test_stream_image(dev: Device, gray: GrayLevel) -> None:
    dev.stream_image(_blank_image(dev), gray_level=gray)
    assert _state(dev) is ArenaState.STREAMING_FRAME


def test_open_loop_trial_with_duration(dev: Device) -> None:
    pid = next(
        e.pattern_id
        for e in dev.patterns
        if dev.patterns.info(e.pattern_id).frame_count > 10
    )
    dev.start_trial(pid, TrialMode.OPEN_LOOP, frame_rate=50, duration=0.5)
    first = dev.frame_position.index
    time.sleep(0.2)
    assert dev.frame_position.index != first
    time.sleep(0.5)
    assert _state(dev) is ArenaState.ALL_OFF  # the controller stopped it


# -- SD card ----------------------------------------------------------------


def test_pattern_round_trip(dev: Device) -> None:
    rows, cols = dev.arena_shape  # type: ignore[misc]
    h, w = dev.arena.pixel_shape
    frames = [
        [[(x // 20 + f) % 16 for x in range(w)] for _ in range(h)] for f in range(6)
    ]
    pattern = Pattern.from_frames(frames, duty_cycle=100)
    dev.stop()
    pid = dev.patterns.upload(TEMP_NAME, pattern, overwrite=True)
    try:
        info = dev.patterns.info(pid)
        assert (info.frame_count, info.rows, info.cols) == (6, rows, cols)
        assert info.duty_cycle == 100
        assert Pattern.from_bytes(dev.patterns.download(pid)) == pattern
        dev.show_pattern(pid)
        for i in range(6):
            dev.show_frame(i)
            assert dev.frame_position.index == i
        with pytest.raises(ValueError):
            dev.show_frame(6)  # refused locally; the display keeps running
        assert _state(dev) is ArenaState.SHOW_FRAME
    finally:
        dev.stop()
        dev.patterns.delete(dev.patterns.find(TEMP_NAME))  # type: ignore[arg-type]
    assert dev.patterns.find(TEMP_NAME) is None


def test_patterns_by_name_and_name_cache(dev: Device) -> None:
    h, w = dev.arena.pixel_shape
    pattern = Pattern.from_frames(np.zeros((7, h, w), dtype=np.uint8))  # 7 frames
    dev.stop()
    dev.patterns.cache = False
    try:
        pid = dev.patterns.upload(TEMP_NAME, pattern, overwrite=True)
        # Cache off: names are looked up on the card (case and .pat optional).
        assert dev.patterns.find("ZZ_HWTEST") == pid
        dev.show_pattern("zz_hwtest")
        assert dev.frame_position.frame_count == 7
        dev.stop()

        # Cache on: same answers, from one request per lookup.
        dev.patterns.cache = True
        assert dev.patterns.find(TEMP_NAME) == pid
        dev.start_trial("ZZ_hwtest.PAT", frame_rate=10)
        assert dev.frame_position.frame_count == 7
        dev.stop()

        # Changed behind the library's back (as another program would): the
        # pattern moves earlier, and the ids of the patterns it passes shift.
        dev._send(m.set_pattern_filename(pid, TEMP_FIRST))
        assert dev.patterns.find(TEMP_NAME) is None
        on_card = {e.name: e.pattern_id for e in dev.patterns}
        assert on_card[TEMP_FIRST] < pid
        for name, expected in on_card.items():
            assert dev.patterns.find(name) == expected, name
        dev.show_pattern("00_HWTEST")
        assert dev.frame_position.frame_count == 7
        dev.stop()

        # Changed through the library: the cache follows.
        assert dev.patterns.name(on_card[TEMP_FIRST]) == TEMP_FIRST  # be sure
        dev.patterns.delete(on_card[TEMP_FIRST])
        assert dev.patterns.find(TEMP_FIRST) is None
        on_card = {e.name: e.pattern_id for e in dev.patterns}
        for name, expected in on_card.items():
            assert dev.patterns.find(name) == expected, name
    finally:
        _remove_temp_patterns(dev)
    assert dev.patterns.find(TEMP_NAME) is None
    assert dev.patterns.find(TEMP_FIRST) is None


def test_transfer_refused_while_displaying(dev: Device) -> None:
    dev.all_on()
    with pytest.raises(CommandError) as excinfo:
        dev.patterns.download(1)
    assert excinfo.value.status == ErrorCode.DISPLAY_ACTIVE


# -- telemetry and sampling --------------------------------------------------


def test_telemetry_records_commands_with_host_times(dev: Device) -> None:
    dev.telemetry.drain()
    t0 = time.time()
    dev.all_on()
    dev.all_off()
    t1 = time.time()
    events = dev.telemetry.drain()
    cmds = [e for e in events if isinstance(e.record, CommandRecord)]
    assert [e.record.opcode for e in cmds][:2] == [Opcode.ALL_ON, Opcode.ALL_OFF]
    for e in cmds[:2]:
        assert e.time is not None and t0 - 0.05 <= e.time <= t1 + 0.05
    states = [e.record.new_state for e in events if isinstance(e.record, StateRecord)]
    assert ArenaState.ALL_ON in states


def test_sampling_rate(dev: Device) -> None:
    samples = list(dev.sample(dev.io.read_digital, rate_hz=100, count=100))
    span = samples[-1].time - samples[0].time
    assert 99 / span >= 90


# -- analog I/O --------------------------------------------------------------

needs_dac = pytest.mark.skipif(_flag("ARENA_NO_DAC"), reason="ARENA_NO_DAC set")


@needs_dac
def test_analog_out_readback(dev: Device) -> None:
    dev.io.set_analog_out_mode(AoMode.PROGRAMMABLE)
    try:
        for mv in (0, 1234, 5000):
            dev.io.analog_out = mv
            assert abs(dev.io.analog_out - mv) <= 2  # 12-bit DAC
    finally:
        dev.io.analog_out = 0


@needs_dac
@pytest.mark.skipif(not _flag("ARENA_LOOPBACK"), reason="needs ARENA_LOOPBACK=1")
def test_analog_loopback_is_linear(dev: Device) -> None:
    dev.io.set_analog_out_mode(AoMode.PROGRAMMABLE)
    points = []
    try:
        for mv in range(0, 5001, 1000):
            dev.io.analog_out = mv
            time.sleep(0.02)
            reads = [dev.io.read_analog_in().ain1_mv for _ in range(5)]
            points.append((dev.io.analog_out, statistics.mean(reads)))
    finally:
        dev.io.analog_out = 0
    xs, ys = zip(*points, strict=True)
    gain, offset = statistics.linear_regression(xs, ys)
    assert 0.9 < gain < 1.1
    assert abs(offset) < 500
    assert max(abs(y - (gain * x + offset)) for x, y in points) < 100


# -- reset ------------------------------------------------------------------


@pytest.mark.skipif(not _flag("ARENA_ALLOW_RESET"), reason="needs ARENA_ALLOW_RESET=1")
def test_reset_and_reconnect(dev: Device) -> None:
    mac = dev.info.mac
    dev.reset()
    assert not dev.is_open
    dev.reconnect(timeout=20)
    assert dev.info.mac == mac
    assert dev.diagnostics.health().uptime_ms < 30_000


# -- host-paced playback -----------------------------------------------------

PLAYBACK_FPS = 200  # below the 300 Hz default GS16 refresh of a 2x10


def _assert_timing(report: PlaybackReport, lateness: list[float] | None = None) -> None:
    """On schedule apart from rare outliers (OS or VM scheduling, SD stalls)."""
    assert report.skipped == 0
    assert report.median_late_s < 0.0005
    assert report.max_late_s < 0.010
    if lateness is not None:
        assert sum(v < 0.002 for v in lateness) >= 0.95 * len(lateness)


def _long_pattern(dev: Device) -> int:
    for entry in dev.patterns:
        if dev.patterns.info(entry.pattern_id).frame_count >= 100:
            return entry.pattern_id
    pytest.skip("needs a pattern with at least 100 frames on the SD card")


def test_play_frames_timing_and_display(dev: Device) -> None:
    pid = _long_pattern(dev)
    report = dev.play_frames(range(100), fps=PLAYBACK_FPS, pattern=pid, verify=True)
    print(report)
    _assert_timing(report)
    # The controller's own accounting is exact; an SD read stall can still make
    # a frame miss its refresh slot now and then at fps close to the refresh.
    assert report.displayed + report.superseded == report.changes
    assert report.superseded <= 0.02 * report.changes


def test_play_images_timing(dev: Device) -> None:
    h, w = dev.arena.pixel_shape
    images = [
        [[((x + 4 * f) // 20) % 2 * 15 for x in range(w)] for _ in range(h)]
        for f in range(100)
    ]
    report = dev.play_images(images, fps=PLAYBACK_FPS)
    print(report)
    assert report.sent == 100
    _assert_timing(report)


def test_pace_live_frames(dev: Device) -> None:
    pid = _long_pattern(dev)
    dev.show_pattern(pid)
    pacer = dev.pace(PLAYBACK_FPS, count=200)
    lateness = []
    for tick in pacer:
        lateness.append(tick.late_by)
        dev.show_frame(tick.index % 100)
    print(pacer.report)
    _assert_timing(pacer.report, lateness)
