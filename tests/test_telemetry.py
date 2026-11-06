import struct

import pytest

from arena_interface import ProtocolError
from arena_interface.protocol import ArenaState
from arena_interface.telemetry import (
    CommandRecord,
    FrameRecord,
    StateKind,
    StateRecord,
    UnknownRecord,
    decode_telemetry_block,
)

# Record layouts from Telemetry.h: [len, type, seq u32, t_us u32, payload].


def _record(rtype: int, seq: int, t_us: int, body: bytes) -> bytes:
    return struct.pack("<BBII", 10 + len(body), rtype, seq, t_us) + body


def _block(
    records: list[bytes], *, first_seq: int, more: bool = False, flags: int = 1
) -> bytes:
    header = struct.pack(
        "<IIHIBBH", 5_000_000, first_seq, len(records), 3, more, flags, 2
    )
    return header + b"".join(records)


def test_decode_block_with_all_record_types() -> None:
    cmd = _record(1, 10, 100, bytes([0x70, 0x00, 2, 0x05, 0x00]))
    frame = _record(2, 11, 200, struct.pack("<HHIHIBB", 5, 3, 1200, 900, 2400, 1, 0b11))
    state = _record(3, 12, 300, struct.pack("<BBH", 2, 4, 3))
    block = decode_telemetry_block(_block([cmd, frame, state], first_seq=10, more=True))

    assert block.t_now_us == 5_000_000
    assert block.first_seq == 10
    assert block.dropped == 3
    assert block.more
    assert block.events_enabled and not block.survived_reboot
    assert block.boot_count == 2
    assert block.last_seq == 12

    c, f, s = block.records
    assert c == CommandRecord(10, 100, 0x70, 0, b"\x05\x00")
    assert f == FrameRecord(11, 200, 5, 3, 1200, 900, 2400, 1, True, True)
    assert isinstance(s, StateRecord)
    assert s.kind is StateKind.STATE_CHANGE
    assert s.new_state is ArenaState.SHOW_FRAME
    assert s.arg == 3


def test_empty_block() -> None:
    block = decode_telemetry_block(_block([], first_seq=0, flags=0b1110))
    assert block.records == ()
    assert block.last_seq is None
    assert not block.events_enabled
    assert block.survived_reboot and block.heap_collision and block.synthetic


def test_unknown_record_type_and_state_kind() -> None:
    odd = _record(9, 1, 0, b"\x01\x02")
    future_state = _record(3, 2, 0, struct.pack("<BBH", 99, 0, 0))
    rec, state = decode_telemetry_block(
        _block([odd, future_state], first_seq=1)
    ).records
    assert isinstance(rec, UnknownRecord) and rec.type == 9
    assert isinstance(state, StateRecord) and state.kind == 99
    assert state.new_state is None


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00" * 10,  # shorter than the header
        _block([_record(2, 1, 0, b"\x00" * 4)], first_seq=1),  # truncated FRAME body
        _block([_record(1, 1, 0, b"")], first_seq=1)[:-3],  # length runs past the end
    ],
)
def test_malformed_blocks(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_telemetry_block(payload)


def test_record_count_mismatch() -> None:
    payload = bytearray(_block([_record(3, 1, 0, bytes(4))], first_seq=1))
    payload[8] = 2  # n_records field claims two
    with pytest.raises(ProtocolError, match="2 records"):
        decode_telemetry_block(bytes(payload))
