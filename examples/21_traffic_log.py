"""Log every byte exchanged with the controller, for debugging.

Shows: the library's loggers. ``arena_interface.transport`` logs raw bytes
at DEBUG level ("->" sent, "<-" received, with status and opcode);
``arena_interface.firmware`` logs the controller's own debug text (debug
firmware builds only). Decoded results are printed by this script.
Requires: a connected controller (or use --simulate).
"""

import argparse
import logging

from arena_interface import Device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(name)-26s %(levelname)-7s %(message)s",
        level=logging.WARNING,  # everything else stays quiet
    )
    logging.getLogger("arena_interface.transport").setLevel(logging.DEBUG)
    logging.getLogger("arena_interface.firmware").setLevel(logging.INFO)

    with Device.open(args.port, simulate=args.simulate) as dev:
        print(f"decoded: refresh rate = {dev.refresh_rate} Hz")
        print(f"decoded: pattern 1 = {dev.patterns.name(1)}")
        print(f"decoded: frame position = {dev.frame_position}")


if __name__ == "__main__":
    main()
