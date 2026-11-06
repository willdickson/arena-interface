"""Python host library for the G6 arena controller over USB serial.

```python
from arena_interface import Device

with Device.open() as dev:
    print(dev.firmware_version)
```
"""

from .arena import PRESETS, Arena
from .device import (
    IO,
    Device,
    Diagnostics,
    PanelCheck,
    PanelFirmware,
    PatternEntry,
    PatternLibrary,
)
from .discovery import PortInfo, find_ports
from .errors import (
    CommandError,
    DeviceDisconnected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    PerformanceWarning,
    ProtocolError,
    UnsupportedCommand,
)
from .messages import (
    AnalogInputs,
    ControllerInfo,
    CrashReport,
    DioPort,
    FirmwareImageInfo,
    FirmwareVersion,
    FramePosition,
    Health,
    PatternInfo,
    SdInfo,
)
from .pattern import Pattern
from .playback import LatePolicy, Pacer, Playback, PlaybackReport, Tick
from .protocol import (
    AoLutMode,
    AoMode,
    ArenaState,
    Capability,
    DioRole,
    ErrorCode,
    FirmwareFlag,
    GrayLevel,
    PanelDisplayMode,
    TrialMode,
)
from .streaming import EventStream, Sample, Sampler, Telemetry, TelemetryEvent
from .telemetry import (
    CommandRecord,
    FrameRecord,
    StateKind,
    StateRecord,
    TelemetryBlock,
    UnknownRecord,
)

__all__ = [
    "PerformanceWarning",
    "Tick",
    "PlaybackReport",
    "Playback",
    "Pacer",
    "LatePolicy",
    "IO",
    "PRESETS",
    "AnalogInputs",
    "AoLutMode",
    "AoMode",
    "Arena",
    "ArenaState",
    "Capability",
    "CommandError",
    "CommandRecord",
    "ControllerInfo",
    "CrashReport",
    "Device",
    "DeviceDisconnected",
    "DeviceError",
    "DeviceNotFound",
    "DeviceTimeout",
    "Diagnostics",
    "DioPort",
    "DioRole",
    "EventStream",
    "ErrorCode",
    "FirmwareFlag",
    "FirmwareImageInfo",
    "FirmwareVersion",
    "FramePosition",
    "FrameRecord",
    "GrayLevel",
    "Health",
    "PanelCheck",
    "PanelDisplayMode",
    "PanelFirmware",
    "PatternEntry",
    "PatternInfo",
    "Pattern",
    "PatternLibrary",
    "PortInfo",
    "Sample",
    "Sampler",
    "ProtocolError",
    "SdInfo",
    "StateKind",
    "StateRecord",
    "TelemetryBlock",
    "Telemetry",
    "TelemetryEvent",
    "TrialMode",
    "UnknownRecord",
    "UnsupportedCommand",
    "find_ports",
]

__version__ = "0.1.0"
