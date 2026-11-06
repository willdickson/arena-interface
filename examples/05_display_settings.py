"""Read and change the display settings.

Shows: all_on(), all_off(), stop(), refresh_rate, panel_display_mode,
frames_sent and reset_frames_sent().
Requires: a connected controller (or use --simulate).
Note: a refresh rate set here stays in force until the controller restarts.
"""

import argparse
import time

from arena_interface import Device, PanelDisplayMode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        print(f"Refresh rate:       {dev.refresh_rate} Hz")
        print(f"Panel display mode: {dev.panel_display_mode.name}")

        dev.panel_display_mode = PanelDisplayMode.PERSIST  # the default
        dev.reset_frames_sent()

        dev.all_on()
        time.sleep(0.5)
        print(f"Frames sent in 0.5 s of ALL_ON: {dev.frames_sent}")

        dev.stop()  # same effect as all_off(): blank and stop
        print("Arena blanked")


if __name__ == "__main__":
    main()
