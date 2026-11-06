"""Log what the arena actually displayed, from the controller's own record.

Shows: telemetry.events() during an open-loop trial, FrameRecord and
StateRecord events with wall-clock times, and writing a frame log to CSV.
The controller timestamps each frame change itself, so the log is more
accurate than timing on the host.
Requires: a controller with telemetry and a pattern on its SD card (or
--simulate). Writes frame_log.csv.
"""

import argparse
import csv
from pathlib import Path

from arena_interface import Device, FrameRecord, StateRecord, TrialMode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--seconds", type=float, default=1.0, help="trial length")
    parser.add_argument("--output", type=Path, default=Path("frame_log.csv"))
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        dev.telemetry.drain()  # discard anything logged before now
        dev.start_trial(1, TrialMode.OPEN_LOOP, frame_rate=20, duration=args.seconds)

        frames = 0
        with args.output.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "pattern", "frame"])
            stream = dev.telemetry.events(duration=args.seconds + 0.3)
            for event in stream:
                rec = event.record
                if isinstance(rec, FrameRecord):
                    writer.writerow([f"{event.time:.6f}", rec.pattern_id, rec.index])
                    frames += 1
                elif isinstance(rec, StateRecord) and rec.new_state is not None:
                    print(f"{event.time:.3f}  state -> {rec.new_state.name}")
    print(f"Logged {frames} frame changes to {args.output} (lost: {stream.lost})")


if __name__ == "__main__":
    main()
