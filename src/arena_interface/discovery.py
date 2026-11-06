"""Finding controllers among the computer's serial ports."""

from __future__ import annotations

from dataclasses import dataclass

from serial.tools import list_ports

__all__ = ["TEENSY_PID", "TEENSY_VID", "PortInfo", "find_ports"]

#: USB vendor id of a Teensy in USB-serial mode (PJRC).
TEENSY_VID = 0x16C0
#: USB product id of a Teensy in USB-serial mode.
TEENSY_PID = 0x0483


@dataclass(frozen=True)
class PortInfo:
    """A serial port found on this computer.

    Attributes:
        device: Name to open, e.g. ``"/dev/ttyACM0"`` or ``"COM5"``.
        description: Human-readable description from the OS.
        vid: USB vendor id, or None for non-USB ports.
        pid: USB product id, or None for non-USB ports.
        serial_number: USB serial number, if reported.
    """

    device: str
    description: str
    vid: int | None
    pid: int | None
    serial_number: str | None

    @property
    def is_teensy(self) -> bool:
        """True if the USB ids are those of a Teensy in serial mode."""
        return (self.vid, self.pid) == (TEENSY_VID, TEENSY_PID)


def find_ports(*, all_ports: bool = False) -> list[PortInfo]:
    """List serial ports that may be a G6 arena controller.

    Nothing is sent to any port. A Teensy-based device that is not a
    controller has the same USB ids, so a match is a candidate, not a
    guarantee; ``Device.open()`` can confirm candidates by probing them.

    Args:
        all_ports: List every serial port, not only Teensy ones.

    Returns:
        Matching ports, sorted by device name.

    Example:
        >>> for port in find_ports():  # doctest: +SKIP
        ...     print(port.device, port.description)
        /dev/ttyACM0 Teensyduino Serial
    """
    found = [
        PortInfo(
            device=p.device,
            description=p.description or "",
            vid=p.vid,
            pid=p.pid,
            serial_number=p.serial_number,
        )
        for p in list_ports.comports()
    ]
    if not all_ports:
        found = [p for p in found if p.is_teensy]
    return sorted(found, key=lambda p: p.device)
