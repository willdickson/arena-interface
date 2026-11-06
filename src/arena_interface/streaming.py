"""Continuous data from the controller: telemetry events and periodic samples.

The controller never sends data on its own, so both are polling loops exposed
as blocking iterators:

```python
with Device.open() as dev:
    for event in dev.telemetry.events(duration=10):
        print(event.time, event.record)

    for s in dev.sample(dev.io.read_analog_in, rate_hz=100, duration=5):
        print(s.time, s.value.ain1_mv)
```

Times are wall-clock seconds (``time.time()``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from . import messages as m
from .telemetry import (
    StateKind,
    StateRecord,
    TelemetryBlock,
    TelemetryRecord,
    decode_telemetry_block,
)

if TYPE_CHECKING:
    from .device import Device

__all__ = ["EventStream", "Sample", "Sampler", "Telemetry", "TelemetryEvent"]

_log = logging.getLogger(__name__)
_T = TypeVar("_T")
_WRAP = 2**32
_WRAP_S = _WRAP / 1e6  # the controller's microsecond clock wraps every ~71.6 min
# The firmware stamps a command record with its receipt time but appends it
# after handling the command, so records the command causes come first with
# later times. A step back of "almost a whole wrap" is such a reordering.
_MAX_REORDER_US = 300_000_000


@dataclass(frozen=True)
class TelemetryEvent:
    """One telemetry record with its host time.

    Attributes:
        time: Wall-clock time (``time.time()`` seconds) at which the controller
            recorded the event, or None for records written before the
            controller's last reset.
        record: The decoded record (``FrameRecord``, ``StateRecord``,
            ``CommandRecord`` or ``UnknownRecord``); ``record.t_us`` is the raw
            controller time.
    """

    time: float | None
    record: TelemetryRecord

    @property
    def seq(self) -> int:
        """Record sequence number."""
        return self.record.seq


@dataclass(frozen=True)
class Sample(Generic[_T]):
    """One reading taken by [`Sampler`][arena_interface.streaming.Sampler].

    Attributes:
        time: Wall-clock time of the reading (midpoint of the request).
        value: What the read function returned.
    """

    time: float
    value: _T


def _is_boot(record: TelemetryRecord) -> bool:
    return isinstance(record, StateRecord) and record.kind == StateKind.BOOT


class Telemetry:
    """The controller's telemetry ring (``device.telemetry``).

    The controller logs every command it receives, every change of the
    displayed frame and every state change, with microsecond timestamps. The
    ring survives software resets. Recording is on by default.

    Records read from the controller are kept until they have been returned
    to you, so breaking out of an [`events`][arena_interface.streaming.Telemetry.events]
    loop and starting a new one (or calling
    [`drain`][arena_interface.streaming.Telemetry.drain]) neither loses nor
    repeats events.

    Attributes:
        lost: Records lost to ring overflow, as seen by this connection.
    """

    def __init__(self, device: Device) -> None:
        """Bind to a device."""
        self._dev = device
        self._fetched_seq: int | None = None  # highest seq read from the ring
        self._expected: int | None = None  # next seq if nothing was lost
        self._boot_seq: int | None = None  # seq of the latest BOOT record seen
        self._backlog: list[TelemetryEvent] = []
        self.lost = 0

    def configure(
        self,
        events: bool = True,
        *,
        synthetic: bool = False,
        synthetic_rate: int | None = None,
    ) -> None:
        """Turn event recording and the synthetic test producer on or off."""
        self._dev._send(
            m.set_telemetry(events, synthetic=synthetic, synthetic_rate=synthetic_rate)
        )

    def read_block(self, ack_seq: int | None = None) -> TelemetryBlock:
        """Low level: acknowledge records up to ``ack_seq`` and read the next block.

        Mixing this with ``events()`` or ``drain()`` on the same device can
        acknowledge records those have not returned yet.
        """
        return decode_telemetry_block(
            self._dev._send(m.get_telemetry_block(ack_seq)).payload
        )

    def drain(self) -> list[TelemetryEvent]:
        """Return every waiting event and acknowledge them all."""
        with self._dev._lock:
            self._catch_up()
            if self._fetched_seq is not None:
                self.read_block(self._fetched_seq)  # free the last block
            events, self._backlog = self._backlog, []
        return events

    def events(
        self, *, poll_interval: float = 0.05, duration: float | None = None
    ) -> EventStream:
        """Stream events as the controller records them.

        Args:
            poll_interval: Seconds between polls when no records are waiting.
            duration: Stop after this many seconds; None runs until you break
                out of the loop.

        Returns:
            An iterable [`EventStream`][arena_interface.streaming.EventStream].
        """
        return EventStream(self, poll_interval, duration)

    # -- internals --------------------------------------------------------

    def _read_raw(self) -> tuple[TelemetryBlock, float]:
        """Read one block, acknowledging everything already fetched."""
        t0 = time.time()
        block = self.read_block(self._fetched_seq)
        return block, (t0 + time.time()) / 2

    def _accept(self, blocks: list[tuple[TelemetryBlock, float]]) -> None:
        """Time the new records of ``blocks`` and append them to the backlog."""
        fresh = [
            (rec, block, host)
            for block, host in blocks
            for rec in block.records
            if self._fetched_seq is None or rec.seq > self._fetched_seq
        ]
        for rec, _, _ in fresh:  # locate the latest reset before timing anything
            if _is_boot(rec):
                self._boot_seq = rec.seq
        for rec, block, host in fresh:
            if self._expected is not None and rec.seq > self._expected:
                gap = rec.seq - self._expected
                self.lost += gap
                _log.warning("%d telemetry record(s) lost before seq %d", gap, rec.seq)
            self._expected = rec.seq + 1
            before_boot = self._boot_seq is not None and rec.seq < self._boot_seq
            when = None if before_boot else self._host_time(rec, block, host)
            self._backlog.append(TelemetryEvent(when, rec))
            self._fetched_seq = rec.seq

    def _host_time(
        self, rec: TelemetryRecord, block: TelemetryBlock, host: float
    ) -> float:
        """Host time of a record, assuming it is less than one clock wrap old.

        True for records read while streaming. Records fetched in a catch-up
        can be older; ``_retime``
        corrects those.
        """
        return host - ((block.t_now_us - rec.t_us) % _WRAP) / 1e6

    def _retime(self) -> None:
        """Date the current boot's backlog by chaining back from the newest record.

        Records are logged in (nearly) time order, so going back one record
        subtracts the clock difference between the two; command records,
        stamped on receipt but logged after their effects, may step back
        slightly in time. This is exact unless the controller
        logged nothing for more than one clock wrap (71.6 min); the BOOT
        record's age must then equal the controller's uptime, and any missing
        whole wraps are added at the largest gap between records.
        """
        timed = [i for i, e in enumerate(self._backlog) if e.time is not None]
        if len(timed) < 2:
            return
        events = [self._backlog[i] for i in timed]
        times = [0.0] * len(events)
        gaps = [0.0] * len(events)  # gaps[i]: from events[i] to events[i + 1]
        times[-1] = events[-1].time  # type: ignore[assignment]  # newest is fresh
        for i in range(len(events) - 2, -1, -1):
            step = (events[i + 1].record.t_us - events[i].record.t_us) % _WRAP
            if step > _WRAP - _MAX_REORDER_US:
                step -= _WRAP  # logged out of time order, not a whole wrap apart
            gaps[i] = step / 1e6
            times[i] = times[i + 1] - gaps[i]
        boot_time = self._dev._boot_time
        if boot_time is not None and _is_boot(events[0].record):
            expected = boot_time + events[0].record.t_us / 1e6
            missing = round((times[0] - expected) / _WRAP_S)
            if missing > 0:
                widest = max(range(len(events) - 1), key=gaps.__getitem__)
                _log.info(
                    "telemetry: %d clock wrap(s) placed in the %.0f s gap after seq %d",
                    missing,
                    gaps[widest],
                    events[widest].seq,
                )
                for i in range(widest + 1):
                    times[i] -= missing * _WRAP_S
        for i, t in zip(timed, times, strict=True):
            self._backlog[i] = TelemetryEvent(t, self._backlog[i].record)

    def _catch_up(self) -> None:
        """Fetch every record waiting in the ring into the backlog."""
        while True:
            block, host = self._read_raw()
            if block.last_seq is not None:
                # Page forward: the next read acknowledges this block.
                self._accept([(block, host)])
            if not block.more:
                break
        # Re-time all fetched records now that the latest reset is known.
        if any(_is_boot(e.record) for e in self._backlog):
            last_boot = max(e.seq for e in self._backlog if _is_boot(e.record))
            self._boot_seq = max(self._boot_seq or 0, last_boot)
            self._backlog = [
                TelemetryEvent(None, e.record) if e.seq < last_boot else e
                for e in self._backlog
            ]
        self._retime()

    def _poll(self) -> bool:
        """Fetch one block into the backlog; return True if more are waiting."""
        block, host = self._read_raw()
        self._accept([(block, host)])
        return block.more


class EventStream:
    """Iterator over telemetry events; see ``device.telemetry.events()``.

    Attributes:
        lost: Records lost to ring overflow during this stream.
    """

    def __init__(
        self, telemetry: Telemetry, poll_interval: float, duration: float | None
    ) -> None:
        """Create the stream; nothing is read until iteration starts."""
        self._telemetry = telemetry
        self._poll_interval = poll_interval
        self._duration = duration
        self.lost = 0

    def __iter__(self) -> Iterator[TelemetryEvent]:
        """Yield events until the duration elapses or the loop is broken."""
        tel = self._telemetry
        lost_before = tel.lost
        deadline = None if self._duration is None else time.monotonic() + self._duration
        with tel._dev._lock:
            tel._catch_up()
        while True:
            while tel._backlog:
                yield tel._backlog.pop(0)
            self.lost = tel.lost - lost_before
            if deadline is not None and time.monotonic() >= deadline:
                return
            with tel._dev._lock:
                more = tel._poll()
            if not more and not tel._backlog:
                time.sleep(self._poll_interval)


class Sampler(Generic[_T]):
    """Iterator calling a read function at a fixed rate; see ``device.sample()``.

    Attributes:
        skipped: Samples skipped because a read finished too late.
    """

    def __init__(
        self,
        read: Callable[[], _T],
        rate_hz: float,
        duration: float | None,
        count: int | None,
    ) -> None:
        """Validate the schedule; nothing is read until iteration starts."""
        if rate_hz <= 0:
            raise ValueError(f"rate_hz must be positive, got {rate_hz}")
        self._read = read
        self._period = 1.0 / rate_hz
        self._duration = duration
        self._count = count
        self.skipped = 0

    def __iter__(self) -> Iterator[Sample[_T]]:
        """Yield samples until the duration or count is reached."""
        start = time.monotonic()
        n = 0
        taken = 0
        while True:
            if self._count is not None and taken >= self._count:
                return
            due = start + n * self._period
            if self._duration is not None and due - start >= self._duration:
                return
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            t0 = time.time()
            value = self._read()
            yield Sample((t0 + time.time()) / 2, value)
            taken += 1
            n += 1
            late = int((time.monotonic() - start) / self._period) - n
            if late > 0:
                self.skipped += late
                n += late
