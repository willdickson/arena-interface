"""Host-paced playback: the computer decides when each frame is shown.

Three ways to use it, all on a ``Device``:

```python
dev.show_pattern(4)
report = dev.play_frames(range(200), fps=200)        # frames of an SD pattern
report = dev.play_images(images, fps=120)            # streamed images

player = dev.start_frames(range(200), fps=200)       # same, in the background
report = player.wait()

for tick in dev.pace(rate_hz=200, duration=10):      # live: you choose each frame
    dev.show_frame(choose_frame())
```

Timing: frames are scheduled against a fixed clock (no drift). The scheduler
sleeps until just before each slot and then busy-waits for sub-millisecond
accuracy. When a frame cannot be sent on time, ``late="skip"`` drops frames
whose slot has passed so the sequence keeps its schedule, and ``late="send"``
sends every frame, so the sequence runs long. Either way the
[`PlaybackReport`][arena_interface.playback.PlaybackReport] says what
happened, and falling behind is logged on ``arena_interface.playback``.

A frame is only displayed if it stays in place for at least one refresh of
the controller, so ``fps`` should not exceed the refresh rate
(``dev.refresh_rate``); a ``PerformanceWarning`` is issued if it does.
Frames of an SD pattern also wait for the controller's SD read, which
occasionally stalls, so at fps close to the refresh rate a frame can now and
then be replaced unseen; ``play_frames(..., verify=True)`` reports it.
"""

from __future__ import annotations

import logging
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Literal

__all__ = ["LatePolicy", "Pacer", "Playback", "PlaybackReport", "Tick"]

_log = logging.getLogger(__name__)

LatePolicy = Literal["skip", "send"]

#: Busy-wait this long before each slot (sleep is less precise than this).
_SPIN_S = 0.0015 if sys.platform == "win32" else 0.0005
#: At most one "falling behind" log line per this many seconds.
_LOG_INTERVAL_S = 1.0


def _check_rate(rate_hz: float, late: str) -> None:
    if not rate_hz > 0:
        raise ValueError(f"the rate must be positive, got {rate_hz}")
    if late not in ("skip", "send"):
        raise ValueError(f"late must be 'skip' or 'send', got {late!r}")


@dataclass(frozen=True)
class Tick:
    """One slot of a [`Pacer`][arena_interface.playback.Pacer] schedule.

    Attributes:
        index: Slot number, counting from 0 (skipped slots are counted).
        time: Wall-clock time (``time.time()``) the slot was due.
        late_by: Seconds between the slot's due time and this tick.
        skipped: Slots skipped just before this one (``late="skip"`` only).
    """

    index: int
    time: float
    late_by: float
    skipped: int


@dataclass(frozen=True)
class PlaybackReport:
    """What happened during a playback.

    Attributes:
        rate_hz: Requested frames (or ticks) per second.
        planned: Frames in the sequence (ticks for an open-ended pacer: those
            that came due).
        sent: Frames actually sent.
        skipped: Frames dropped because their slot had passed.
        late: Frames sent more than half a frame period after their slot.
        median_late_s: Median lateness of the frames sent.
        p99_late_s: 99th-percentile lateness of the frames sent.
        max_late_s: Largest lateness of a frame sent.
        duration_s: Time from the first slot to the end.
        stopped: Playback was stopped before the end.
        refresh_rate: The controller's refresh rate during playback, if read.
        changes: Frame changes the sequence asked for, counting from the
            frame on show when playback started (``verify=True``; a frame
            equal to the one before it is not a change). None if frames were
            skipped.
        displayed: Frame changes the controller logged as reaching the
            panels (``verify=True``). Equal to ``changes`` when every
            frame was displayed.
        superseded: Frames replaced before they reached the panels
            (``verify=True``).
    """

    rate_hz: float
    planned: int
    sent: int
    skipped: int
    late: int
    median_late_s: float
    p99_late_s: float
    max_late_s: float
    duration_s: float
    stopped: bool = False
    refresh_rate: int | None = None
    changes: int | None = None
    displayed: int | None = None
    superseded: int | None = None

    @property
    def on_schedule(self) -> bool:
        """True if no frame was skipped or late."""
        return self.skipped == 0 and self.late == 0

    def __str__(self) -> str:
        """One-paragraph summary."""
        text = (
            f"{self.sent}/{self.planned} frames at {self.rate_hz:g}/s in "
            f"{self.duration_s:.2f} s; skipped {self.skipped}, late {self.late}; "
            f"lateness median {self.median_late_s * 1e3:.2f} ms, "
            f"p99 {self.p99_late_s * 1e3:.2f} ms, max {self.max_late_s * 1e3:.2f} ms"
        )
        if self.displayed is not None:
            text += (
                f"; {self.displayed}/{self.changes} frame changes displayed "
                f"({self.superseded} replaced unseen)"
            )
        if self.stopped:
            text += "; stopped early"
        return text


class _Schedule:
    """Fixed-rate slots with drift-free timing and lateness bookkeeping."""

    def __init__(self, rate_hz: float, late: LatePolicy, stop: threading.Event) -> None:
        self.period = 1.0 / rate_hz
        self.rate_hz = rate_hz
        self.late = late
        self.stop = stop
        self.lateness: list[float] = []
        self.skipped = 0
        self.planned = 0
        self.stopped = False
        self._t0 = 0.0
        self._wall0 = 0.0
        self._last_log = 0.0
        self._skipped_since_log = 0
        self._late_since_log = 0

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self._wall0 = time.time()

    def wall(self, slot: int) -> float:
        return self._wall0 + slot * self.period

    def next_slot(self, slot: int, last: int | None) -> tuple[int, int] | None:
        """Wait for ``slot``; return (slot to use, slots skipped) or None to stop.

        With ``late="skip"``, slots whose time has passed by a full period are
        skipped (but never beyond ``last``, the final slot, when given).
        """
        skipped = 0
        now = time.perf_counter()
        if self.late == "skip":
            behind = int((now - self._t0) / self.period) - slot
            if behind > 0:
                if last is not None:
                    behind = min(behind, last - slot)
                skipped = behind
                slot += behind
                self.skipped += behind
                self._note_behind(skipped=behind)
        due = self._t0 + slot * self.period
        while True:
            if self.stop.is_set():
                self.stopped = True
                return None
            remaining = due - time.perf_counter()
            if remaining <= 0:
                break
            if remaining > _SPIN_S:
                self.stop.wait(remaining - _SPIN_S)
        late_by = time.perf_counter() - due
        self.lateness.append(late_by)
        if late_by > self.period / 2:
            self._note_behind(late=1)
        return slot, skipped

    def _note_behind(self, *, skipped: int = 0, late: int = 0) -> None:
        self._skipped_since_log += skipped
        self._late_since_log += late
        now = time.monotonic()
        if now - self._last_log >= _LOG_INTERVAL_S:
            _log.warning(
                "playback falling behind its %g/s schedule: %d skipped, %d late",
                self.rate_hz,
                self._skipped_since_log,
                self._late_since_log,
            )
            self._last_log = now
            self._skipped_since_log = self._late_since_log = 0

    def report(self, sent: int, **extra: object) -> PlaybackReport:
        lat = sorted(self.lateness) or [0.0]
        return PlaybackReport(
            rate_hz=self.rate_hz,
            planned=self.planned,
            sent=sent,
            skipped=self.skipped,
            late=sum(1 for v in self.lateness if v > self.period / 2),
            median_late_s=statistics.median(lat),
            p99_late_s=lat[min(len(lat) - 1, int(len(lat) * 0.99))],
            max_late_s=lat[-1],
            duration_s=time.perf_counter() - self._t0,
            stopped=self.stopped,
            **extra,  # type: ignore[arg-type]
        )


def run_sequence(
    count: int,
    send: Callable[[int], object],
    rate_hz: float,
    late: LatePolicy,
    stop: threading.Event,
    progress: Callable[[int], None] | None = None,
) -> tuple[_Schedule, int]:
    """Send items ``0 .. count-1`` on a fixed schedule; return (schedule, sent).

    Library internals; use ``Device.play_frames`` and friends.
    """
    schedule = _Schedule(rate_hz, late, stop)
    schedule.planned = count
    sent = 0
    slot = 0
    schedule.start()
    while slot < count:
        nxt = schedule.next_slot(slot, count - 1)
        if nxt is None:
            break
        slot, _ = nxt
        send(slot)
        sent += 1
        if progress is not None:
            progress(slot + 1)
        slot += 1
    return schedule, sent


class Playback:
    """A playback running in a background thread.

    Returned by ``Device.start_frames`` and ``Device.start_images``. Your
    thread may keep using the device; each of its commands delays the next
    frame slightly (the report shows the effect).

    Use [`wait`][arena_interface.playback.Playback.wait] to block until it
    ends, or [`stop`][arena_interface.playback.Playback.stop] to end it early.
    As a context manager, leaving the block stops playback if it is still
    running.
    """

    def __init__(
        self,
        target: Callable[[threading.Event, Callable[[int], None]], PlaybackReport],
        total: int,
    ) -> None:
        """Start ``target`` in a thread; ``total`` is the number of frames."""
        self._stop = threading.Event()
        self._report: PlaybackReport | None = None
        self._error: BaseException | None = None
        self._done_frames = 0
        self.total = total
        """Frames in the sequence."""
        self._thread = threading.Thread(
            target=self._run, args=(target,), name="arena-playback", daemon=True
        )
        self._thread.start()

    def _run(
        self, target: Callable[[threading.Event, Callable[[int], None]], PlaybackReport]
    ) -> None:
        try:
            self._report = target(self._stop, self._set_progress)
        except BaseException as exc:  # re-raised by wait()
            self._error = exc

    def _set_progress(self, done: int) -> None:
        self._done_frames = done

    @property
    def running(self) -> bool:
        """True until playback finishes, is stopped, or fails."""
        return self._thread.is_alive()

    @property
    def progress(self) -> int:
        """Frames of the sequence passed so far (sent or skipped)."""
        return self._done_frames

    def stop(self) -> PlaybackReport:
        """Stop playback (the current frame stays displayed) and return the report."""
        self._stop.set()
        return self.wait()

    def wait(self, timeout: float | None = None) -> PlaybackReport:
        """Block until playback ends and return its report.

        Raises:
            TimeoutError: If ``timeout`` seconds pass first.
            DeviceError: Any error the playback thread hit, re-raised here.
        """
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("playback still running")
        if self._error is not None:
            raise self._error
        assert self._report is not None
        return self._report

    @property
    def report(self) -> PlaybackReport | None:
        """The report once playback has ended without error, else None."""
        return None if self.running else self._report

    def __enter__(self) -> Playback:
        """Return self."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop playback if still running (thread errors are not re-raised here)."""
        self._stop.set()
        self._thread.join()


class Pacer:
    """A fixed-rate clock for live playback; see ``Device.pace``.

    Iterate to receive one [`Tick`][arena_interface.playback.Tick] per slot
    and send your frame in the loop body. After the loop,
    [`report`][arena_interface.playback.Pacer.report] summarises the timing.
    """

    def __init__(
        self,
        rate_hz: float,
        *,
        duration: float | None = None,
        count: int | None = None,
        late: LatePolicy = "skip",
        refresh_rate: int | None = None,
    ) -> None:
        """Validate the schedule; timing starts when iteration starts."""
        _check_rate(rate_hz, late)
        self._rate = rate_hz
        self._duration = duration
        self._count = count
        self._late = late
        self._refresh = refresh_rate
        self._stop = threading.Event()
        self._schedule: _Schedule | None = None
        self._ticks = 0

    def stop(self) -> None:
        """End the iteration at the next slot (e.g. from another thread)."""
        self._stop.set()

    def __iter__(self) -> Iterator[Tick]:
        """Yield ticks until the duration or count is reached, or stop()."""
        schedule = _Schedule(self._rate, self._late, self._stop)
        self._schedule = schedule
        if self._count is not None:
            last: int | None = self._count - 1
        elif self._duration is not None:
            last = max(0, int(self._duration * self._rate) - 1)
        else:
            last = None
        schedule.start()
        slot = 0
        while last is None or slot <= last:
            nxt = schedule.next_slot(slot, last)
            if nxt is None:
                break
            slot, skipped = nxt
            self._ticks += 1
            schedule.planned = slot + 1
            yield Tick(slot, schedule.wall(slot), schedule.lateness[-1], skipped)
            slot += 1

    @property
    def report(self) -> PlaybackReport:
        """Timing of the ticks so far ("sent" counts ticks yielded)."""
        if self._schedule is None:
            raise RuntimeError("the pacer has not been iterated yet")
        return self._schedule.report(self._ticks, refresh_rate=self._refresh)
