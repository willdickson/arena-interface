# Examples

The scripts below live in the `examples/` folder of the repository. Every one
accepts `--port` (default: auto-detect) and `--simulate`, which runs it
against the simulated controller with no hardware attached:

```bash
uv run python examples/06_trials.py --simulate
```

## Getting started

### `01_connect.py`

Connect to the device, print its firmware version, and disconnect.

```python
--8<-- "examples/01_connect.py"
```

### `02_ports.py`

List serial ports, then connect to one by name.

```python
--8<-- "examples/02_ports.py"
```

### `03_open_close.py`

Context manager versus manual open/close, and leaving the display on.

```python
--8<-- "examples/03_open_close.py"
```

## Core usage

### `04_device_info.py`

Read what the controller reports about itself.

```python
--8<-- "examples/04_device_info.py"
```

### `05_display_settings.py`

Read and change the display settings.

```python
--8<-- "examples/05_display_settings.py"
```

### `06_trials.py`

Run trials from a pattern stored on the SD card.

```python
--8<-- "examples/06_trials.py"
```

### `07_show_frames.py`

Choose each displayed frame from the host.

```python
--8<-- "examples/07_show_frames.py"
```

### `08_pattern_library.py`

Manage the patterns on the controller's SD card.

```python
--8<-- "examples/08_pattern_library.py"
```

### `09_make_pattern.py`

Build a pattern from pixel arrays and save it as a .pat file.

```python
--8<-- "examples/09_make_pattern.py"
```

### `10_stream_images.py`

Send images from the host straight to the arena.

```python
--8<-- "examples/10_stream_images.py"
```

### `11_analog_io.py`

Analog output and inputs.

```python
--8<-- "examples/11_analog_io.py"
```

### `12_digital_io.py`

Digital IO BNC ports: roles, outputs and levels.

```python
--8<-- "examples/12_digital_io.py"
```

### `13_sample_to_csv.py`

Record the analog inputs at a fixed rate into a CSV file.

```python
--8<-- "examples/13_sample_to_csv.py"
```

### `14_telemetry_events.py`

Log what the arena actually displayed, from the controller's own record.

```python
--8<-- "examples/14_telemetry_events.py"
```

### `15_partial_arena.py`

Arena presets, partial arenas, and rigs smaller than the firmware's grid.

```python
--8<-- "examples/15_partial_arena.py"
```

### `16_panel_firmware.py`

Update the panels' own firmware through the controller.

```python
--8<-- "examples/16_panel_firmware.py"
```

### `17_diagnostics.py`

Controller diagnostics for troubleshooting.

```python
--8<-- "examples/17_diagnostics.py"
```

## Robustness

### `18_errors.py`

Handling errors: what each exception means and how to recover.

```python
--8<-- "examples/18_errors.py"
```

### `19_reconnect.py`

Recover from a controller reset or an unplugged cable.

```python
--8<-- "examples/19_reconnect.py"
```

### `20_threads.py`

Share one Device between threads.

```python
--8<-- "examples/20_threads.py"
```

## Recipes

### `21_traffic_log.py`

Log every byte exchanged with the controller, for debugging.

```python
--8<-- "examples/21_traffic_log.py"
```

### `22_experiment_run.py`

A complete experiment run: upload a stimulus, run timed trials, save logs.

```python
--8<-- "examples/22_experiment_run.py"
```

### `23_ao_calibration.py`

Calibrate Analog In 1 against the analog output with a loopback cable.

```python
--8<-- "examples/23_ao_calibration.py"
```

### `24_host_playback.py`

Play frame sequences at a fixed rate, paced by the computer.

```python
--8<-- "examples/24_host_playback.py"
```

### `25_live_pacing.py`

Closed loop from the computer: choose each frame live at a fixed rate.

```python
--8<-- "examples/25_live_pacing.py"
```

### `plotting/live_plot.py`

Live plot of the analog inputs with matplotlib.

```python
--8<-- "examples/plotting/live_plot.py"
```

### `camera/webcam.py`

Stream a webcam to the arena in real time. A box with the arena's aspect
ratio, moved and resized with the mouse, picks the part of the picture that
is shrunk to the arena's pixels and streamed. GS16 with contrast stretching by
default; `--gs2` uses dithered on/off pixels. `--rotate DEG` (0-360) moves
the picture's centre around the arena. Needs OpenCV
(`uv sync --extra camera`); `--simulate` uses a moving test picture.

```python
--8<-- "examples/camera/webcam.py"
```

### `camera/movie.py`

The same for a movie file (`.mp4`, `.avi`, `.gif`, ...), played at its own frame rate
with `dev.pace()`.

```bash
uv run python examples/camera/movie.py clip.mp4
```

```python
--8<-- "examples/camera/movie.py"
```

### `camera/roi_view.py`

The box and the preview window shared by both camera examples, with the
mouse and keyboard controls in its docstring.

```python
--8<-- "examples/camera/roi_view.py"
```

## Notebook

`examples/notebooks/walkthrough.ipynb` is a Jupyter tour of the main features
against the simulated controller. Change `SIMULATE = True` to `False` to run it
on a real arena.
