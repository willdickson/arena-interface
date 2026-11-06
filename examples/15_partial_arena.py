"""Arena presets, partial arenas, and rigs smaller than the firmware's grid.

Shows: the built-in Arena presets, installed columns, drawing on a rig's full
grid, Pattern.fit_to() onto a controller's panel grid (e.g. a 3x10 rig on
4x10 firmware), and stream_image() with arena=, which fits automatically.
Requires: a connected controller (or use --simulate).
"""

import argparse

from arena_interface import PRESETS, Arena, Device, GrayLevel, Pattern


def columns_lit(rig: Arena) -> list[list[int]]:
    """Light every installed column; uninstalled grid columns stay blank."""
    height, width = rig.pixel_shape
    return [
        [1 if x // 20 in rig.columns else 0 for x in range(width)]
        for _ in range(height)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--rig", default="G6_2x8of10", help="arena preset of the rig")
    args = parser.parse_args()

    for name, arena in PRESETS.items():
        print(
            f"{name:14} id {arena.arena_id}  {arena.rows}x{arena.cols} grid, "
            f"columns {arena.columns[0]}-{arena.columns[-1]}"
        )

    # Offline: a 3x10 rig drawn at its own size, placed on a 4x10 firmware grid.
    small = Arena.preset("G6_3x10")
    pattern = Pattern.from_frames(
        [columns_lit(small)], gray_level=GrayLevel.GS2, arena=small
    )
    fitted = pattern.fit_to(4, 10)
    print(f"{pattern} -> fitted for 4x10 firmware: {fitted}")

    # On the device: stream an image drawn for the rig; it is fitted to the
    # controller's grid (an error explains it if the rig cannot fit).
    rig = Arena.preset(args.rig)
    with Device.open(args.port, simulate=args.simulate) as dev:
        installed = len(rig.columns)
        print(f"Controller grid: {dev.arena_shape}; rig: {rig} ({installed} columns)")
        dev.stream_image(columns_lit(rig), gray_level=GrayLevel.GS2, arena=rig)
        print("Streamed the rig image")


if __name__ == "__main__":
    main()
