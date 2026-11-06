# arena-interface

A Python library for controlling the **G6 arena controller** (Teensy 4.1) over
its USB serial connection. It replaces the JavaScript host library with a
blocking, fully type-hinted Python API for scripts and notebooks.

```python
from arena_interface import Device, TrialMode

with Device.open() as dev:                      # finds the controller by USB id
    print(dev.firmware_version)                 # e.g. "12bba7e9 2x10 2026-11-02 main"
    dev.start_trial(1, TrialMode.OPEN_LOOP, frame_rate=60, duration=5)
```

## What it covers

- **Every firmware command**: display control, trials from the SD card,
  host-chosen frames, streamed frames, the SD pattern library, analog and
  digital I/O, health and crash diagnostics, the telemetry log, and panel
  firmware updates.
- **Patterns from pixel arrays**: build `.pat` files and stream frames from
  nested lists or numpy arrays, for any arena preset or custom grid. The
  output matches the JavaScript tools byte for byte.
- **Host-paced playback**: play frame sequences at a fixed rate (SD frames or
  streamed images), blocking, in the background, or live, with a timing
  report.
- **Continuous data**: an event stream of what the arena actually displayed,
  timed by the controller, and fixed-rate sampling of any reading.
- **Safety**: closing a connection blanks the arena; commands the firmware
  does not support, or that would stop the display because of a bad
  argument, are refused before anything is sent.
- **A simulated controller** that behaves like the firmware, so tests, examples
  and notebooks run without hardware.

## Where to go next

- [Getting started](getting-started.md): install, connect, first commands.
- [User guide](user-guide.md): how the controller works and how to use each
  part of the library.
- [Examples](examples.md): 25 runnable scripts, a live plot and a notebook.
- [Protocol reference](PROTOCOL.md): the wire protocol, for the curious and
  for firmware work.
- [API reference](api.md): every class and function.

## Requirements

- Python 3.11 or newer on Linux, macOS or Windows.
- [pyserial](https://pyserial.readthedocs.io/), installed automatically.
- Optional: numpy (faster pattern encoding, array input/output), matplotlib
  (plotting example), Jupyter (notebook example).
