# Examples

Each script is runnable on its own and accepts `--port` (default: auto-detect)
and `--simulate` (run against the simulated controller, no hardware needed):

```bash
uv run python examples/01_connect.py                 # auto-detect the controller
uv run python examples/01_connect.py --port COM5     # explicit port
uv run python examples/01_connect.py --simulate      # no hardware needed
```

Examples that write files take `--output`; the default names are listed in
the repository's `.gitignore`. `tests/test_examples.py` runs every example
with `--simulate`.

## Getting started

| File | Shows |
|------|-------|
| `01_connect.py` | Connect to the device, print its firmware version, and disconnect |
| `02_ports.py` | List serial ports, then connect to one by name |
| `03_open_close.py` | Context manager versus manual open/close, and leaving the display on |

## Core usage

| File | Shows |
|------|-------|
| `04_device_info.py` | Read what the controller reports about itself |
| `05_display_settings.py` | Read and change the display settings |
| `06_trials.py` | Run trials from a pattern stored on the SD card |
| `07_show_frames.py` | Choose each displayed frame from the host |
| `08_pattern_library.py` | Manage the patterns on the controller's SD card |
| `09_make_pattern.py` | Build a pattern from pixel arrays and save it as a .pat file |
| `10_stream_images.py` | Send images from the host straight to the arena |
| `11_analog_io.py` | Analog output and inputs |
| `12_digital_io.py` | Digital IO BNC ports: roles, outputs and levels |
| `13_sample_to_csv.py` | Record the analog inputs at a fixed rate into a CSV file |
| `14_telemetry_events.py` | Log what the arena actually displayed, from the controller's own record |
| `15_partial_arena.py` | Arena presets, partial arenas, and rigs smaller than the firmware's grid |
| `16_panel_firmware.py` | Update the panels' own firmware through the controller |
| `17_diagnostics.py` | Controller diagnostics for troubleshooting |

## Robustness

| File | Shows |
|------|-------|
| `18_errors.py` | Handling errors: what each exception means and how to recover |
| `19_reconnect.py` | Recover from a controller reset or an unplugged cable |
| `20_threads.py` | Share one Device between threads |

## Recipes

| File | Shows |
|------|-------|
| `21_traffic_log.py` | Log every byte exchanged with the controller, for debugging |
| `22_experiment_run.py` | A complete experiment run: upload a stimulus, run timed trials, save logs |
| `23_ao_calibration.py` | Calibrate Analog In 1 against the analog output with a loopback cable |
| `24_host_playback.py` | Play frame sequences at a fixed rate, paced by the computer |
| `25_live_pacing.py` | Closed loop from the computer: choose each frame live at a fixed rate |
| `plotting/live_plot.py` | Live plot of the analog inputs with matplotlib |

## Notebook

| File | Shows |
|------|-------|
| `notebooks/walkthrough.ipynb` | A tour of the main features against the simulator |

`plotting/live_plot.py` needs matplotlib; the notebook needs Jupyter.
