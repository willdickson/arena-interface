"""Recover from a controller reset or an unplugged cable.

Shows: reset() (reboots the controller and closes the connection),
reconnect() (waits for the same controller to come back, checked by its MAC
address), and catching DeviceDisconnected to reconnect after an unplug.
Requires: a connected controller (or --simulate, which fakes the unplug).
On hardware, unplug and replug the USB cable when asked.
"""

import argparse

from arena_interface import Device, DeviceDisconnected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        print(f"Connected to {dev.info.mac} on {dev.port_name}")

        dev.reset()
        print(f"Reset sent; connection open: {dev.is_open}")
        dev.reconnect(timeout=15)
        print(f"Back after reset; uptime {dev.diagnostics.health().uptime_ms} ms")

        if dev.simulator is not None:
            dev.simulator.unplug()
        else:
            input("Unplug the USB cable, then press Enter... ")
        try:
            dev.all_off()
        except DeviceDisconnected as exc:
            print(f"Lost the device: {exc}")
            if dev.simulator is None:
                print("Plug the cable back in (waiting up to 30 s)")
            dev.reconnect(timeout=30)
            dev.all_off()
            print("Reconnected and working again")


if __name__ == "__main__":
    main()
