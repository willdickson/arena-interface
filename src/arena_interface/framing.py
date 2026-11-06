r"""Byte-level framing of the controller's serial protocol.

This module is pure (no I/O). It builds request frames and splits the byte
stream coming back from the device into response frames and diagnostic text
lines. See ``docs/PROTOCOL.md`` § Message framing for the wire format.

Request framings:

* binary command: ``[len, opcode, params...]`` with ``len`` = bytes after it,
  at most ``0x31``;
* stream frame: ``[0x32, len_lo, len_hi, payload...]``;
* opcode-first commands (``0x83``, ``0x85``, ``0xE0``) are plain byte strings
  built by the message layer.

Device output:

* response frame: ``[len, status, echo_opcode, payload...]``;
* diagnostic line (DEBUG_SERIAL firmware builds only): ``0xFF`` followed by
  ASCII text up to and including ``\n``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

__all__ = [
    "DIAGNOSTIC_SENTINEL",
    "MAX_BINARY_LENGTH",
    "MAX_BINARY_PARAMS",
    "MAX_RESPONSE_LENGTH",
    "MAX_STREAM_PAYLOAD",
    "STREAM_FRAME_OPCODE",
    "DiagnosticLine",
    "FrameReader",
    "Response",
    "encode_binary",
    "encode_stream",
]

_log = logging.getLogger(__name__)

#: Largest legal length byte of a binary command (firmware ``0x31``).
MAX_BINARY_LENGTH = 0x31
#: Most parameter bytes a binary command can carry (length byte minus opcode).
MAX_BINARY_PARAMS = MAX_BINARY_LENGTH - 1
#: Opcode / discriminator of a stream frame.
STREAM_FRAME_OPCODE = 0x32
#: Largest stream-frame payload the u16 length field can describe.
MAX_STREAM_PAYLOAD = 0xFFFF
#: First byte of a diagnostic text line; never a valid response length.
DIAGNOSTIC_SENTINEL = 0xFF
#: Largest response length byte (200-byte buffer minus the length byte).
MAX_RESPONSE_LENGTH = 198
#: Smallest response length byte (status + echo opcode).
MIN_RESPONSE_LENGTH = 2


def _check_byte(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be 0..255, got {value}")
    return value


def encode_binary(opcode: int, params: bytes = b"") -> bytes:
    """Build a binary command frame ``[len, opcode, params...]``.

    Args:
        opcode: Command opcode, 0..255.
        params: Parameter bytes, at most 48 of them.

    Returns:
        The frame to write to the port.

    Raises:
        ValueError: If the opcode is out of range or there are too many
            parameter bytes for binary framing.

    Example:
        >>> encode_binary(0x70, bytes([5, 0])).hex(" ")
        '03 70 05 00'
    """
    _check_byte(opcode, "opcode")
    params = bytes(params)
    if len(params) > MAX_BINARY_PARAMS:
        raise ValueError(
            f"binary commands carry at most {MAX_BINARY_PARAMS} parameter bytes, "
            f"got {len(params)}"
        )
    return bytes([len(params) + 1, opcode]) + params


def encode_stream(payload: bytes) -> bytes:
    """Build a stream frame ``[0x32, len_lo, len_hi, payload...]``.

    Args:
        payload: Frame payload ("FR" prefix plus panel blocks).

    Returns:
        The frame to write to the port.

    Raises:
        ValueError: If the payload is empty or longer than 65535 bytes.
    """
    payload = bytes(payload)
    if not 0 < len(payload) <= MAX_STREAM_PAYLOAD:
        raise ValueError(
            f"stream payload must be 1..{MAX_STREAM_PAYLOAD} bytes, got {len(payload)}"
        )
    return bytes([STREAM_FRAME_OPCODE]) + len(payload).to_bytes(2, "little") + payload


@dataclass(frozen=True)
class Response:
    """One response frame from the device.

    Attributes:
        status: Status byte; 0 means success.
        opcode: Opcode of the request this answers (the echo byte).
        payload: Bytes after the echo byte; binary data or an ASCII message.
    """

    status: int
    opcode: int
    payload: bytes

    @property
    def ok(self) -> bool:
        """True if the device reported success (status 0)."""
        return self.status == 0

    @property
    def text(self) -> str:
        """The payload decoded as an ASCII message (invalid bytes replaced)."""
        return self.payload.decode("ascii", errors="replace")


@dataclass(frozen=True)
class DiagnosticLine:
    """A debug text line emitted by DEBUG_SERIAL firmware builds.

    Attributes:
        text: The line without the sentinel byte and trailing newline.
    """

    text: str


class FrameReader:
    r"""Incremental parser for the device-to-host byte stream.

    Feed it bytes as they arrive and pull complete items with
    [`next`][arena_interface.framing.FrameReader.next]. Bytes that cannot start
    a valid frame are dropped (and logged) so the reader resynchronises on its
    own. During a bulk transfer the caller takes raw bytes with
    [`take_raw`][arena_interface.framing.FrameReader.take_raw] instead.

    Example:
        >>> reader = FrameReader()
        >>> reader.feed(bytes([0x04, 0x00, 0x17, 0x2C]))
        >>> reader.next() is None  # incomplete: one byte missing
        True
        >>> reader.feed(b"\x01")
        >>> reader.next()
        Response(status=0, opcode=23, payload=b',\x01')
    """

    def __init__(self) -> None:
        """Create an empty reader."""
        self._buf = bytearray()
        #: Number of bytes dropped while resynchronising.
        self.discarded = 0

    def __len__(self) -> int:
        """Number of buffered, not yet parsed bytes."""
        return len(self._buf)

    def feed(self, data: bytes) -> None:
        """Append bytes received from the device."""
        self._buf += data

    def next(self) -> Response | DiagnosticLine | None:
        """Return the next complete item, or None if more bytes are needed."""
        while self._buf:
            first = self._buf[0]
            if first == DIAGNOSTIC_SENTINEL:
                end = self._buf.find(b"\n")
                if end < 0:
                    return None
                line = bytes(self._buf[1:end]).decode("ascii", errors="replace")
                del self._buf[: end + 1]
                return DiagnosticLine(line.rstrip("\r"))
            if first > MAX_RESPONSE_LENGTH:
                self._drop(1, f"invalid length byte 0x{first:02x}")
                continue
            total = 1 + first
            if len(self._buf) < total:
                return None
            if first < MIN_RESPONSE_LENGTH:
                self._drop(total, "runt frame")
                continue
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            return Response(status=frame[1], opcode=frame[2], payload=frame[3:])
        return None

    def take_raw(self, max_bytes: int) -> bytes:
        """Remove and return up to ``max_bytes`` buffered bytes, unparsed."""
        out = bytes(self._buf[:max_bytes])
        del self._buf[:max_bytes]
        return out

    def clear(self) -> int:
        """Discard everything buffered and return how many bytes were dropped."""
        n = len(self._buf)
        self._buf.clear()
        return n

    def _drop(self, n: int, reason: str) -> None:
        _log.warning("dropping %d byte(s) from device: %s", n, reason)
        del self._buf[:n]
        self.discarded += n
