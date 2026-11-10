"""Manage the patterns on the controller's SD card.

Shows: iterating device.patterns, info(), upload(), download(), rename(),
find() by name with the name cache on, delete() and archive(). The example
uploads its own pattern and removes it again, so the card ends as it started.
Requires: a controller with an SD card (or --simulate). The display is
stopped first, because transfers need it off.
"""

import argparse

from arena_interface import Device, Pattern


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    # cache_patterns: keep the names, so finding a pattern by name is fast.
    with Device.open(args.port, simulate=args.simulate, cache_patterns=True) as dev:
        dev.stop()
        for entry in dev.patterns:
            info = dev.patterns.info(entry.pattern_id)
            print(
                f"{entry.pattern_id:3} {entry.name:30} {info.frame_count:5} frames "
                f"{info.gray_level.name} {info.rows}x{info.cols}"
            )

        # A one-frame pattern sized for this controller.
        height, width = dev.arena.pixel_shape
        frame = [
            [15 if x < width // 2 else 0 for x in range(width)] for _ in range(height)
        ]
        pid = dev.patterns.upload("zz_example_08.pat", Pattern.from_frames([frame]))
        print(f"Uploaded as id {pid}")

        data = dev.patterns.download(pid)
        print(f"Downloaded {len(data)} bytes; frames: {len(Pattern.from_bytes(data))}")

        pid = dev.patterns.rename(pid, "zz_example_08_renamed.pat")
        print(f"Renamed; id is now {pid}: {dev.patterns.name(pid)}")
        found = dev.patterns.find("ZZ_example_08_renamed")  # case, .pat optional
        print(f"Found by name: id {found}")

        dev.patterns.delete(pid)
        print(f"Deleted; {len(dev.patterns)} patterns remain")

        if args.simulate:  # a full card can take a while to download
            print(f"SD archive: {len(dev.patterns.archive())} bytes (ZIP)")


if __name__ == "__main__":
    main()
