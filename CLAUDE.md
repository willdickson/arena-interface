# arena-interface

Python host library for the G6 arena controller firmware (Teensy 4.1) over USB serial.
See @docs/SPEC.md for requirements and docs/PROTOCOL.md for the wire protocol.
Progress is tracked in docs/PLAN.md.

## Commands
- Setup: `uv sync`
- Test: `uv run pytest`
- Lint/format: `uv run ruff check . && uv run ruff format .`
- Type check: `uv run mypy src`
- Docs preview: `uv run mkdocs serve`
- Docs build: `uv run mkdocs build --strict`

## Project rules
- Package manager is uv. Add deps with `uv add` (`uv add --dev` for dev tools).
  Never use pip or edit dependency lists in pyproject.toml by hand.
- Python >= 3.11, src layout (`src/arena_interface/`), fully type-hinted, include `py.typed`.
- Runtime dependencies are `pyserial` and `numpy` only.
- Docstrings: Google style (rendered by mkdocstrings).

## Reference material (read-only, do not modify)
- `reference/firmware/`: Teensy 4.1 implementation (.h/.cpp).
  **Source of truth for the protocol.**
- `reference/js/`: JavaScript host implementation. Use it to understand intended
  host behavior, but do NOT mirror its structure or naming.
- `reference/panel/`: G6 panel firmware. **Source of truth for how panel-block
  bytes are displayed** (pixel order, grayscale packing, duty cycle). Within a
  block the first 20 pixels are the panel's top row (confirmed on hardware).

## Code conventions
- Pythonic API: snake_case, context managers, properties, dataclasses/enums,
  exceptions rather than error codes, no callback-heavy style.
- All serial I/O goes through one transport class so it can be mocked in tests.
- Custom exceptions derive from `DeviceError` in `errors.py`.
- Tests must run without hardware, using a fake serial port
  (`arena_interface.testing.FakePort`).
  Hardware tests are marked `@pytest.mark.hardware` and skipped by default.

## Examples
- Every example in `examples/` is a complete, runnable script with a module
  docstring (what it shows, prerequisites) and a `main()` guarded by
  `if __name__ == "__main__":`.
- Examples use only the public API (`from arena_interface import ...`), never internals.
- Each example accepts `--port` (optional, defaults to auto-detect) via argparse,
  and `--simulate` to run against the fake port with no hardware.
- `tests/test_examples.py` runs every example with `--simulate`; a new or changed
  example is not done until that test passes.
- Docs embed examples with pymdownx.snippets (`--8<-- "examples/01_connect.py"`)
  rather than copying code into markdown.
- Keep each example short (aim for under 60 lines) and focused on one idea.
- Name files `NN_topic.py` and list each in `examples/README.md` and `docs/examples.md`.

## Workflow
- Work one milestone at a time from docs/PLAN.md and tick items off.
- Stop for my review where the plan says so.
- If firmware and JS disagree, follow the firmware and note it in docs/PROTOCOL.md.
- If something is ambiguous, ask rather than guess.
- Update docs when the public API changes. `mkdocs build --strict` must pass.
