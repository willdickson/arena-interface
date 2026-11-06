"""Telemetry event streaming and periodic sampling."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from itertools import islice
from types import SimpleNamespace

import pytest

from arena_interface import (
    ArenaState,
    CommandRecord,
    Device,
    FramePosition,
    StateKind,
    StateRecord,
    TelemetryBlock,
)
from arena_interface.protocol import Opcode
from arena_interface.streaming import Telemetry
from arena_interface.testing import FakeController


@pytest.fixture
def ctrl() -> FakeController:
    return FakeController()


@pytest.fixture
def dev(ctrl: FakeController) -> Iterator[Device]:
    dev = Device.open(simulate=ctrl)
    dev.telemetry.drain()  # start from an empty log
    yield dev
    dev.close()


def _seqs(events: list) -> list[int]:
    return [e.seq for e in events]


# -- drain ------------------------------------------------------------------


def test_drain_returns_new_events_once(dev: Device) -> None:
    dev.all_on()
    dev.all_off()
    first = dev.telemetry.drain()
    opcodes = [e.record.opcode for e in first if isinstance(e.record, CommandRecord)]
    assert opcodes == [Opcode.ALL_ON, Opcode.ALL_OFF]
    states = [e.record.new_state for e in first if isinstance(e.record, StateRecord)]
    assert ArenaState.ALL_ON in states
    assert dev.telemetry.drain() == []


def test_event_times_are_wall_clock(dev: Device) -> None:
    t0 = time.time()
    dev.refresh_rate  # noqa: B018 - a command that leaves a CMD record
    t1 = time.time()
    (event,) = [e for e in dev.telemetry.drain() if isinstance(e.record, CommandRecord)]
    assert event.time is not None
    assert t0 - 0.05 <= event.time <= t1 + 0.05


def test_command_times_with_out_of_order_records(dev: Device) -> None:
    """Found on hardware: a CMD record carries its receipt time but is logged
    after the STATE record it causes; chaining back must not see a wrap."""
    t0 = time.time()
    dev.all_on()
    dev.all_off()
    t1 = time.time()
    events = dev.telemetry.drain()
    assert any(isinstance(e.record, StateRecord) for e in events)
    for e in events:
        assert e.time is not None and t0 - 0.05 <= e.time <= t1 + 0.05


# -- events() ---------------------------------------------------------------


def test_events_stop_after_duration(dev: Device) -> None:
    start = time.monotonic()
    assert list(dev.telemetry.events(duration=0.1, poll_interval=0.01)) == []
    assert 0.1 <= time.monotonic() - start < 1.0


def test_events_see_commands_from_another_thread(dev: Device) -> None:
    def commander() -> None:
        time.sleep(0.05)
        dev.all_on()
        dev.all_off()

    thread = threading.Thread(target=commander)
    thread.start()
    events = list(dev.telemetry.events(duration=0.3, poll_interval=0.01))
    thread.join()
    states = [e.record.new_state for e in events if isinstance(e.record, StateRecord)]
    assert states[:2] == [ArenaState.ALL_ON, ArenaState.ALL_OFF]


def test_breaking_out_loses_and_repeats_nothing(dev: Device) -> None:
    for _ in range(30):  # far more than one 178-byte block holds
        dev.refresh_rate  # noqa: B018
    first = list(islice(dev.telemetry.events(), 5))
    rest = dev.telemetry.drain()
    seqs = _seqs(first) + _seqs(rest)
    assert len(seqs) == 30
    assert seqs == list(range(seqs[0], seqs[0] + 30))


def test_lost_records_are_counted(
    dev: Device, ctrl: FakeController, caplog: pytest.LogCaptureFixture
) -> None:
    dev.all_on()
    dev.telemetry.drain()
    for _ in range(3):
        dev.refresh_rate  # noqa: B018
    del ctrl._telemetry[1]  # the ring "overflowed" and lost one record
    stream = dev.telemetry.events(duration=0.05)
    with caplog.at_level(logging.WARNING):
        events = list(stream)
    assert len(events) == 2
    assert stream.lost == 1
    assert dev.telemetry.lost == 1
    assert "1 telemetry record(s) lost" in caplog.text


def test_records_from_before_a_reset_have_no_time(dev: Device) -> None:
    dev.all_on()  # written by the boot that is about to end
    dev.reset()
    dev.reconnect(timeout=1)
    events = dev.telemetry.drain()
    boot = [
        e
        for e in events
        if isinstance(e.record, StateRecord) and e.record.kind == StateKind.BOOT
    ]
    assert boot, "the reboot leaves a BOOT record"
    boot_seq = boot[-1].seq
    assert all(e.time is None for e in events if e.seq < boot_seq)
    assert all(e.time is not None for e in events if e.seq >= boot_seq)


def test_timing_survives_controller_clock_wrap() -> None:
    no_uptime = SimpleNamespace(_boot_time=None)  # _accept does no I/O
    tel = Telemetry(no_uptime)  # type: ignore[arg-type]
    record = CommandRecord(seq=1, t_us=2**32 - 500_000, opcode=0, status=0, params=b"")
    block = TelemetryBlock(
        t_now_us=250_000,  # 0.75 s after the record, across the wrap
        first_seq=1,
        dropped=0,
        more=False,
        events_enabled=True,
        survived_reboot=False,
        heap_collision=False,
        synthetic=False,
        boot_count=0,
        records=(record,),
    )
    tel._accept([(block, 1000.0)])
    (event,) = tel._backlog
    assert event.time == pytest.approx(1000.0 - 0.75)


def test_records_older_than_one_clock_wrap() -> None:
    """Found on hardware: a controller up 2 h 10 min dated boot-time records
    one 71.6-minute wrap too recent."""
    ctrl = FakeController()
    ctrl._boot_time -= 7800.0  # pretend it booted 7800 s ago (> one wrap)
    with Device.open(simulate=ctrl) as dev:
        now = time.time()
        events = dev.telemetry.drain()
    boot = next(e for e in events if isinstance(e.record, StateRecord))
    assert boot.record.kind == StateKind.BOOT
    assert boot.time == pytest.approx(now - 7800.0, abs=1.0)
    newest = events[-1]
    assert newest.time == pytest.approx(now, abs=1.0)


# -- sample() ---------------------------------------------------------------


def test_sample_count_and_values(dev: Device) -> None:
    dev.show_pattern(1)
    samples = list(dev.sample(lambda: dev.frame_position, rate_hz=200, count=5))
    assert len(samples) == 5
    assert all(s.value == FramePosition(0, 16) for s in samples)
    times = [s.time for s in samples]
    assert times == sorted(times)


def test_sample_rate_and_duration(dev: Device) -> None:
    samples = list(dev.sample(dev.io.read_digital, rate_hz=50, duration=0.2))
    assert 9 <= len(samples) <= 11
    gaps = [b.time - a.time for a, b in zip(samples, samples[1:], strict=False)]
    assert sum(gaps) / len(gaps) == pytest.approx(0.02, abs=0.005)


def test_sample_skips_late_readings() -> None:
    def slow() -> int:
        time.sleep(0.025)
        return 1

    with Device.open(simulate=True) as dev:
        sampler = dev.sample(slow, rate_hz=100, duration=0.2)
        samples = list(sampler)
    assert len(samples) < 20
    assert sampler.skipped >= 10
    assert len(samples) + sampler.skipped >= 18


def test_sample_rejects_bad_rate(dev: Device) -> None:
    with pytest.raises(ValueError):
        dev.sample(dev.io.read_digital, rate_hz=0)
