"""Choose each displayed frame from the host.

Shows: show_pattern() (show-frame mode at frame 0), show_frame() stepping,
frame_position, and the local check that rejects an out-of-range frame
before it reaches the controller (which would stop the display).
Requires: a controller with at least one pattern on its SD card (or
--simulate).
"""

import argparse
import time

from arena_interface import Device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument(
        "--pattern",
        type=lambda s: int(s) if s.isdigit() else s,
        default=1,
        help="pattern file name (e.g. grating.pat) or id (first pattern = 1)",
    )
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        dev.show_pattern(args.pattern)
        count = dev.frame_position.frame_count
        print(f"Pattern {args.pattern} has {count} frames")

        for index in range(0, count, max(1, count // 8)):
            dev.show_frame(index)
            time.sleep(0.05)
            print(f"  showing {dev.frame_position.index}")

        try:
            dev.show_frame(count)  # one past the end
        except ValueError as exc:
            print(f"Rejected locally: {exc}")


if __name__ == "__main__":
    main()
