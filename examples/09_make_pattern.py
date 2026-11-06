"""Build a pattern from pixel arrays and save it as a .pat file.

Shows: Pattern.from_frames() with numpy arrays (nested lists work too), an
arena preset, gray levels, duty cycle, origin="lower" for bottom-origin data,
to_bytes() and reading the file back with Pattern.from_bytes(). Upload the
result with device.patterns.upload("grating.pat", pattern).
Requires: nothing (no controller needed). Writes grating.pat.
"""

import argparse
from pathlib import Path

import numpy as np

from arena_interface import Arena, GrayLevel, Pattern


def grating(height: int, width: int, shift: int, period: int = 20) -> np.ndarray:
    """A triangle-wave grating with 16 brightness levels, moved by `shift` pixels."""
    phase = ((np.arange(width) + shift) % period) / period
    row = np.round(15 * (1 - np.abs(2 * phase - 1))).astype(np.uint8)
    return np.tile(row, (height, 1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (unused: no device needed)")
    parser.add_argument(
        "--simulate", action="store_true", help="accepted for consistency"
    )
    parser.add_argument("--arena", default="G6_2x10", help="arena preset name")
    parser.add_argument("--output", type=Path, default=Path("grating.pat"))
    args = parser.parse_args()

    arena = Arena.preset(args.arena)
    height, width = arena.pixel_shape
    frames = [grating(height, width, shift) for shift in range(20)]

    pattern = Pattern.from_frames(
        frames, gray_level=GrayLevel.GS16, arena=arena, duty_cycle=128
    )
    args.output.write_bytes(pattern.to_bytes())
    print(f"Wrote {args.output}: {pattern}")

    # Orientation: by default row 0 is the TOP of the arena. Data whose row 0 is
    # the bottom (MATLAB / JS convention) needs origin="lower".
    marker = np.zeros((height, width), dtype=np.uint8)
    marker[0, 0] = 15
    top = Pattern.from_frames([marker], arena=arena)
    bottom = Pattern.from_frames([marker], arena=arena, origin="lower")
    row_top = int(np.argmax(top.frame(0)[:, 0]))  # frame() returns a numpy array
    row_bottom = int(np.argmax(bottom.frame(0)[:, 0]))
    print(f"marker in row {row_top} vs {row_bottom} (rows counted from the top)")

    again = Pattern.from_bytes(args.output.read_bytes())
    print(f"Read back: {again}; identical: {again == pattern}")


if __name__ == "__main__":
    main()
