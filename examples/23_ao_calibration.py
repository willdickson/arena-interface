"""Calibrate Analog In 1 against the analog output with a loopback cable.

Shows: a measurement workflow: step io.analog_out, read io.read_analog_in()
several times at each level, fit gain and offset by least squares, and save
the table. Analog inputs are uncalibrated in firmware, so a fit like this
converts their readings to real millivolts.
Requires: a BNC cable from the analog output to Analog In 1 (or --simulate,
which fakes the cable with a small gain and offset error).
Writes ao_calibration.csv.
"""

import argparse
import csv
import statistics
from pathlib import Path

from arena_interface import AoMode, Device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument(
        "--steps", type=int, default=11, help="levels from 0 to 5000 mV"
    )
    parser.add_argument("--output", type=Path, default=Path("ao_calibration.csv"))
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        if dev.simulator is not None:
            dev.simulator.analog_loopback = (1.013, -27.0)  # gain, offset (mV)
        dev.io.set_analog_out_mode(AoMode.PROGRAMMABLE)
        table = []
        for i in range(args.steps):
            dev.io.analog_out = round(5000 * i / (args.steps - 1))
            out_mv = dev.io.analog_out  # what the DAC actually produces
            readings = [
                s.value.ain1_mv for s in dev.sample(dev.io.read_analog_in, 200, count=5)
            ]
            table.append((out_mv, statistics.mean(readings)))
        dev.io.analog_out = 0

    xs, ys = zip(*table, strict=True)
    gain, offset = statistics.linear_regression(xs, ys)  # ain1 = gain * out + offset
    residual = max(abs(y - (gain * x + offset)) for x, y in table)
    with args.output.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ao_mv", "ain1_mean_mv"])
        writer.writerows(table)
    print(f"Analog In 1 = {gain:.4f} x output {offset:+.1f} mV")
    print(f"Largest residual: {residual:.1f} mV")
    print(f"To correct a reading: true_mv = (ain1_mv {-offset:+.1f}) / {gain:.4f}")
    print(f"Table saved to {args.output}")


if __name__ == "__main__":
    main()
