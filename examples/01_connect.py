"""Connect to the device, print its firmware version, and disconnect.

Shows: Device.open(), the context manager, auto-detection.
Requires: a connected device (or use --simulate).
"""

import argparse

from arena_interface import Device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        print(f"Connected. Firmware version: {dev.firmware_version}")


if __name__ == "__main__":
    main()
