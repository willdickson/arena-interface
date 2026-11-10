"""Analog output and inputs.

Shows: io.analog_out (set, and read back from the DAC), a lookup table that
follows the displayed frame, the frame-number output mode, and
io.read_analog_in().
Requires: a controller with the extended I/O capability and a pattern on
its SD card (or --simulate). Use a pattern with several frames to see the
table step. The analog output BNC changes voltage while this runs.
"""

import argparse

from arena_interface import AoLutMode, AoMode, Device, TrialMode


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
        io = dev.io
        io.set_analog_out_mode(AoMode.PROGRAMMABLE)
        io.analog_out = 2500
        print(f"Analog out set to 2500 mV, DAC reads back {io.analog_out} mV")

        # Lookup table: one level per frame index (up to 21 levels).
        io.load_analog_table([0, 1250, 2500, 3750, 5000], AoLutMode.FRAME_LOCKED)
        dev.show_pattern(args.pattern)
        for frame in range(min(5, dev.frame_position.frame_count)):
            dev.show_frame(frame)
            print(f"  frame {frame}: analog out {io.analog_out} mV")

        # Frame-number mode: 0 V at the first frame, 5 V at the last.
        io.set_analog_out_mode(AoMode.FRAME_NUMBER)
        dev.start_trial(args.pattern, TrialMode.SHOW_FRAME)
        dev.show_frame(dev.frame_position.frame_count - 1)
        print(f"Frame-number mode, last frame: {io.analog_out} mV")
        io.set_analog_out_mode(AoMode.PROGRAMMABLE)

        inputs = io.read_analog_in()
        print(f"Analog in: {inputs.ain1_mv} mV, {inputs.ain2_mv} mV")


if __name__ == "__main__":
    main()
