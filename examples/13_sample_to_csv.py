"""Record the analog inputs at a fixed rate into a CSV file.

Shows: device.sample() with a read method, wall-clock timestamps, and the
count of readings skipped because the computer fell behind.
Requires: a controller with the extended I/O capability (or --simulate).
Writes analog_samples.csv.
"""

import argparse
import csv
from pathlib import Path

from arena_interface import Device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--rate", type=float, default=100.0, help="samples per second")
    parser.add_argument("--seconds", type=float, default=2.0, help="how long to record")
    parser.add_argument("--output", type=Path, default=Path("analog_samples.csv"))
    args = parser.parse_args()

    with (
        Device.open(args.port, simulate=args.simulate) as dev,
        args.output.open("w", newline="") as f,
    ):
        writer = csv.writer(f)
        writer.writerow(["time_s", "ain1_mv", "ain2_mv"])
        sampler = dev.sample(
            dev.io.read_analog_in, rate_hz=args.rate, duration=args.seconds
        )
        rows = 0
        for s in sampler:
            writer.writerow([f"{s.time:.6f}", s.value.ain1_mv, s.value.ain2_mv])
            rows += 1
    print(f"Wrote {rows} samples to {args.output} ({sampler.skipped} skipped)")


if __name__ == "__main__":
    main()
