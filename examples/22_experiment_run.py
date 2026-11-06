"""A complete experiment run: upload a stimulus, run timed trials, save logs.

Shows: a multi-step workflow combining Pattern, patterns.upload(overwrite=True),
start_trial() with a controller-timed duration, and telemetry.events() to
save, per trial, the frames the arena actually showed.
Requires: a controller with an SD card and telemetry (or --simulate).
Writes one CSV per trial into experiment_logs/.
"""

import argparse
import csv
from pathlib import Path

from arena_interface import Arena, Device, FrameRecord, Pattern, TrialMode

TRIALS = [("slow", 10), ("fast", 40), ("reverse", -20)]  # (name, frames per second)


def make_stimulus(arena: Arena) -> Pattern:
    """A 20-frame moving bar sized for the controller's grid."""
    h, w = arena.pixel_shape
    frames = [
        [[15 if (x - f * 10) % w < 20 else 0 for x in range(w)] for _ in range(h)]
        for f in range(20)
    ]
    return Pattern.from_frames(frames, arena=arena)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--seconds", type=float, default=1.0, help="trial length")
    parser.add_argument("--output", type=Path, default=Path("experiment_logs"))
    args = parser.parse_args()
    args.output.mkdir(exist_ok=True)

    with Device.open(args.port, simulate=args.simulate) as dev:
        dev.stop()
        pid = dev.patterns.upload(
            "moving_bar.pat", make_stimulus(dev.arena), overwrite=True
        )
        dev.telemetry.drain()  # start the logs from here

        for name, rate in TRIALS:
            dev.start_trial(
                pid, TrialMode.OPEN_LOOP, frame_rate=rate, duration=args.seconds
            )
            path = args.output / f"trial_{name}.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["time_s", "frame"])
                for event in dev.telemetry.events(duration=args.seconds + 0.2):
                    if isinstance(event.record, FrameRecord):
                        writer.writerow([f"{event.time:.6f}", event.record.index])
            print(f"trial {name:8} ({rate:+} fps) -> {path}")


if __name__ == "__main__":
    main()
