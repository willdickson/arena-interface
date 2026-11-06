import pytest

from arena_interface.framing import (
    DiagnosticLine,
    FrameReader,
    Response,
    encode_binary,
    encode_stream,
)

# -- encode_binary ----------------------------------------------------------


@pytest.mark.parametrize(
    ("opcode", "params", "expected"),
    [
        (0x00, b"", "01 00"),  # ALL_OFF
        (0xFF, b"", "01 ff"),  # ALL_ON
        (0x70, bytes([0x05, 0x01]), "03 70 05 01"),  # SET_FRAME_POSITION 261
        (0x1B, bytes([1]), "02 1b 01"),
    ],
)
def test_encode_binary(opcode: int, params: bytes, expected: str) -> None:
    assert encode_binary(opcode, params).hex(" ") == expected


def test_encode_binary_max_length() -> None:
    frame = encode_binary(0xA2, bytes(48))
    assert frame[0] == 0x31
    assert len(frame) == 50


def test_encode_binary_too_long() -> None:
    with pytest.raises(ValueError, match="at most 48"):
        encode_binary(0xA2, bytes(49))


@pytest.mark.parametrize("opcode", [-1, 256])
def test_encode_binary_bad_opcode(opcode: int) -> None:
    with pytest.raises(ValueError):
        encode_binary(opcode)


def test_encode_binary_opcode_type() -> None:
    with pytest.raises(TypeError):
        encode_binary(True)


# -- encode_stream ----------------------------------------------------------


def test_encode_stream_header() -> None:
    payload = bytes(range(256)) * 4 + bytes(40)  # 1064 bytes = 2x10 GS2 frame
    frame = encode_stream(payload)
    assert frame[:3] == bytes([0x32, 0x28, 0x04])
    assert frame[3:] == payload


@pytest.mark.parametrize("size", [0, 0x10000])
def test_encode_stream_bad_size(size: int) -> None:
    with pytest.raises(ValueError):
        encode_stream(bytes(size))


# -- FrameReader ------------------------------------------------------------


def test_reader_single_frame() -> None:
    r = FrameReader()
    r.feed(bytes([0x04, 0x00, 0x17, 0x2C, 0x01]))
    assert r.next() == Response(status=0, opcode=0x17, payload=b"\x2c\x01")
    assert r.next() is None
    assert len(r) == 0


def test_reader_split_across_feeds() -> None:
    data = bytes([0x05, 0x01, 0x82]) + b"bad"
    r = FrameReader()
    for b in data[:-1]:
        r.feed(bytes([b]))
        assert r.next() is None
    r.feed(data[-1:])
    resp = r.next()
    assert isinstance(resp, Response)
    assert not resp.ok
    assert resp.text == "bad"


def test_reader_multiple_frames_in_one_chunk() -> None:
    r = FrameReader()
    r.feed(bytes([2, 0, 0x30, 2, 0, 0x00]))
    first, second = r.next(), r.next()
    assert isinstance(first, Response) and first.opcode == 0x30
    assert isinstance(second, Response) and second.opcode == 0x00


def test_reader_diagnostic_line() -> None:
    r = FrameReader()
    r.feed(b"\xff[123] hello")
    assert r.next() is None  # no newline yet
    r.feed(b" world\r\n" + bytes([2, 0, 0x33]))
    assert r.next() == DiagnosticLine("[123] hello world")
    resp = r.next()
    assert isinstance(resp, Response) and resp.opcode == 0x33


@pytest.mark.parametrize("runt", [bytes([0]), bytes([1, 0x55])])
def test_reader_drops_runt_frames(runt: bytes) -> None:
    r = FrameReader()
    r.feed(runt + bytes([2, 0, 0x34]))
    resp = r.next()
    assert isinstance(resp, Response) and resp.opcode == 0x34
    assert r.discarded == len(runt)


def test_reader_drops_invalid_length_byte() -> None:
    r = FrameReader()
    r.feed(bytes([0xC7, 2, 0, 0x34]))  # 199 > max response length
    resp = r.next()
    assert isinstance(resp, Response) and resp.opcode == 0x34
    assert r.discarded == 1


def test_reader_take_raw_and_clear() -> None:
    r = FrameReader()
    r.feed(b"abcdef")
    assert r.take_raw(4) == b"abcd"
    assert r.take_raw(10) == b"ef"
    r.feed(b"xyz")
    assert r.clear() == 3
    assert len(r) == 0
