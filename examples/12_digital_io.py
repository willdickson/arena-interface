"""Digital IO BNC ports: roles, outputs and levels.

Shows: io.dio_roles(), io.set_dio_role(), io.set_digital_out() and
io.read_digital(), including the refusal to drive a port configured as an
input.
Requires: a controller with the extended I/O capability (or --simulate).
Port 1 is driven high, then low, while this runs.
"""

import argparse

from arena_interface import CommandError, Device, DioRole


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        io = dev.io
        for number, port in enumerate(io.dio_roles(), start=1):
            level = "high" if port.high else "low"
            print(f"Port {number}: {port.role.name:20} level={level}")

        io.set_dio_role(1, DioRole.OUT_PROGRAMMABLE)
        io.set_digital_out(1, True)
        print(f"Port 1 driven high; levels now {io.read_digital()}")
        io.set_digital_out(1, False)

        # Port 2 boots as the external-trigger input; driving it is refused.
        roles = io.dio_roles()
        if roles[1].role is DioRole.IN_TRIGGER:
            try:
                io.set_digital_out(2, True)
            except CommandError as exc:
                print(f"Refused as expected: {exc.message}")


if __name__ == "__main__":
    main()
