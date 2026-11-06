# Plan

Work one milestone at a time. Tick items off as they are completed.

- [x] M0. Scaffold: `uv init --lib`, add pyserial; dev deps pytest, ruff, mypy;
      docs deps mkdocs-material, mkdocstrings[python]; mkdocs.yml; verify every
      command in CLAUDE.md runs
- [x] M1. Read firmware + JS; write docs/PROTOCOL.md (framing, commands,
      responses, errors, timing, quirks, discrepancies).
      **STOP for my review before continuing.**
- [x] M2. Transport + framing layer with unit tests
- [x] M3. Message encode/decode (dataclasses/enums) with tests
- [x] M4. Fake serial port (`arena_interface.testing.FakePort`) that replays the
      firmware's responses, so tests and examples run without hardware
- [x] M5. High-level `Device` API, exceptions, auto-detect
- [x] M5a. Patterns from pixel arrays: `Arena` presets, `.pat` encode/decode,
      stream frames, `origin="upper"|"lower"`, rig → firmware-grid mapping;
      numpy optional extra.
  - [ ] M5a-yaml. Optional maDisplayTools YAML arena registry (PyYAML extra).
        Waiting for sample YAML files from the user.
- [x] M6. Streaming / notifications: telemetry event stream
      (`device.telemetry.events()`), polling sampler (`device.sample()`),
      wall-clock timestamps; telemetry moves out of `diagnostics`.
- [x] M6b. Host-paced frame playback: pre-computed sequences and live
      (closed-loop) pacing; SD frame positions and streamed images; late
      frames skipped or all sent (caller's choice) with timing feedback;
      blocking and background playback. No FicTrac bridge.
- [x] M7. Examples: propose list, **wait for my OK**, then write in groups
      (getting started, core, robustness, recipes). After each group run
      `uv run pytest tests/test_examples.py`
- [x] M8. Docs: getting started, user guide, protocol reference, API reference,
      examples page; `mkdocs build --strict` passes
- [x] M9. Optional: hardware tests (`-m hardware`), README polish, packaging check

## Status (updated 2026-11-04)

- Hardware check done (2×10 debug build `12bba7e9`, panels 4 and 5 fitted):
  connect, auto-detect, identity, health, crash report, SD info, pattern
  list (41 patterns), DIO roles, analog inputs, telemetry and 200 Hz sampling
  all work. Panel order across the arena and within panels confirmed.
  Found and fixed: telemetry times for records older than one controller
  clock wrap (71.6 min).
- The first arena's DAC chip is faulty (GET_AO_VOLTAGE: "I2C read failed");
  analog output was tested on the second arena instead.
- Live plot: Tk backend only (decided 2026-11-04; Qt could be an optional extra later).
- M8 docs done 2026-11-04.
- Second arena (working DAC; 2 rows, columns 1-6, 9, 10 = grid 0-5, 8, 9)
  checked 2026-11-04: column layout confirmed, DAC readback, lookup table,
  frame-number mode and loopback calibration (gain 1.044, offset +139 mV)
  all work. M9 hardware suite (`tests/test_hardware.py`, `-m hardware`):
  14 passed. It found a second telemetry timing bug (out-of-order CMD
  records), now fixed and reproduced in the simulator.
- Reset/reconnect hardware test (`ARENA_ALLOW_RESET=1`) not run: in this VM
  the USB passthrough is pinned to the bus/device address, so a reset would
  likely leave the controller on the host. Run it on a machine with native USB.
- M9 done 2026-11-04: README rewritten; `uv build` checked (wheel ships
  `py.typed`, sdist excludes `reference/`); wheel installed and used on a
  clean Python 3.10, with and without the numpy extra; full test suite and
  mypy also pass on Python 3.10.
- Minimum Python raised to 3.11 (2026-11-05; 3.10 is end of life, and 3.11's
  high-resolution sleep on Windows simplifies M6b playback timing).
- License MIT, author Will Dickson <wbd@caltech.edu> (LICENSE file and package
  metadata, 2026-11-04). No git repository yet, by the user's choice.
- Later: M5a-yaml; a Windows run of the hardware suite; the reset test on native USB.
- M5a-yaml is waiting for sample maDisplayTools YAML files.
- M6b done 2026-11-05: `playback.py` (play_frames/play_images, start_*,
  pace, PlaybackReport, PerformanceWarning), examples 24 and 25, user guide
  section; hardware playback tests pass (200 fps, no skips, display verified).
  Refresh-rate and streaming limits measured (PROTOCOL.md Timing).
- Not yet verified on hardware: row repetition vs. blank padding for rigs
  smaller than the firmware grid.
- All protocol decisions agreed in review are in `docs/PROTOCOL.md`
  § Host-side decisions; there are no open protocol questions.
