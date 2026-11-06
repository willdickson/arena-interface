"""Send images from the host straight to the arena.

Shows: stream_image() with a moving bar in a simple timed loop. Streamed
frames need no SD card. (Precisely paced playback is a planned feature; this
loop just sleeps between frames.)
Requires: a connected controller (or use --simulate).
"""

import argparse
import time

from arena_interface import Device, GrayLevel


def bar(height: int, width: int, x0: int, bar_width: int = 20) -> list[list[int]]:
    """An image with a vertical bright bar starting at column x0."""
    row = [1 if (x - x0) % width < bar_width else 0 for x in range(width)]
    return [list(row) for _ in range(height)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--fps", type=float, default=30.0, help="frames per second")
    parser.add_argument("--seconds", type=float, default=2.0, help="how long to run")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        height, width = dev.arena.pixel_shape
        frames = int(args.fps * args.seconds)
        start = time.monotonic()
        for n in range(frames):
            dev.stream_image(bar(height, width, x0=n * 4), gray_level=GrayLevel.GS2)
            time.sleep(max(0.0, start + (n + 1) / args.fps - time.monotonic()))
        elapsed = time.monotonic() - start
        print(
            f"Streamed {frames} frames in {elapsed:.2f} s ({frames / elapsed:.1f} fps)"
        )


if __name__ == "__main__":
    main()
