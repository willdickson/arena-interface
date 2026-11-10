"""Update the panels' own firmware through the controller.

Shows: panel_firmware.info(), upload() (with CRC check), verify() and
program(). Programming is only done when you ask for it with --program.
Requires: a controller with an SD card and a panel image file (--image), or
--simulate, which uses a dummy image. The display is stopped first.
"""

import argparse
from pathlib import Path

from arena_interface import Device


def dummy_image() -> bytes:
    """A stand-in image with the 32-byte footer the controller reads."""
    return bytes(8192) + b"G6PANEL\0" + b"sim-1.0".ljust(16, b"\0") + bytes(8)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--image", type=Path, help="panel firmware image to upload")
    parser.add_argument("--program", type=int, metavar="PANEL", help="flash this panel")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        dev.stop()
        fw = dev.panel_firmware
        print(f"Stored image: {fw.info() or 'none'}")

        image = (
            args.image.read_bytes()
            if args.image
            else (dummy_image() if args.simulate else None)
        )
        if image is None:
            print("Pass --image FILE to upload a panel image.")
            return
        crc = fw.upload(image)
        print(f"Uploaded {len(image)} bytes, CRC-32 0x{crc:08x}: {fw.info()}")

        panel = args.program or (1 if args.simulate else None)
        if panel is None:
            print("Pass --program N to flash panel N (the first panel is 1).")
            return
        print(f"Before: {fw.verify(panel)}")
        print(f"Programming panel {panel}: {fw.program(panel)}")
        print(f"After:  {fw.verify(panel)}")


if __name__ == "__main__":
    main()
