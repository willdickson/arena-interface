"""Play frame sequences at a fixed rate, paced by the computer.

Shows: play_frames() (frames of an SD pattern, with verify=True to check what
the controller displayed), play_images() (streamed images), and
start_frames() running in the background while the script does other work.
Requires: a controller with a pattern of 20+ frames on its SD card (or
--simulate).
"""

import argparse
import time

from arena_interface import Device, GrayLevel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--pattern", type=int, default=1, help="pattern id (1-based)")
    parser.add_argument("--fps", type=float, default=100.0, help="frames per second")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        # 1. Frames of an SD pattern: forwards, then backwards.
        dev.show_pattern(args.pattern)
        n = dev.frame_position.frame_count
        frames = [*range(n), *reversed(range(n))]
        report = dev.play_frames(frames, fps=args.fps, late="skip", verify=True)
        print(f"SD frames:  {report}")

        # 2. Streamed images: a bar moving across the arena.
        h, w = dev.arena.pixel_shape
        images = [
            [[1 if (x - 8 * f) % w < 20 else 0 for x in range(w)] for _ in range(h)]
            for f in range(w // 8)
        ]
        report = dev.play_images(images, fps=args.fps, gray_level=GrayLevel.GS2)
        print(f"Streamed:   {report}")

        # 3. In the background: the script keeps working meanwhile.
        with dev.start_frames(frames, fps=args.fps) as player:
            while player.running:
                print(f"  background progress {player.progress}/{player.total}")
                time.sleep(0.2)
        print(f"Background: {player.report}")


if __name__ == "__main__":
    main()
