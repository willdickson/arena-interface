"""Serial transport: the one place where bytes are written to and read from the port.

[`Transport`][arena_interface.transport.Transport] wraps a pyserial-like port
and provides request/response exchanges on top of
[`framing`][arena_interface.framing]:

* [`request`][arena_interface.transport.Transport.request]: send a frame and
  wait for the response whose echo opcode matches;
* [`request_bulk_read`][arena_interface.transport.Transport.request_bulk_read]:
  the same, then receive the raw bytes that follow a size header (0x84, 0x8A);
* [`request_bulk_write`][arena_interface.transport.Transport.request_bulk_write]:
  send a header plus a raw payload, then wait for the response (0x85, 0xE0).

The protocol has no request id, so exchanges are strictly one at a time. A
lock serialises them, which makes a ``Transport`` safe to share between
threads (each call is atomic; sequences of calls are not).

Traffic is logged at DEBUG level on the ``arena_interface.transport`` logger.
Diagnostic text lines from DEBUG_SERIAL firmware builds are logged at INFO
level on ``arena_interface.firmware``.
"""

from __future__ import annotations

import logging
import threading
import time
from types import TracebackType
from typing import NoReturn, Protocol

import serial

from .errors import DeviceDisconnected, DeviceNotFound, DeviceTimeout
from .framing import DiagnosticLine, FrameReader, Response

__all__ = [
    "DEFAULT_BULK_IDLE_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "SerialPortLike",
    "Transport",
]

_log = logging.getLogger(__name__)
_device_log = logging.getLogger("arena_interface.firmware")

#: Default time (s) to wait for a response.
DEFAULT_TIMEOUT = 1.0
#: Default time (s) a bulk transfer may go without receiving a byte.
DEFAULT_BULK_IDLE_TIMEOUT = 5.0
#: Port read timeout (s): how long one read() blocks waiting for data.
POLL_INTERVAL = 0.01
#: Port write timeout (s).
WRITE_TIMEOUT = 5.0
#: Size of the bulk-transfer size header payload (u64).
_BULK_SIZE_BYTES = 8
#: Chunk size for writing bulk payloads.
_WRITE_CHUNK = 64 * 1024
#: Longest hex dump written to the traffic log.
_LOG_LIMIT = 48


class SerialPortLike(Protocol):
    """The subset of ``serial.Serial`` used by the transport.

    ``arena_interface.testing.FakePort`` implements it for tests and examples.
    """

    timeout: float | None

    @property
    def in_waiting(self) -> int:
        """Number of bytes ready to read."""
        ...

    def read(self, size: int = 1) -> bytes:
        """Read up to ``size`` bytes, blocking at most ``timeout`` seconds."""
        ...

    def write(self, data: bytes, /) -> int | None:
        """Write bytes."""
        ...

    def reset_input_buffer(self) -> None:
        """Discard bytes received but not yet read."""
        ...

    def close(self) -> None:
        """Release the port."""
        ...


def _hex(data: bytes) -> str:
    if len(data) <= _LOG_LIMIT:
        return data.hex(" ")
    return f"{data[:16].hex(' ')} ... ({len(data)} bytes)"


class Transport:
    """Request/response exchanges with the controller over one serial port.

    Args:
        port: An open pyserial-like port. The transport sets its read timeout
            and takes ownership: closing the transport closes the port.
        name: Port name used in log and error messages.

    Example:
        >>> from arena_interface.framing import encode_binary
        >>> with Transport.open("/dev/ttyACM0") as t:  # doctest: +SKIP
        ...     reply = t.request(encode_binary(0x17), expect=0x17)
        ...     print(int.from_bytes(reply.payload, "little"), "Hz")
    """

    def __init__(self, port: SerialPortLike, *, name: str | None = None) -> None:
        """Wrap an already open port."""
        self._port: SerialPortLike | None = port
        self.name = name or str(getattr(port, "port", None) or "<port>")
        self._reader = FrameReader()
        self._lock = threading.Lock()
        port.timeout = POLL_INTERVAL

    @classmethod
    def open(cls, port_name: str, *, baudrate: int = 115200) -> Transport:
        """Open a serial port by name.

        Args:
            port_name: OS port name, e.g. ``"/dev/ttyACM0"`` or ``"COM5"``.
            baudrate: Ignored by the device (USB CDC); kept for completeness.

        Returns:
            A connected transport.

        Raises:
            DeviceNotFound: If the port does not exist or cannot be opened.
        """
        try:
            port = serial.Serial(
                port_name,
                baudrate=baudrate,
                timeout=POLL_INTERVAL,
                write_timeout=WRITE_TIMEOUT,
            )
        except (serial.SerialException, OSError, ValueError) as exc:
            raise DeviceNotFound(f"cannot open {port_name}: {exc}") from exc
        return cls(port, name=port_name)

    # -- lifecycle --------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True until the transport is closed or the connection is lost."""
        return self._port is not None

    def close(self) -> None:
        """Close the port. Safe to call more than once."""
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except (serial.SerialException, OSError) as exc:  # pragma: no cover
                _log.debug("error closing %s: %s", self.name, exc)

    def __enter__(self) -> Transport:
        """Return self; the port is already open."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the port."""
        self.close()

    # -- exchanges --------------------------------------------------------

    def request(
        self, frame: bytes, *, expect: int, timeout: float = DEFAULT_TIMEOUT
    ) -> Response:
        """Send a frame and return the device's response to it.

        Responses whose echo opcode differs from ``expect`` are late replies to
        an earlier request; they are logged and skipped.

        Args:
            frame: Complete request frame.
            expect: Opcode the response must echo.
            timeout: Seconds to wait for the response.

        Returns:
            The response (any status; the caller interprets it).

        Raises:
            DeviceTimeout: If no matching response arrives in time.
            DeviceDisconnected: If the port fails.
        """
        with self._lock:
            self._write(frame)
            return self._await_response(expect, timeout)

    def request_bulk_read(
        self,
        frame: bytes,
        *,
        expect: int,
        timeout: float = DEFAULT_TIMEOUT,
        idle_timeout: float = DEFAULT_BULK_IDLE_TIMEOUT,
    ) -> tuple[Response, bytes]:
        """Send a frame, then receive a size-prefixed raw byte stream.

        The device first answers with a normal response whose payload is the
        size (u64 LE), then sends that many raw bytes. A failed request
        (non-zero status) has no raw part.

        Args:
            frame: Complete request frame.
            expect: Opcode the response must echo.
            timeout: Seconds to wait for the size header.
            idle_timeout: Seconds the raw stream may stall before giving up.

        Returns:
            The header response and the raw bytes (empty on failure).

        Raises:
            DeviceTimeout: If the header does not arrive, or the stream stalls
                before all bytes are received.
            DeviceDisconnected: If the port fails.
        """
        with self._lock:
            self._write(frame)
            header = self._await_response(expect, timeout)
            if not header.ok or len(header.payload) < _BULK_SIZE_BYTES:
                return header, b""
            size = int.from_bytes(header.payload[:_BULK_SIZE_BYTES], "little")
            return header, self._read_raw(size, idle_timeout)

    def request_bulk_write(
        self,
        header: bytes,
        data: bytes,
        *,
        expect: int,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Response:
        """Send a header and a raw payload, then return the device's response.

        The device answers only after it has read the whole payload.

        Args:
            header: Opcode-first command header.
            data: Raw payload following the header.
            expect: Opcode the response must echo.
            timeout: Seconds to wait for the response once all data is written.

        Returns:
            The response.

        Raises:
            DeviceTimeout: If writing stalls or no response arrives in time.
            DeviceDisconnected: If the port fails.
        """
        with self._lock:
            self._write(header)
            view = memoryview(data)
            for start in range(0, len(view), _WRITE_CHUNK):
                self._write(bytes(view[start : start + _WRITE_CHUNK]), log=False)
            _log.debug("-> %d raw bytes", len(data))
            return self._await_response(expect, timeout)

    def discard_input(self) -> int:
        """Drop all received but unprocessed bytes; return how many were dropped."""
        with self._lock:
            return self._discard_input()

    # -- internals --------------------------------------------------------

    def _require_port(self) -> SerialPortLike:
        if self._port is None:
            raise DeviceDisconnected(f"{self.name} is closed")
        return self._port

    def _write(self, data: bytes, *, log: bool = True) -> None:
        port = self._require_port()
        if log:
            _log.debug("-> %s", _hex(data))
        try:
            port.write(data)
        except serial.SerialTimeoutException as exc:
            raise DeviceTimeout(f"write to {self.name} timed out") from exc
        except (serial.SerialException, OSError) as exc:
            self._lost(exc)

    def _read_some(self) -> bytes:
        port = self._require_port()
        try:
            waiting = port.in_waiting
            data = port.read(waiting if waiting > 0 else 1)
        except (serial.SerialException, OSError) as exc:
            self._lost(exc)
        return data

    def _lost(self, exc: BaseException) -> NoReturn:
        self.close()
        raise DeviceDisconnected(f"connection to {self.name} lost: {exc}") from exc

    def _await_response(self, expect: int, timeout: float) -> Response:
        deadline = time.monotonic() + timeout
        while True:
            item = self._reader.next()
            if isinstance(item, Response):
                _log.debug(
                    "<- status=%d opcode=0x%02x payload=%s",
                    item.status,
                    item.opcode,
                    _hex(item.payload),
                )
                if item.opcode == expect:
                    return item
                _log.warning(
                    "skipping stale response for 0x%02x (waiting for 0x%02x)",
                    item.opcode,
                    expect,
                )
                continue
            if isinstance(item, DiagnosticLine):
                _device_log.info("%s", item.text)
                continue
            if time.monotonic() >= deadline:
                dropped = self._discard_input()
                raise DeviceTimeout(
                    f"no response to 0x{expect:02x} from {self.name} within "
                    f"{timeout:g} s"
                    + (f" ({dropped} unparsed byte(s) discarded)" if dropped else "")
                )
            self._reader.feed(self._read_some())

    def _read_raw(self, size: int, idle_timeout: float) -> bytes:
        chunks = [self._reader.take_raw(size)]
        received = len(chunks[0])
        last_progress = time.monotonic()
        while received < size:
            data = self._read_some()
            if data:
                chunk = data[: size - received]
                chunks.append(chunk)
                received += len(chunk)
                if len(chunk) < len(data):  # pragma: no cover - device never does this
                    self._reader.feed(data[len(chunk) :])
                last_progress = time.monotonic()
            elif time.monotonic() - last_progress >= idle_timeout:
                self._discard_input()
                raise DeviceTimeout(
                    f"bulk transfer from {self.name} stalled: received {received} "
                    f"of {size} bytes"
                )
        _log.debug("<- %d raw bytes", received)
        return b"".join(chunks)

    def _discard_input(self) -> int:
        dropped = self._reader.clear()
        port = self._port
        if port is not None:
            try:
                dropped += port.in_waiting
                port.reset_input_buffer()
            except (serial.SerialException, OSError) as exc:
                self._lost(exc)
        return dropped
