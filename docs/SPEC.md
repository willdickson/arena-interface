# Specification

## Goal
A Pythonic library to control and communicate with the G6 arena controller over its
USB serial interface, replacing the existing JavaScript host library.

## Inputs
- `reference/firmware/`: Teensy 4.1 serial interface (.h/.cpp)
- `reference/js/`: existing JavaScript host implementation

The new library is a reimplementation, not a line-by-line port.

## Users and use cases
- Scripts and notebooks that configure the device and read data from it.
- <replace with real examples, e.g. "stream sensor readings to a CSV",
  "run a calibration">

## Functional requirements
1. Discover and connect to the device (by port name, or auto-detect by USB VID/PID).
2. Implement every command/message the firmware supports (see PROTOCOL.md).
3. Support streaming / periodic data / async notifications if the firmware sends them.
4. Clean shutdown: closing the connection leaves the device in a safe state.

## API sketch (illustrative, not binding)

```python
from arena_interface import Device

with Device.open() as dev:              # auto-detect
    print(dev.firmware_version)
    dev.led.brightness = 128
    for sample in dev.stream(rate_hz=100):
        print(sample.timestamp, sample.value)
```

## Non-functional requirements
- Uses pyserial and numpy only; blocking API (async is out of scope for v1).
- Sensible timeouts; no hangs if the device disappears (raise `DeviceDisconnected`).
- Works on Linux, macOS, Windows.
- Thread-safety: state clearly in the docs whether the Device may be shared
  between threads.
- Python >= 3.11, fully type-hinted.

## Project tooling
- uv-based project (`pyproject.toml`, `uv.lock`, src layout).
- Dev tools: pytest, ruff, mypy.
- Docs: MkDocs with the Material theme and mkdocstrings (Python).

## Out of scope
- GUI, firmware changes, async API (revisit later).

## Documentation (MkDocs Material)
- Pages: Home, Getting started, User guide, Examples, Protocol reference, API reference.
- API reference generated with mkdocstrings from docstrings.
- Every public function/class has a docstring with a short example where useful.

## Examples
Provide an extensive, tested set of examples covering the whole public API.

### Required examples (adjust to the device's real features)
Getting started
1. Connect with auto-detect, print firmware version, disconnect
2. Connect by explicit port name; list available ports
3. Context manager usage vs. manual open/close

Core usage
4. Read device info/status
5. Read and write each configurable setting (one example per feature group)
6. Send each command type and handle its response
7. Stream data for N seconds; write to CSV
8. Handle asynchronous/device-initiated messages

Robustness
9. Error handling: timeouts, device disconnect, bad responses (custom exceptions)
10. Reconnect after the device is unplugged/reset
11. Using the library from multiple threads (if supported)

Recipes
12. Live plotting with matplotlib (optional dependency, in its own folder)
13. Logging all traffic for debugging (raw bytes and decoded messages)
14. Calibration or other multi-step workflow
15. Jupyter notebook walkthrough (`examples/notebooks/`)

### Requirements
- Every public feature has at least one example.
- Examples run in tests with the fake port; hardware use is optional.
- `docs/examples.md` lists each example with a short description and the
  embedded source.
- Before writing examples, propose the final list mapped to the features in
  PROTOCOL.md and wait for approval.

## Open questions
- <anything you're unsure about, so Claude asks instead of guessing>
