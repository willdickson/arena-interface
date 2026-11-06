"""Build a pattern from pixel arrays and save it as a .pat file.

Shows: Pattern.from_frames() with nested lists (numpy arrays work too), an
arena preset, gray levels, duty cycle, origin="lower" for bottom-origin data,
to_bytes() and reading the file back with Pattern.from_bytes(). Upload the
result with device.patterns.upload("grating.pat", pattern).
Requires: nothing (no controller needed). Writes grating.pat.
"""

import argparse
from pathlib import Path

from arena_interface import Arena, GrayLevel, Pattern


def grating(height: int, width: int, shift: int, period: int = 20) -> list[list[int]]:
    """A triangle-wave grating with 16 brightness levels, moved by `shift` pixels."""
    wave = [round(15 * (1 - abs(2 * i / period - 1))) for i in range(period)]
    row = [wave[(x + shift) % period] for x in range(width)]
    return [list(row) for _ in range(height)]


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
    marker = [[0] * width for _ in range(height)]
    marker[0][0] = 15
    top = Pattern.from_frames([marker], arena=arena)
    bottom = Pattern.from_frames([marker], arena=arena, origin="lower")
    row_top = [r[0] for r in top.frame(0)].index(15)
    row_bottom = [r[0] for r in bottom.frame(0)].index(15)
    print(f"marker in row {row_top} vs {row_bottom} (rows counted from the top)")

    again = Pattern.from_bytes(args.output.read_bytes())
    print(f"Read back: {again}; identical: {again == pattern}")


if __name__ == "__main__":
    main()
