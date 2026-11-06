# User guide

This guide explains how the controller works and how each part of the library
maps onto it. Every name used here is importable from `arena_interface`.

## How the controller works

The controller drives a grid of 20 × 20-pixel LED panels from a Teensy 4.1.
Each firmware build is compiled for one grid (2 × 10, 4 × 10 or 4 × 12 panels);
the library reads it when connecting: `dev.arena` is that grid as an
[`Arena`](api.md#arena_interface.arena.Arena) (e.g. the `G6_2x10` preset), and
`dev.arena.pixel_shape` is the image size for patterns.

The controller is always in one state (`ArenaState`):

| State | Started by | What the arena shows |
|---|---|---|
| `ALL_OFF` | `all_off()`, `stop()`, end of a timed trial, after an error | Dark |
| `ALL_ON` | `all_on()` | Every pixel at full brightness |
| `OPEN_LOOP` | `start_trial(..., TrialMode.OPEN_LOOP)` | A pattern advancing at a frame rate |
| `SHOW_FRAME` | `show_pattern()`, `show_frame()`, `start_trial(..., TrialMode.SHOW_FRAME)` | The frame you choose |
| `CLOSED_LOOP` | `start_trial(..., TrialMode.CLOSED_LOOP)` | A pattern whose speed follows Analog In 1 |
| `STREAMING_FRAME` | `stream_frame()`, `stream_image()` | A frame sent from the computer |
| `PSRAM_PLAY` | `show_panel_frame()`, `play_panel_frames()` | Frames stored in the panels themselves |
| `ERROR_DISPLAY` | A rejected command | A "CE NN" error code for 0.75 s, then dark |

The controller only ever answers commands; it never sends anything on its
own. Everything "live" in this library (event streams, sampling) is the
computer asking repeatedly.

## Connecting

```python
from arena_interface import Device

dev = Device.open()                  # auto-detect
dev = Device.open("/dev/ttyACM0")    # a specific port; no detection
dev = Device.open(simulate=True)     # the simulated controller
```

Auto-detection looks for USB serial ports with the Teensy's ids (16C0:0483).
Any Teensy has these ids, so when several match, the library asks each one
for its controller information and keeps the one that answers as a G6
controller (`probe=False` turns that off and makes several matches an error).
`find_ports()` lists candidates without sending anything.

When connecting, the library reads the controller's identity:

```python
dev.info.mac                # unique per controller
dev.firmware                # FirmwareVersion: git SHA, build date, grid, flags
dev.capabilities            # Capability flags
dev.supports(Capability.IO_EXT)
```

### Closing

Close with a `with` block or `dev.close()`. Closing sends ALL_OFF first so
the arena goes dark: the controller otherwise keeps displaying after the
computer disconnects. Pass `all_off_on_close=False` (or set the attribute) to
leave a stimulus running. Closing never raises, even if the device has
already gone.

### Reset and reconnect

`dev.reset()` reboots the controller; the USB connection drops and the
device object is closed. `dev.reconnect()` waits (10 s by default) for the
same controller to come back, checked by its MAC address, so a different
controller on the same port is not picked up by mistake. `reconnect()` also
recovers after a `DeviceDisconnected` error (cable unplugged).

## Displaying patterns from the SD card

Patterns on the SD card are addressed by a **1-based id** in alphabetical
order of file name.

```python
dev.start_trial(3, TrialMode.OPEN_LOOP, frame_rate=60)      # forwards
dev.start_trial(3, TrialMode.OPEN_LOOP, frame_rate=-60)     # backwards
dev.start_trial(3, TrialMode.CLOSED_LOOP, gain=20)          # 2 fps per volt
dev.start_trial(3, frame_rate=60, duration=5.0)             # stops by itself
dev.start_trial(3, frame_rate=60, duty_cycle=64)            # dimmer, this trial only
```

`duration` is timed by the controller (10 ms resolution, up to 655 s), so the
trial ends on time even if the computer is busy. `duty_cycle` overrides the
brightness stored in the pattern for this trial only.

To choose each frame yourself:

```python
dev.show_pattern(3)                 # load, show frame 0, don't advance
for i in range(dev.frame_position.frame_count):
    dev.show_frame(i)
```

`show_frame` checks the index locally, because an out-of-range index would
make the controller show an error code and stop the display.

## Patterns from pixel arrays

A [`Pattern`](api.md#arena_interface.pattern.Pattern) is one image per frame
covering the whole arena: `rows * 20` pixels high, `cols * 20` wide, which
is `dev.arena.pixel_shape` for the connected controller.

```python
from arena_interface import Pattern, GrayLevel

h, w = dev.arena.pixel_shape                      # e.g. (40, 200) for 2x10
frames = [[[15 if (x + f) % 40 < 20 else 0 for x in range(w)] for _ in range(h)]
          for f in range(40)]
pattern = Pattern.from_frames(frames, gray_level=GrayLevel.GS16, arena=dev.arena)

pattern_id = dev.patterns.upload("stripes.pat", pattern)
dev.start_trial(pattern_id, frame_rate=40)
```

- **Gray levels**: `GrayLevel.GS2` takes 0/1 (or booleans), `GrayLevel.GS16`
  takes 0-15.
- **Orientation**: row 0 is the **top** of the arena (`origin="upper"`, as in
  numpy and matplotlib). Data whose row 0 is the bottom, as in the MATLAB and
  JavaScript tools, needs `origin="lower"`. Columns run left to right.
- **Brightness**: `duty_cycle` (1-255, default 128) for all frames or one per
  frame.
- **numpy**: frames can be 2-D arrays or one 3-D array; `pattern.to_numpy()`
  returns a `(frames, height, width)` array. Without numpy, nested lists work.
- **Files**: `pattern.to_bytes()` gives the `.pat` file; `Pattern.from_bytes()`
  reads one (checking every checksum).

### Arenas, partial arenas and fitting

[`Arena`](api.md#arena_interface.arena.Arena) describes a rig. The arenas in the
maDisplayTools registry are built in:

```python
from arena_interface import Arena, PRESETS

Arena.preset("G6_2x8of10")    # 2x10 grid, columns 1-8 installed
list(PRESETS)                 # all preset names
```

Partial-arena presets such as `G6_3x12of18` describe arenas **built** with a
permanent cut-out, for example for access when used with a 2-photon
microscope. Draw a partial arena's images on its full grid; columns without
panels are ignored.

!!! tip "Temporarily removed panels"
    Panels or whole columns taken out for a while, to make room for other
    instruments, do not make an arena partial. Keep using the full arena (or
    no `arena=` at all): the library always sends data for every panel
    position, so the remaining panels display correctly and a replaced panel
    works again with no change on the computer.

A rig can also be smaller than the grid its controller firmware was
built for (a 3 × 10 rig on 4 × 10 firmware, or `G6_3x12of18` on 4 × 12). The
library then fits the image to the firmware's grid: grid column *c* goes to
firmware column *c*, and the rig's rows are repeated up the frame so the
panels light whichever row connector they are on.

```python
rig = Arena.preset("G6_3x10")
pattern = Pattern.from_frames(frames, arena=rig)
dev.patterns.upload("rig.pat", pattern)          # fitted automatically
fitted = pattern.fit_to(4, 10)                     # or explicitly
```

The controller refuses any `.pat` file whose grid differs from its own; the
library checks this before uploading.

## The SD-card pattern library

```python
len(dev.patterns)                          # how many
for entry in dev.patterns:                 # PatternEntry(pattern_id, name)
    print(entry.pattern_id, entry.name)
dev.patterns.info(3)                       # frames, gray level, grid, size
data = dev.patterns.download(3)            # the .pat file
new_id = dev.patterns.upload("x.pat", data_or_pattern, overwrite=True)
new_id = dev.patterns.rename(new_id, "y.pat")
dev.patterns.delete(new_id)
zip_bytes = dev.patterns.archive()         # the whole card as a ZIP
dev.patterns.format_card()                 # erases everything
```

!!! note
    Ids are positions in alphabetical order, so uploading, renaming and
    deleting can change the ids of other patterns. The methods that change
    the library return the new id; look ids up with `patterns.find(name)`
    rather than storing them for long.

Downloading, uploading, archiving and formatting need the display to be off:
call `dev.stop()` first, otherwise the controller refuses with a
`CommandError`.

## Streaming frames from the computer

`stream_image()` displays one image immediately, without the SD card:

```python
dev.stream_image(image, gray_level=GrayLevel.GS2)
dev.stream_image(rig_image, arena=Arena.preset("G6_3x10"))   # fitted
```

`stream_frame(payload)` sends a frame payload you encoded yourself
(`pattern.stream_payload(i)`). Each frame is one USB transfer of 1-10 KB,
depending on the arena size and gray level. To send frames at a fixed rate,
use host-paced playback (next section).

Frames can also live in the panels' own memory: `show_panel_frame(i)` and
`play_panel_frames(start, count, fps)`.

## Host-paced playback

Here the computer decides when each frame is shown, either from a sequence
prepared in advance or chosen live, for example from a tracking system.

```python
dev.show_pattern(4)
report = dev.play_frames(range(200), fps=200)              # frames of an SD pattern
report = dev.play_images(images, fps=120)                  # streamed images
print(report)    # e.g. "200/200 frames at 200/s in 1.00 s; skipped 0, late 0; ..."
```

**In the background.** `start_frames()` and `start_images()` take the same
arguments and return a [`Playback`](api.md#arena_interface.playback.Playback)
while the frames play in a separate thread:

```python
with dev.start_frames(range(200), fps=200) as player:
    while player.running:
        print(player.progress, "of", player.total)   # your script keeps working
        time.sleep(0.1)
report = player.report       # or player.wait() / player.stop()
```

Your script may keep using the device meanwhile; each of its commands delays
the next frame slightly, and the report shows the effect. `wait()` re-raises
any error the playback hit (for example `DeviceDisconnected`).

**Live (closed loop).** `pace()` is a fixed-rate clock; send whatever frame
you choose on each tick:

```python
pacer = dev.pace(200, duration=10)
for tick in pacer:
    dev.show_frame(frame_from_tracker())     # or dev.stream_image(...)
print(pacer.report)
```

Each [`Tick`](api.md#arena_interface.playback.Tick) has its slot number, due
time and lateness; `tick.skipped` says how many slots were skipped just
before it, so you can catch up.

### Timing and late frames

Frames are scheduled against a fixed clock, so errors do not accumulate. The
scheduler sleeps until just before each slot and then waits the last fraction
of a millisecond actively, which gives sub-millisecond timing on Linux, macOS
and Windows (Python 3.11+).

When a frame cannot be sent on time (the computer or the controller was
busy), `late="skip"` (the default) drops frames whose slot has passed, so the
sequence keeps its schedule and length; `late="send"` sends every frame,
possibly late, so the sequence runs long. Either way:

- the [`PlaybackReport`](api.md#arena_interface.playback.PlaybackReport) gives
  frames sent, skipped and late (more than half a frame period after their
  slot), and median, 99th-percentile and worst lateness;
- falling behind is logged on `arena_interface.playback` (at most one line
  per second);
- `play_frames(..., verify=True)` reads the controller's log afterwards and
  reports how many frame changes actually reached the panels (`displayed`,
  to compare with `changes`) and how many were replaced unseen.

### How fast?

A frame is displayed only if it stays in place for at least one refresh of
the controller (`dev.refresh_rate`), so keep `fps` below the refresh rate; a
`PerformanceWarning` is issued if it is too close. On a 2x10 arena the
refresh rate defaults to 300 Hz for GS16 content and 1000 Hz for GS2. Pass
`refresh_rate=` to the playback functions to raise it first, but note that a
refresh rate set from the computer stays in force (for all content) until the
controller restarts, and that the panels themselves redraw at 1 kHz.

Frames of an SD pattern also wait for the controller to read them from the
card. That usually takes under 2 ms but occasionally stalls, so close to the
refresh rate a frame can now and then be replaced unseen; `verify=True` shows
it. Measured limits are in the
[protocol reference](PROTOCOL.md#refresh-rate-and-measured-performance).

## Display settings

```python
dev.refresh_rate                 # Hz at which the frame is re-sent to the panels
dev.refresh_rate = 500           # stays in force until the controller restarts
dev.panel_display_mode           # PanelDisplayMode: ONESHOT, PERSIST, TRIGGERED, GATED
dev.frames_sent                  # frames pushed to the panels; reset_frames_sent()
```

## Analog and digital I/O

```python
io = dev.io
io.analog_out = 2500                         # mV, 0-5000; reads back from the DAC
io.load_analog_table([0, 2500, 5000])        # follows the frame index (1-21 levels)
io.set_analog_out_mode(AoMode.FRAME_NUMBER)  # 0 V first frame .. 5 V last frame
io.read_analog_in()                          # AnalogInputs(ain1_mv, ain2_mv), uncalibrated

io.dio_roles()                               # roles and levels of ports 1 and 2
io.set_dio_role(1, DioRole.OUT_PROGRAMMABLE)
io.set_digital_out(1, True)
io.read_digital()
```

Port 2 starts as the external-trigger input for the triggered and gated
panel modes; the controller refuses to drive it until its role is changed.
The analog inputs are not calibrated in firmware; example 23 shows a loopback
calibration.

## Telemetry: what the arena actually displayed

The controller logs every command it receives, every change of the displayed
frame and every state change, with microsecond timestamps, in a ring buffer
that survives software resets.

```python
from arena_interface import FrameRecord

dev.telemetry.drain()                              # discard older events
dev.start_trial(1, frame_rate=60, duration=2)
for event in dev.telemetry.events(duration=2.2):
    if isinstance(event.record, FrameRecord):
        print(event.time, event.record.index)
```

- `event.time` is wall-clock time (`time.time()`) of the controller's own
  timestamp, so it is more accurate than timing on the computer.
  `event.record.t_us` is the raw controller time.
- Records are only removed from the controller after they have been
  returned to you. Breaking out of a loop and starting another neither loses
  nor repeats events.
- Records written before the controller's last reset have `time=None`.
- Events lost because the ring overflowed are counted (`stream.lost`,
  `dev.telemetry.lost`) and logged.
- Times stay correct for records hours old: each record is dated from the
  next newer one. The controller's microsecond clock wraps every 71.6 minutes,
  so if it logged nothing for longer than that, the gap is ambiguous. The
  library resolves it from the controller's uptime when the boot record is
  among the records read; otherwise records before such a gap can be dated
  71.6 minutes (or a multiple) too late.

## Sampling at a fixed rate

`dev.sample()` calls any reading at a fixed rate:

```python
for s in dev.sample(dev.io.read_analog_in, rate_hz=100, duration=10):
    print(s.time, s.value.ain1_mv)
```

The schedule does not drift; readings that cannot keep up are skipped and
counted (`sampler.skipped`). Each reading is one USB round trip of about
1 ms, so a few hundred readings per second is the practical limit.

## Diagnostics and panel firmware

`dev.diagnostics` has the controller's health counters (uptime, loop and SD
timing, and what the previous boot was doing when it last reset), the last
CPU fault, SD card identity, debug-text output and the panel SPI clock.

`dev.panel_firmware` stores a panel firmware image on the SD card, then
programs and verifies panels one at a time (1-based panel numbers, as on the
panel map). The display must be off.

## Errors

All exceptions derive from `DeviceError`.

| Exception | Meaning | What to do |
|---|---|---|
| `CommandError` | The controller refused a command. `.status` and `.message` say why. | Check the message; `ErrorCode.DISPLAY_ACTIVE` means stop the display first. |
| `UnsupportedCommand` | The firmware does not advertise this feature. Nothing was sent. | Check `dev.supports(...)` first, or update the firmware. |
| `DeviceTimeout` | No answer in time. Also a `TimeoutError`. | Retry; if it persists, reset or replug. |
| `DeviceDisconnected` | The connection was lost; the device is now closed. | `dev.reconnect()`. |
| `DeviceNotFound` | No controller found, or the port could not be opened. | See [troubleshooting](getting-started.md#troubleshooting). |
| `ProtocolError` | The device sent malformed data. | Report it; usually a firmware mismatch. |

Invalid arguments (a frame index out of range, a pattern with the wrong
size) raise `ValueError` or `TypeError` before anything is sent. The library
checks the cases that would otherwise make the controller show an error code
and stop the display.

## Timeouts

Ordinary commands wait 1 s for an answer. Slower commands get longer limits:
SD-card operations 30 s, programming or verifying a panel 60 s, and
formatting the card 180 s. Bulk transfers (pattern and archive downloads)
fail if no data arrives for 5 s.

## Threads

A `Device` may be shared between threads. Each method is atomic: the wire
protocol allows one request at a time, and the device serialises them, so a
thread just waits while another's request is in progress. Multi-step methods
(such as `patterns.upload`) hold the lock for their whole sequence. A sequence
of calls in your own code is not atomic; use your own lock if another thread
must not interleave. Event streams and samplers release the lock between
polls, so other threads can send commands while they run.

## Logging

The library logs through the standard `logging` module:

| Logger | Level | Content |
|---|---|---|
| `arena_interface.transport` | DEBUG | Every byte sent and received |
| `arena_interface.transport` | WARNING | Late replies to earlier requests, skipped |
| `arena_interface.framing` | WARNING | Bytes from the device that were not a valid frame |
| `arena_interface.firmware` | INFO | Debug text printed by debug firmware builds |
| `arena_interface.streaming` | WARNING | Lost telemetry records |

```python
import logging
logging.basicConfig(level=logging.WARNING)
logging.getLogger("arena_interface.transport").setLevel(logging.DEBUG)
```

## Testing your own code

`Device.open(simulate=True)` connects to a
[`FakeController`](api.md#arena_interface.testing.FakeController), which
emulates the firmware: the same replies, error codes and side effects, an
in-memory SD card, the telemetry log and the I/O state. Pass your own
controller to configure it or to inject faults:

```python
from arena_interface.testing import FakeController, make_test_pattern

ctrl = FakeController(rows=4, cols=10, patterns={"a.pat": make_test_pattern(4, 10)})
with Device.open(simulate=ctrl) as dev:
    ctrl.analog_in = (1500, -200)          # what the inputs read
    ctrl.unplug()                          # the next command raises DeviceDisconnected
```

`dev.simulator` gives access to the controller of a simulated device. The
simulator does not render pixels.
