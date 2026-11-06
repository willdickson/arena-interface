# Getting started

## Install

The library is installed from a copy of this repository.

=== "uv"

    ```bash
    uv sync                          # creates .venv with the library and dev tools
    ```

    To use it from another uv project:

    ```bash
    uv add /path/to/arena-interface
    ```

=== "pip"

    ```bash
    pip install /path/to/arena-interface
    ```

The dependencies, pyserial and numpy, are installed automatically.

## Allow access to the serial port

=== "Linux"

    The controller appears as `/dev/ttyACM0` (or `ttyACM1`, ...). Your user
    needs permission to open it:

    1. Install the Teensy udev rules from PJRC, which also stop ModemManager
       from grabbing the port:

        ```bash
        curl -fsSL https://www.pjrc.com/teensy/00-teensy.rules -o /tmp/00-teensy.rules
        sudo cp /tmp/00-teensy.rules /etc/udev/rules.d/
        sudo udevadm control --reload-rules && sudo udevadm trigger
        ```

    2. Unplug and replug the controller.

    If you still get "permission denied", add yourself to the `dialout` group
    (`sudo usermod -aG dialout $USER`) and log out and back in.

=== "macOS"

    No setup is needed. The controller appears as `/dev/cu.usbmodem...`.

=== "Windows"

    No setup is needed on Windows 10 and later. The controller appears as a
    `COM` port.

Only one program can use the port at a time: close serial monitors, the web
console or other scripts first.

## Connect

```python
from arena_interface import Device

with Device.open() as dev:
    print(dev.firmware_version)
    print(dev.arena)              # the panel grid, e.g. G6_2x10
    print(dev.arena.pixel_shape)  # image size for patterns, e.g. (40, 200)
```

`Device.open()` without arguments finds the controller by its USB ids. If
several Teensy boards are connected, it asks each one whether it is a
controller. To skip detection, pass the port name: `Device.open("/dev/ttyACM0")`
or `Device.open("COM5")`. To see what is connected:

```python
from arena_interface import find_ports

for port in find_ports():
    print(port.device, port.description)
```

Leaving the `with` block blanks the arena and releases the port. Use
`Device.open(..., all_off_on_close=False)` to leave the display running.

## Try it without hardware

Every part of the library works against a simulated controller:

```python
with Device.open(simulate=True) as dev:
    dev.start_trial(1, frame_rate=30)
    print(dev.frame_position)
```

The simulator starts with two demo patterns on its SD card. All examples
accept `--simulate`:

```bash
uv run python examples/01_connect.py --simulate
uv run python examples/06_trials.py               # the real controller
```

## First things to try

```python
from arena_interface import Device, TrialMode

with Device.open() as dev:
    for entry in dev.patterns:                      # what is on the SD card
        print(entry.pattern_id, entry.name)

    dev.start_trial(1, TrialMode.OPEN_LOOP, frame_rate=30, duration=3)

    dev.show_pattern(1)                             # host-chosen frames
    for i in range(dev.frame_position.frame_count):
        dev.show_frame(i)
```

Then read the [user guide](user-guide.md), or browse the [examples](examples.md).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `DeviceNotFound: no G6 arena controller found` | Cable unplugged, or the port is not visible to the OS. Run `find_ports(all_ports=True)` to see every port. |
| `DeviceNotFound: cannot open ...: Permission denied` | Port permissions (see above). |
| `DeviceNotFound: cannot open ...: ... busy` | Another program has the port open. |
| `DeviceTimeout` on every command | The port is not a controller, or the controller is wedged; reset it or replug the cable. |
| A brief "CE NN" glyph on the arena | The controller rejected a command. The library prevents most cases; see [Errors](user-guide.md#errors). |
