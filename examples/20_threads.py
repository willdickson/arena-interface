"""Share one Device between threads.

Shows: a background thread sampling the analog inputs while the main thread
runs trials. Every Device method is atomic, so the two never corrupt each
other's exchanges; a thread just waits briefly while the other's request is
on the wire.
Requires: a controller with a pattern on its SD card and the extended I/O
capability (or --simulate).
"""

import argparse
import threading
import time

from arena_interface import Device, TrialMode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        readings = []
        stop = threading.Event()

        def sampler() -> None:
            for s in dev.sample(dev.io.read_analog_in, rate_hz=50):
                readings.append(s)
                if stop.is_set():
                    return

        thread = threading.Thread(target=sampler)
        thread.start()
        for rate in (10, 20, 40):
            dev.start_trial(1, TrialMode.OPEN_LOOP, frame_rate=rate)
            time.sleep(0.3)
            print(f"rate {rate:2} fps: frame {dev.frame_position.index}")
        dev.stop()
        stop.set()
        thread.join()
        print(f"Background thread took {len(readings)} samples meanwhile")


if __name__ == "__main__":
    main()
