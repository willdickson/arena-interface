"""Decoding of the controller's telemetry ring (GET_TELEMETRY_BLOCK).

The controller logs every command, every displayed frame change and every
state change into a ring buffer that survives software resets. A host drains
it with GET_TELEMETRY_BLOCK, acknowledging records once stored. Record layout
follows ``Telemetry.h`` (ring layout version 2).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from .errors import ProtocolError
from .protocol import ArenaState

__all__ = [
    "CommandRecord",
    "FrameRecord",
    "StateKind",
    "StateRecord",
    "TelemetryBlock",
    "TelemetryRecord",
    "UnknownRecord",
    "decode_telemetry_block",
]

_HEADER = struct.Struct("<IIHIBBH")  # 18 bytes
_RECORD_HEADER = struct.Struct("<BBII")  # len, type, seq, t_us = 10 bytes


class StateKind(IntEnum):
    """Kinds of STATE telemetry records."""

    BOOT = 1
    STATE_CHANGE = 2
    ERROR_GLYPH = 3
    SD_SLOW = 4
    RING_OVERRUN = 5
    TELEMETRY = 6
    SD_OPEN = 7
    WATCHDOG_CONTEXT = 8
    PREV_ISR_COUNT = 9
    TIMER_FAIL = 10
    SD_LAYOUT = 11
    SD_SLOW_CONTEXT = 12
    SD_READS = 13
    SD_READS_CHECKPOINT = 14


@dataclass(frozen=True)
class _RecordBase:
    seq: int
    t_us: int


@dataclass(frozen=True)
class CommandRecord(_RecordBase):
    """A command the controller received.

    Attributes:
        seq: Record sequence number.
        t_us: Receipt time (controller microseconds; wraps every ~71.6 min).
        opcode: Command opcode (0xFE for synthetic test records).
        status: Status of the reply (0xFF if none was sent).
        params: Up to the first 8 parameter bytes of the request.
    """

    opcode: int
    status: int
    params: bytes


@dataclass(frozen=True)
class FrameRecord(_RecordBase):
    """A change of the displayed frame.

    Attributes:
        seq: Record sequence number.
        t_us: Start of the SPI transfer to the panels.
        index: Frame index shown.
        pattern_id: Pattern shown.
        sd_load_us: Duration of the SD read that produced the frame.
        spi_us: Duration of the SPI transfer.
        request_age_us: Time from the request (or decision) to the transfer.
        superseded: Frames loaded but replaced before being shown.
        from_sd: The frame was read from the SD card.
        contiguous: The pattern file is stored contiguously.
    """

    index: int
    pattern_id: int
    sd_load_us: int
    spi_us: int
    request_age_us: int
    superseded: int
    from_sd: bool
    contiguous: bool


@dataclass(frozen=True)
class StateRecord(_RecordBase):
    """A state transition or other controller event.

    Attributes:
        seq: Record sequence number.
        t_us: Event time.
        kind: Event kind (raw int for kinds unknown to this library).
        code: Kind-specific code byte.
        arg: Kind-specific argument.
    """

    kind: StateKind | int
    code: int
    arg: int

    @property
    def new_state(self) -> ArenaState | None:
        """For STATE_CHANGE records, the state entered; otherwise None."""
        if self.kind != StateKind.STATE_CHANGE:
            return None
        try:
            return ArenaState(self.code)
        except ValueError:
            return None


@dataclass(frozen=True)
class UnknownRecord(_RecordBase):
    """A record of a type this library does not know.

    Attributes:
        seq: Record sequence number.
        t_us: Record time.
        type: Record type byte.
        raw: The complete record bytes.
    """

    type: int
    raw: bytes = field(repr=False)


TelemetryRecord = CommandRecord | FrameRecord | StateRecord | UnknownRecord


@dataclass(frozen=True)
class TelemetryBlock:
    """One GET_TELEMETRY_BLOCK reply.

    Attributes:
        t_now_us: Controller time when the reply was built.
        first_seq: Sequence number of the first record (0 if none).
        dropped: Records lost to ring overflow since the ring was created.
        more: More unread records are waiting.
        events_enabled: Event recording is on.
        survived_reboot: The ring contents were kept across a reset.
        heap_collision: The ring was disabled to protect memory.
        synthetic: The synthetic test producer is running.
        boot_count: Resets the ring has survived.
        records: Decoded records, oldest first.
    """

    t_now_us: int
    first_seq: int
    dropped: int
    more: bool
    events_enabled: bool
    survived_reboot: bool
    heap_collision: bool
    synthetic: bool
    boot_count: int
    records: tuple[TelemetryRecord, ...]

    @property
    def last_seq(self) -> int | None:
        """Sequence number of the last record, to acknowledge next; None if empty."""
        return self.records[-1].seq if self.records else None


def _state_kind(value: int) -> StateKind | int:
    try:
        return StateKind(value)
    except ValueError:
        return value


def _decode_record(raw: bytes) -> TelemetryRecord:
    length, rtype, seq, t_us = _RECORD_HEADER.unpack_from(raw)
    body = raw[_RECORD_HEADER.size :]
    try:
        if rtype == 1:
            opcode, status, plen = body[0], body[1], body[2]
            return CommandRecord(seq, t_us, opcode, status, bytes(body[3 : 3 + plen]))
        if rtype == 2:
            idx, pat, sd_us, spi_us, age_us, sup, flags = struct.unpack_from(
                "<HHIHIBB", body
            )
            return FrameRecord(
                seq,
                t_us,
                idx,
                pat,
                sd_us,
                spi_us,
                age_us,
                sup,
                bool(flags & 0x01),
                bool(flags & 0x02),
            )
        if rtype == 3:
            kind, code, arg = struct.unpack_from("<BBH", body)
            return StateRecord(seq, t_us, _state_kind(kind), code, arg)
    except (IndexError, struct.error):
        raise ProtocolError(f"truncated telemetry record of type {rtype}") from None
    return UnknownRecord(seq, t_us, rtype, bytes(raw))


def decode_telemetry_block(payload: bytes) -> TelemetryBlock:
    """Decode a GET_TELEMETRY_BLOCK reply.

    Raises:
        ProtocolError: If the payload is malformed.
    """
    if len(payload) < _HEADER.size:
        raise ProtocolError(f"telemetry block too short ({len(payload)} bytes)")
    t_now, first_seq, n_records, dropped, more, flags, boots = _HEADER.unpack_from(
        payload
    )
    records: list[TelemetryRecord] = []
    pos = _HEADER.size
    while pos < len(payload):
        length = payload[pos]
        if length < _RECORD_HEADER.size or pos + length > len(payload):
            raise ProtocolError(f"bad telemetry record length {length} at offset {pos}")
        records.append(_decode_record(payload[pos : pos + length]))
        pos += length
    if len(records) != n_records:
        raise ProtocolError(
            f"telemetry block says {n_records} records but contains {len(records)}"
        )
    return TelemetryBlock(
        t_now_us=t_now,
        first_seq=first_seq,
        dropped=dropped,
        more=bool(more),
        events_enabled=bool(flags & 0x01),
        survived_reboot=bool(flags & 0x02),
        heap_collision=bool(flags & 0x04),
        synthetic=bool(flags & 0x08),
        boot_count=boots,
        records=tuple(records),
    )
