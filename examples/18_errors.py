"""Handling errors: what each exception means and how to recover.

Shows: CommandError (the controller refused), ValueError (rejected locally
before sending), UnsupportedCommand (older firmware), DeviceTimeout (no
answer), and DeviceError as the catch-all base class.
Requires: a connected controller (or --simulate, which can also fake a
silent device; on hardware the timeout case is skipped).
"""

import argparse

from arena_interface import (
    CommandError,
    Device,
    DeviceError,
    DeviceTimeout,
    ErrorCode,
    UnsupportedCommand,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        dev.all_on()
        try:
            dev.patterns.download(1)  # transfers need the display off
        except CommandError as exc:  # exc.status says why: here DISPLAY_ACTIVE
            print(f"CommandError ({ErrorCode(exc.status).name}): {exc.message}")
        dev.stop()

        try:
            dev.refresh_rate = 0  # out of range
        except ValueError as exc:
            print(f"ValueError, nothing sent: {exc}")

        try:
            block = dev.telemetry.read_block()
            print(f"Telemetry available ({len(block.records)} records waiting)")
        except UnsupportedCommand as exc:
            print(f"UnsupportedCommand, nothing sent: {exc}")

        if dev.simulator is not None:
            dev.simulator.muted = True  # the fake device stops answering
            try:
                dev.all_off()
            except DeviceTimeout as exc:
                print(f"DeviceTimeout: {exc}")
            dev.simulator.muted = False

        try:
            dev.start_trial(999)  # no such pattern
        except DeviceError as exc:  # base class of every library error
            print(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
