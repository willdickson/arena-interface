"""Run trials from a pattern stored on the SD card.

Shows: start_trial() in open loop (forwards and backwards), closed loop and
with a controller-timed duration; polling frame_position.
Requires: a controller with at least one pattern on its SD card (or
--simulate, which provides demo patterns).
"""

import argparse
import time

from arena_interface import Device, TrialMode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--pattern", type=int, default=1, help="pattern id (1-based)")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        print(f"Pattern {args.pattern}: {dev.patterns.name(args.pattern)}")

        # Open loop: the controller advances frames by itself.
        dev.start_trial(args.pattern, TrialMode.OPEN_LOOP, frame_rate=30)
        for _ in range(3):
            time.sleep(0.2)
            print(f"  forwards  {dev.frame_position}")

        dev.start_trial(args.pattern, TrialMode.OPEN_LOOP, frame_rate=-30)
        time.sleep(0.2)
        print(f"  backwards {dev.frame_position}")

        # Closed loop: frame rate = Analog In 1 voltage x gain / 10.
        dev.start_trial(args.pattern, TrialMode.CLOSED_LOOP, gain=20)
        print("  closed loop running (drive Analog In 1 to move the pattern)")
        time.sleep(0.2)

        # A duration makes the controller stop the trial on its own.
        dev.start_trial(args.pattern, TrialMode.OPEN_LOOP, frame_rate=60, duration=0.5)
        time.sleep(0.7)
        print("  timed trial finished; the arena is dark again")


if __name__ == "__main__":
    main()
