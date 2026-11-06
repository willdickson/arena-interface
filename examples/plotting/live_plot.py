"""Live plot of the analog inputs with matplotlib.

Shows: device.sample() feeding a matplotlib line plot that updates as data
arrives. With a non-interactive backend (e.g. MPLBACKEND=Agg) the final plot
is only saved to analog_plot.png.
The live window uses matplotlib's default Tk backend (TkAgg), which needs
Python's tkinter; on Linux install it with your distribution's package if
missing (e.g. ``sudo apt install python3-tk``).
Requires: matplotlib (``pip install matplotlib``) and a controller with the
extended I/O capability, or --simulate, which drives a sine wave through a
simulated cable from the analog output to Analog In 1.
"""

import argparse
import math
from collections import deque

import matplotlib.pyplot as plt

from arena_interface import Device

NON_INTERACTIVE = {"agg", "pdf", "ps", "svg", "cairo", "template"}


def redraw(ax, line1, line2, t, a1, a2) -> None:
    """Show the latest data and scroll the time axis."""
    line1.set_data(t, a1)
    line2.set_data(t, a2)
    ax.set_xlim(t[0], max(t[-1], t[0] + 1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--seconds", type=float, default=10.0, help="how long to plot")
    parser.add_argument("--rate", type=float, default=50.0, help="samples per second")
    args = parser.parse_args()

    live = plt.get_backend().lower() not in NON_INTERACTIVE
    fig, ax = plt.subplots()
    (line1,) = ax.plot([], [], label="Analog In 1")
    (line2,) = ax.plot([], [], label="Analog In 2")
    ax.set(xlabel="time (s)", ylabel="mV", ylim=(-10500, 10500))
    ax.legend(loc="upper right")
    t, a1, a2 = (deque(maxlen=int(args.rate * 10)) for _ in range(3))

    with Device.open(args.port, simulate=args.simulate) as dev:
        if dev.simulator is not None:
            dev.simulator.analog_loopback = (1.0, 0.0)
        start = None
        for s in dev.sample(dev.io.read_analog_in, args.rate, duration=args.seconds):
            start = start or s.time
            t.append(s.time - start)
            a1.append(s.value.ain1_mv)
            a2.append(s.value.ain2_mv)
            if dev.simulator is not None:
                dev.io.analog_out = round(2500 + 2400 * math.sin(2 * math.pi * t[-1]))
            if live and len(t) % 5 == 0:
                redraw(ax, line1, line2, t, a1, a2)
                plt.pause(0.001)
    redraw(ax, line1, line2, t, a1, a2)
    fig.savefig("analog_plot.png")
    print("Saved analog_plot.png")
    if live:
        plt.show()


if __name__ == "__main__":
    main()
