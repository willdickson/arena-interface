"""Minimal scripted serial port for transport tests.

This is NOT the firmware emulator (``arena_interface.testing.FakePort``, M4).
Each write is passed to a handler that returns the bytes the "device" sends
back; reads then return those bytes, blocking for ``timeout`` when empty like
pyserial does.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

Handler = Callable[[bytes], bytes]


class ScriptedPort:
    """A pyserial-like port driven by a reply handler."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.handler: Handler = handler or (lambda _data: b"")
        self.timeout: float | None = 0.01
        self.written: list[bytes] = []
        self.closed = False
        self.fail_reads = False
        self.max_read: int | None = None
        self._rx = bytearray()
        self._cond = threading.Condition()

    # -- test helpers -----------------------------------------------------

    def push(self, data: bytes) -> None:
        """Make bytes available to read, as if the device had sent them."""
        with self._cond:
            self._rx += data
            self._cond.notify_all()

    # -- pyserial-like API ------------------------------------------------

    @property
    def in_waiting(self) -> int:
        if self.fail_reads:
            raise OSError("device unplugged")
        return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        if self.fail_reads:
            raise OSError("device unplugged")
        with self._cond:
            if not self._rx and self.timeout:
                self._cond.wait(self.timeout)
            n = size if self.max_read is None else min(size, self.max_read)
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    def write(self, data: bytes, /) -> int:
        self.written.append(bytes(data))
        reply = self.handler(bytes(data))
        if reply:
            self.push(reply)
        return len(data)

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._rx.clear()

    def close(self) -> None:
        self.closed = True


def response(opcode: int, status: int = 0, payload: bytes = b"") -> bytes:
    """Encode a response frame as the firmware would send it."""
    return bytes([2 + len(payload), status, opcode]) + payload


def echo_handler(delay: float = 0.0) -> Handler:
    """Reply to every binary command with an empty OK response."""

    def handle(data: bytes) -> bytes:
        if delay:
            time.sleep(delay)
        return response(data[1])

    return handle
