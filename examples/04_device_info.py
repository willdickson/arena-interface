"""Read what the controller reports about itself.

Shows: info (protocol version, capabilities, MAC), firmware (build identity),
supports(), arena (the panel grid), and a short health summary.
Requires: a connected controller (or use --simulate).
"""

import argparse

from arena_interface import Capability, Device, FirmwareFlag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        print(f"Port:          {dev.port_name}")
        print(f"MAC address:   {dev.info.mac}")
        print(f"Protocol:      v{dev.info.protocol_version}")
        print(f"Capabilities:  {dev.capabilities!r}")
        print(f"Firmware:      {dev.firmware_version}")
        print(f"Arena:         {dev.arena} {dev.arena.pixel_shape} pixels")

        for feature in (Capability.IO_EXT, Capability.HEALTH, FirmwareFlag.TELEMETRY):
            print(f"  supports {feature.name:10} {dev.supports(feature)}")

        if dev.supports(Capability.HEALTH):
            health = dev.diagnostics.health()
            print(f"Uptime:        {health.uptime_ms / 1000:.1f} s")
            print(f"State:         {health.state!r}")
            print(f"SD card:       {'mounted' if health.sd_mounted else 'none'}")
            print(f"Frames sent:   {health.frames_sent}")


if __name__ == "__main__":
    main()
