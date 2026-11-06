"""List serial ports, then connect to one by name.

Shows: find_ports() (Teensy candidates and all ports), Device.open(port),
probe=False.
Requires: a connected controller (or use --simulate).
"""

import argparse

from arena_interface import Device, DeviceNotFound, find_ports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    ports = find_ports(all_ports=True)
    print("USB serial ports:")
    for port in (p for p in ports if p.vid is not None):
        print(f"  {port.device:15} {port.vid:04X}:{port.pid:04X}  {port.description}")
    print(f"({sum(p.vid is None for p in ports)} non-USB ports not shown)")

    candidates = find_ports()  # Teensy USB ids only; nothing is sent to them
    print(f"Teensy candidates: {[p.device for p in candidates] or 'none'}")

    if args.simulate:
        dev = Device.open(simulate=True)
    elif args.port:
        dev = Device.open(args.port)  # an explicit port skips detection
    else:
        try:
            # Without probing, more than one candidate is an error.
            dev = Device.open(probe=False)
        except DeviceNotFound as exc:
            print(f"Could not choose a port: {exc}")
            return

    with dev:
        print(f"Connected on {dev.port_name}: {dev.firmware_version}")


if __name__ == "__main__":
    main()
