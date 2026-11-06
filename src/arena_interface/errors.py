"""Exceptions raised by arena_interface.

Every exception the library raises on purpose derives from
[`DeviceError`][arena_interface.errors.DeviceError], so a caller can catch
them all with one ``except`` clause:

```python
from arena_interface import DeviceError

try:
    ...
except DeviceError as exc:
    print(f"arena problem: {exc}")
```

Invalid arguments (a frame index out of range, a payload of the wrong size)
are programming errors and raise the built-in ``ValueError`` / ``TypeError``
instead.
"""

from __future__ import annotations


class DeviceError(Exception):
    """Base class for all errors raised by arena_interface."""


class DeviceNotFound(DeviceError):
    """No device could be found or the serial port could not be opened."""


class DeviceDisconnected(DeviceError):
    """The serial connection was lost (device unplugged or reset).

    The connection is closed when this is raised; open a new one to continue.
    """


class DeviceTimeout(DeviceError, TimeoutError):
    """The device did not answer, or a bulk transfer stalled, within the timeout.

    Also a subclass of the built-in ``TimeoutError``.
    """


class ProtocolError(DeviceError):
    """The device sent data that does not follow the wire protocol."""


class CommandError(DeviceError):
    """The device rejected a command (non-zero status).

    Attributes:
        opcode: Opcode of the rejected command.
        status: Status byte returned by the device.
        message: The device's explanation (may be empty).
    """

    def __init__(self, opcode: int, status: int, message: str = "") -> None:
        """Create the error from a response's opcode, status and message."""
        self.opcode = opcode
        self.status = status
        self.message = message
        text = f"command 0x{opcode:02x} failed with status {status}"
        super().__init__(f"{text}: {message}" if message else text)


class UnsupportedCommand(DeviceError):
    """The connected firmware does not advertise support for this command.

    Raised before anything is sent, because the firmware reacts to an unknown
    command by showing an error glyph and stopping the display.
    """


class PerformanceWarning(UserWarning):
    """A setting that will not perform as intended.

    Issued (with :mod:`warnings`, not raised) for example when frames are
    requested faster than the controller's refresh rate can display them, or
    when the refresh rate is above what the panels or the SPI link can use.
    """
