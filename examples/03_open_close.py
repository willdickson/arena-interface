"""Context manager versus manual open/close, and leaving the display on.

Shows: `with Device.open(...)`, try/finally with close(), is_open, and
all_off_on_close=False to keep a stimulus running after the script ends.
Requires: a connected controller (or use --simulate).
"""

import argparse

from arena_interface import Device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    # 1. Context manager: the arena is blanked (ALL_OFF) and the port released
    #    when the block ends, even if an exception is raised inside it.
    with Device.open(args.port, simulate=args.simulate) as dev:
        dev.all_on()
        print(f"inside the block: open={dev.is_open}")
    print(f"after the block:  open={dev.is_open}")

    # 2. Manual: the same, written out.
    dev = Device.open(args.port, simulate=args.simulate)
    try:
        dev.all_on()
    finally:
        dev.close()  # safe to call twice, and after the device has gone

    # 3. Leave the arena lit when the script exits.
    with Device.open(args.port, simulate=args.simulate, all_off_on_close=False) as dev:
        dev.all_on()
    print("arena left on; run ALL_OFF (e.g. example 05) to blank it")


if __name__ == "__main__":
    main()
