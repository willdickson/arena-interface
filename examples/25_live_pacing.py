"""Closed loop from the computer: choose each frame live at a fixed rate.

Shows: dev.pace() ticking at a fixed rate; on every tick the script reads
Analog In 1 and moves the pattern by a step proportional to the voltage,
then shows that frame. tick.skipped says when the loop fell behind, and
pacer.report summarises the timing.
Requires: a controller with a pattern on its SD card and the extended I/O
capability (or --simulate, which drives Analog In 1 with a slow sine wave).
"""

import argparse
import math

from arena_interface import Device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--pattern", type=int, default=1, help="pattern id (1-based)")
    parser.add_argument("--rate", type=float, default=100.0, help="frames per second")
    parser.add_argument("--seconds", type=float, default=2.0, help="how long to run")
    parser.add_argument(
        "--gain", type=float, default=2.0, help="frames per tick per volt"
    )
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        dev.show_pattern(args.pattern)
        frames = dev.frame_position.frame_count
        position = 0.0
        pacer = dev.pace(args.rate, duration=args.seconds, late="skip")
        for tick in pacer:
            if dev.simulator is not None:  # pretend something drives the input
                dev.simulator.analog_in = (round(3000 * math.sin(tick.index / 40)), 0)
            volts = dev.io.read_analog_in().ain1_mv / 1000
            position += args.gain * volts * (1 + tick.skipped)  # catch up after skips
            dev.show_frame(int(position) % frames)
        print(pacer.report)
        print(f"final frame {dev.frame_position.index} of {frames}")


if __name__ == "__main__":
    main()
