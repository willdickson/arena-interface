"""Diagnose connection timeouts: log every byte, retry, and test the LEDs.

Run it while the controller is not answering (e.g. after powering the arena
on with the USB cable already connected):

    uv run python scripts/connect_debug.py            # auto-detect the port
    uv run python scripts/connect_debug.py /dev/ttyACM0

It tries to connect up to 8 times with full debug logging (bytes sent and
received, diagnostic text from the controller, discarded input, and the
library's own retries when the controller holds stray input). If every
attempt fails, it sends ALL_ON without waiting for an answer and then ALL_OFF,
so you can see whether the controller still obeys commands it does not answer.
"""

import logging
import sys
import time

from arena_interface import Device, DeviceError
from arena_interface import messages as m
from arena_interface.transport import Transport


def main() -> None:
    """Try to connect with full logging; if that fails, test ALL_ON/ALL_OFF."""
    port = sys.argv[1] if len(sys.argv) > 1 else None
    logging.basicConfig(
        level=logging.DEBUG, format="%(relativeCreated)7.0f ms %(name)s: %(message)s"
    )
    for attempt in range(1, 9):
        try:
            with Device.open(port) as dev:
                print(f"attempt {attempt}: connected: {dev.firmware_version}")
                return
        except DeviceError as exc:
            print(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            time.sleep(0.5)

    if port is None:
        from arena_interface import find_ports

        ports = find_ports()
        if not ports:
            print("no controller port found")
            return
        port = ports[0].device
    print(f"\nNo answer. Sending ALL_ON to {port}, then ALL_OFF 2 s later;")
    print("watch the arena: do the panels light up for about 2 seconds?")
    transport = Transport.open(port)
    try:
        for req, pause in ((m.all_on(), 2.0), (m.all_off(), 0.5)):
            try:
                transport.request(req.frame, expect=req.opcode, timeout=1.0)
                print(f"  answered 0x{req.opcode:02x}")
            except DeviceError as exc:
                print(f"  no answer to 0x{req.opcode:02x}: {exc}")
            time.sleep(pause)
        print("Sent ALL_OFF. Did the panels go dark again?")
    finally:
        transport.close()


if __name__ == "__main__":
    main()
