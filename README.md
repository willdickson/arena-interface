# arena-interface

Python host library for the **G6 arena controller** (Teensy 4.1) over USB
serial. It replaces the JavaScript host library with a blocking, fully
type-hinted Python API for scripts and notebooks.

**Documentation:** https://willdickson.github.io/arena-interface/

```python
from arena_interface import Device, TrialMode

with Device.open() as dev:                       # finds the controller by USB id
    print(dev.firmware_version)                  # e.g. "12bba7e9 2x10 2026-11-02 main"
    dev.start_trial(1, TrialMode.OPEN_LOOP, frame_rate=60, duration=5)
```

## Features

- Every firmware command: display control, trials from the SD card,
  host-chosen frames, streamed frames, the SD pattern library, analog and
  digital I/O, diagnostics, the telemetry log and panel firmware updates.
- Patterns from pixel arrays (numpy arrays or nested lists): `.pat` files and stream
  frames for any arena preset or custom grid, byte-identical to the
  JavaScript tools.
- Host-paced playback of frame sequences (SD frames or streamed images),
  blocking, in the background, or live with a fixed-rate clock, with a
  timing report.
- An event stream of what the arena actually displayed, timed by the
  controller, and fixed-rate sampling of any reading.
- Closing a connection blanks the arena; commands the firmware does not
  support, or that would stop the display, are refused before sending.
- A simulated controller that behaves like the firmware, so everything runs
  without hardware.

Requires Python 3.11+ on Linux, macOS or Windows. Dependencies:
pyserial and numpy, installed automatically.

## Install

From a copy of this repository:

```bash
uv sync                    # development environment (.venv)
```

or, into another environment:

```bash
pip install /path/to/arena-interface
```

On Linux, install PJRC's Teensy udev rules so your user can open the port;
see [Getting started](https://willdickson.github.io/arena-interface/getting-started/).

## Examples

`examples/` has 25 runnable scripts, a live matplotlib plot and a Jupyter
notebook. Each accepts `--port` (default: auto-detect) and `--simulate`:

```bash
uv run python examples/01_connect.py --simulate    # no hardware needed
uv run python examples/06_trials.py                # the real controller
```

See [examples/README.md](examples/README.md) for the full list.

## Documentation

The documentation is published at
**https://willdickson.github.io/arena-interface/**: getting started, user
guide, examples, protocol reference and API reference.

Its source is in `docs/` and builds with MkDocs:

```bash
uv run mkdocs serve        # local preview at http://127.0.0.1:8000
uv run mkdocs gh-deploy --strict   # publish to GitHub Pages
```

The wire protocol, including where the firmware and the JavaScript host
differ, is described in the
[protocol reference](https://willdickson.github.io/arena-interface/PROTOCOL/).

## Tests

```bash
uv run pytest              # everything except hardware tests; no device needed
```

The tests use the simulated controller (`arena_interface.testing`) and also
run every example with `--simulate`.

Tests against a real controller are marked `hardware` and skipped by
default:

```bash
uv run pytest -m hardware
ARENA_LOOPBACK=1 uv run pytest -m hardware      # with an Analog Out -> In 1 cable
```

| Variable | Effect |
|---|---|
| `ARENA_PORT` | Serial port to use (default: auto-detect) |
| `ARENA_NO_DAC=1` | Skip analog-output tests (unit without a working DAC) |
| `ARENA_LOOPBACK=1` | Run the loopback linearity test (needs the cable) |
| `ARENA_ALLOW_RESET=1` | Run the test that reboots the controller |

The hardware tests leave the arena dark, do not change the refresh rate or
Digital IO roles, and delete the one temporary pattern (`zz_hwtest.pat`)
they upload.

## Development

```bash
uv run ruff check . && uv run ruff format .     # lint and format
uv run mypy src                                 # type check (strict)
uv run mkdocs build --strict                    # docs must build cleanly
uv build                                        # wheel and sdist in dist/
```

Layout: `src/arena_interface/` (the library), `tests/`, `examples/`, `docs/`.
`reference/` holds the controller firmware, panel firmware and JavaScript
host sources the library was derived from (read-only). Project conventions
are in `CLAUDE.md`; progress is tracked in `docs/PLAN.md`.

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Will Dickson.
