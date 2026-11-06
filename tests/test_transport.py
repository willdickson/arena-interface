import logging
import threading
import time

import pytest
from _ports import ScriptedPort, echo_handler, response

from arena_interface import DeviceDisconnected, DeviceError, DeviceTimeout
from arena_interface.framing import encode_binary
from arena_interface.transport import Transport

# -- request ----------------------------------------------------------------


def test_request_returns_matching_response() -> None:
    port = ScriptedPort(lambda _: response(0x17, payload=(300).to_bytes(2, "little")))
    t = Transport(port)
    reply = t.request(encode_binary(0x17), expect=0x17)
    assert reply.ok
    assert int.from_bytes(reply.payload, "little") == 300
    assert port.written == [bytes([0x01, 0x17])]


def test_request_returns_error_status_without_raising() -> None:
    port = ScriptedPort(lambda _: response(0x16, status=1, payload=b"Expected [03 16]"))
    reply = Transport(port).request(encode_binary(0x16), expect=0x16)
    assert reply.status == 1
    assert reply.text == "Expected [03 16]"


def test_request_handles_byte_by_byte_delivery() -> None:
    port = ScriptedPort(lambda _: response(0xC2, payload=bytes(8)))
    port.max_read = 1
    reply = Transport(port).request(encode_binary(0xC2), expect=0xC2)
    assert len(reply.payload) == 8


def test_request_skips_stale_response(caplog: pytest.LogCaptureFixture) -> None:
    port = ScriptedPort(lambda _: response(0x70) + response(0x72, payload=bytes(4)))
    with caplog.at_level(logging.WARNING):
        reply = Transport(port).request(encode_binary(0x72), expect=0x72)
    assert reply.opcode == 0x72
    assert "stale response for 0x70" in caplog.text


def test_request_logs_diagnostic_lines(caplog: pytest.LogCaptureFixture) -> None:
    port = ScriptedPort(lambda _: b"\xff[42] [cmd] hi\n" + response(0x00))
    with caplog.at_level(logging.INFO, logger="arena_interface.firmware"):
        Transport(port).request(encode_binary(0x00), expect=0x00)
    assert "[42] [cmd] hi" in caplog.text


def test_request_timeout_discards_partial_input() -> None:
    port = ScriptedPort(lambda _: bytes([5, 0, 0x88]))  # truncated frame
    t = Transport(port)
    start = time.monotonic()
    with pytest.raises(DeviceTimeout, match=r"0x88.*3 unparsed"):
        t.request(encode_binary(0x88, b"\x01\x00"), expect=0x88, timeout=0.1)
    assert time.monotonic() - start < 1.0
    # The leftover bytes are gone, so the next exchange is clean.
    port.handler = lambda _: response(0x80, payload=b"\x02\x00")
    assert t.request(encode_binary(0x80), expect=0x80).payload == b"\x02\x00"


def test_timeout_is_a_device_error_and_builtin_timeout() -> None:
    with pytest.raises(DeviceTimeout) as excinfo:
        Transport(ScriptedPort()).request(
            encode_binary(0x00), expect=0x00, timeout=0.02
        )
    assert isinstance(excinfo.value, DeviceError)
    assert isinstance(excinfo.value, TimeoutError)


def test_disconnect_closes_transport() -> None:
    port = ScriptedPort()
    port.fail_reads = True
    t = Transport(port)
    with pytest.raises(DeviceDisconnected, match="unplugged"):
        t.request(encode_binary(0x00), expect=0x00)
    assert not t.is_open
    assert port.closed
    with pytest.raises(DeviceDisconnected, match="closed"):
        t.request(encode_binary(0x00), expect=0x00)


def test_write_failure_is_disconnect() -> None:
    port = ScriptedPort()

    def broken_write(_data: bytes) -> int:
        raise OSError("write failed")

    port.write = broken_write  # type: ignore[method-assign]
    with pytest.raises(DeviceDisconnected):
        Transport(port).request(encode_binary(0x00), expect=0x00)


def test_context_manager_closes_port() -> None:
    port = ScriptedPort()
    with Transport(port) as t:
        assert t.is_open
    assert port.closed
    t.close()  # idempotent


def test_requests_are_serialised_between_threads() -> None:
    in_flight = 0
    overlap = False
    lock = threading.Lock()

    def handler(data: bytes) -> bytes:
        nonlocal in_flight, overlap
        with lock:
            in_flight += 1
            overlap |= in_flight > 1
        time.sleep(0.005)
        with lock:
            in_flight -= 1
        return response(data[1])

    t = Transport(ScriptedPort(handler))
    errors: list[BaseException] = []

    def worker(opcode: int) -> None:
        try:
            for _ in range(10):
                assert t.request(encode_binary(opcode), expect=opcode).opcode == opcode
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(op,)) for op in (0x17, 0x1C, 0xC4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert not overlap


# -- bulk read --------------------------------------------------------------


def _bulk_reply(opcode: int, body: bytes) -> bytes:
    return response(opcode, payload=len(body).to_bytes(8, "little")) + body


def test_bulk_read_receives_body() -> None:
    body = bytes(range(256)) * 40
    port = ScriptedPort(lambda _: _bulk_reply(0x84, body))
    header, data = Transport(port).request_bulk_read(
        encode_binary(0x84, b"\x01\x00"), expect=0x84
    )
    assert header.ok
    assert data == body


def test_bulk_read_body_in_small_reads() -> None:
    body = b"PK" + bytes(500)
    port = ScriptedPort(lambda _: _bulk_reply(0x8A, body))
    port.max_read = 7
    _, data = Transport(port).request_bulk_read(encode_binary(0x8A), expect=0x8A)
    assert data == body


def test_bulk_read_empty_file() -> None:
    port = ScriptedPort(lambda _: _bulk_reply(0x84, b""))
    header, data = Transport(port).request_bulk_read(
        encode_binary(0x84, b"\x01\x00"), expect=0x84
    )
    assert header.ok
    assert data == b""


def test_bulk_read_error_status_has_no_body() -> None:
    port = ScriptedPort(
        lambda _: response(0x84, status=10, payload=b"Stop display first")
    )
    header, data = Transport(port).request_bulk_read(
        encode_binary(0x84, b"\x01\x00"), expect=0x84
    )
    assert header.status == 10
    assert data == b""


def test_bulk_read_stall_raises() -> None:
    body = bytes(1000)
    port = ScriptedPort(lambda _: _bulk_reply(0x84, body)[:-400])  # 400 bytes lost
    t = Transport(port)
    with pytest.raises(DeviceTimeout, match="received 600 of 1000"):
        t.request_bulk_read(
            encode_binary(0x84, b"\x01\x00"), expect=0x84, idle_timeout=0.1
        )


# -- bulk write -------------------------------------------------------------


def test_bulk_write_sends_header_and_data() -> None:
    received = bytearray()
    header = bytes([0x85, 0x00, 0x00]) + (200_000).to_bytes(8, "little")
    data = bytes(i % 251 for i in range(200_000))

    def handler(chunk: bytes) -> bytes:
        received.extend(chunk)
        if len(received) == len(header) + len(data):
            return response(0x85)
        return b""

    port = ScriptedPort(handler)
    reply = Transport(port).request_bulk_write(header, data, expect=0x85)
    assert reply.ok
    assert bytes(received) == header + data
    assert len(port.written) > 2  # payload written in chunks


def test_discard_input() -> None:
    port = ScriptedPort()
    t = Transport(port)
    port.push(b"\x05\x00\x01")
    assert t.discard_input() == 3


def test_echo_handler_delay_is_within_timeout() -> None:
    t = Transport(ScriptedPort(echo_handler(delay=0.02)))
    assert t.request(encode_binary(0x34), expect=0x34).ok
