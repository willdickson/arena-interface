"""CRC algorithms used by the G6 pattern file format and the controller.

* CRC-8/AUTOSAR: ``.pat`` header byte 17.
* CRC-16/CCITT-FALSE: per-frame trailer of ``.pat`` files.
* CRC-32/ISO-HDLC: SET_FIRMWARE_FILE reply (same as ``zlib.crc32``).

Parameters match ``reference/firmware/Crc.h``.
"""

from __future__ import annotations

import zlib


def _crc8_table(poly: int) -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return tuple(table)


def _crc16_table(poly: int) -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC8 = _crc8_table(0x2F)
_CRC16 = _crc16_table(0x1021)


def crc8_autosar(data: bytes) -> int:
    """CRC-8/AUTOSAR (poly 0x2F, init 0xFF, xorout 0xFF).

    Example:
        >>> hex(crc8_autosar(b"123456789"))
        '0xdf'
    """
    crc = 0xFF
    for byte in data:
        crc = _CRC8[crc ^ byte]
    return crc ^ 0xFF


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no xorout).

    Example:
        >>> hex(crc16_ccitt_false(b"123456789"))
        '0x29b1'
    """
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16[(crc >> 8) ^ byte]
    return crc


def crc32(data: bytes) -> int:
    """CRC-32/ISO-HDLC, as returned by SET_FIRMWARE_FILE.

    Example:
        >>> hex(crc32(b"123456789"))
        '0xcbf43926'
    """
    return zlib.crc32(data) & 0xFFFFFFFF
