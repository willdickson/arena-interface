"""Controller diagnostics for troubleshooting.

Shows: diagnostics.health() (counters and previous-boot breadcrumbs),
crash_report(), sd_info(), spi_clock_mhz and debug_output.
Requires: a controller with the health capability (or --simulate).
"""

import argparse

from arena_interface import Capability, Device, FirmwareFlag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    args = parser.parse_args()

    with Device.open(args.port, simulate=args.simulate) as dev:
        diag = dev.diagnostics
        if dev.supports(Capability.HEALTH):
            h = diag.health()
            print(
                f"Uptime {h.uptime_ms / 1000:.1f} s, {h.loop_count} loops, "
                f"longest loop {h.loop_max_us} us"
            )
            print(f"SD reads {h.sd_reads}, slowest {h.sd_read_max_us} us")
            print(f"SD error code {h.sd_error}")
            print(
                f"Last reset cause 0x{h.reset_cause:08x}; previous boot ended in "
                f"{h.previous_op!r}"
                if h.breadcrumb_valid
                else "No previous-boot record"
            )

        if dev.supports(FirmwareFlag.CRASH_REPORT):
            report = diag.crash_report()
            print(f"Crash report: {report.fault_name or 'none recorded'}")

        if dev.supports(FirmwareFlag.SD_FAST_PATH):
            sd = diag.sd_info()
            print(
                f"SD card: {sd.product_name} {sd.capacity_bytes / 1e9:.1f} GB, "
                f"FAT type {sd.fat_type}"
                if sd.mounted
                else "SD card: none"
            )

        print(f"Panel SPI clock: {diag.spi_clock_mhz} MHz")
        print(
            f"Debug text output: {'on' if diag.debug_output else 'off'} "
            "(only debug firmware builds print anything)"
        )


if __name__ == "__main__":
    main()
