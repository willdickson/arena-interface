# API reference

Everything below is importable from the package itself, e.g.
`from arena_interface import Device, Pattern, TrialMode`. The modules are
listed here only to group the reference by topic.

## Connecting and controlling

::: arena_interface.device
    options:
      members:
        - Device
        - PatternLibrary
        - PatternEntry
        - IO
        - Diagnostics
        - PanelFirmware
        - PanelCheck

::: arena_interface.discovery
    options:
      members:
        - find_ports
        - PortInfo
        - TEENSY_VID
        - TEENSY_PID

## Patterns and arenas

::: arena_interface.pattern
    options:
      members:
        - Pattern
        - Origin

::: arena_interface.arena
    options:
      members:
        - Arena
        - PRESETS

## Host-paced playback

::: arena_interface.playback
    options:
      members:
        - PlaybackReport
        - Playback
        - Pacer
        - Tick
        - LatePolicy

## Streaming

::: arena_interface.streaming
    options:
      members:
        - Telemetry
        - EventStream
        - TelemetryEvent
        - Sampler
        - Sample

::: arena_interface.telemetry
    options:
      members:
        - CommandRecord
        - FrameRecord
        - StateRecord
        - UnknownRecord
        - StateKind
        - TelemetryBlock

## Values returned by the device

::: arena_interface.messages
    options:
      show_root_heading: false
      members:
        - ControllerInfo
        - FirmwareVersion
        - FramePosition
        - PatternInfo
        - AnalogInputs
        - DioPort
        - Health
        - WatchdogStatus
        - HealthOp
        - IsrId
        - CrashReport
        - SdInfo
        - FirmwareImageInfo

## Enumerations and flags

::: arena_interface.protocol
    options:
      show_root_heading: false
      members:
        - TrialMode
        - PanelDisplayMode
        - GrayLevel
        - ArenaState
        - DioRole
        - AoMode
        - AoLutMode
        - Capability
        - FirmwareFlag
        - ErrorCode

## Exceptions

::: arena_interface.errors

## Simulated controller

::: arena_interface.testing
