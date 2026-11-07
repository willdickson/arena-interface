"""Stream a webcam to the arena in real time, through a box you move.

Shows: capturing with OpenCV, cropping a region with the arena's aspect
ratio (chosen live with the mouse), shrinking it to dev.arena.pixel_shape and
sending it with dev.stream_image() as fast as the camera delivers frames.
GS16 with contrast stretching by default; --gs2 sends on/off pixels with
ordered dithering. The image is mirrored, like a mirror you face; --no-mirror
turns that off. See roi_view.py for the mouse and keyboard controls.
The camera is asked for compressed MJPG frames at 30 fps: raw (YUYV) frames
need several times the USB bandwidth and can arrive with bands out of order
(seen with a webcam passed through to a virtual machine).
Requires: opencv-python (``uv sync --extra camera``) and a webcam, or
--simulate, which uses a moving test picture and the simulated controller.
"""

import argparse
import os
import sys
import time

import cv2
from roi_view import RoiSelector, synthetic_frame

from arena_interface import Device


def open_camera(index: int, width: int, height: int) -> cv2.VideoCapture:
    """Open a webcam asking for MJPG frames of the given size at 30 fps."""
    camera = cv2.VideoCapture(index)
    camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # before size
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    camera.set(cv2.CAP_PROP_FPS, 30)
    return camera


def hide_native_warnings() -> None:
    """Drop C-library messages (e.g. libjpeg's harmless "Corrupt JPEG data").

    Many Logitech webcams trigger one per frame. Python's own errors still
    show: sys.stderr is moved to a copy of the original stream first.
    """
    sys.stderr = os.fdopen(os.dup(2), "w", buffering=1)
    os.dup2(os.open(os.devnull, os.O_WRONLY), 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument(
        "--simulate", action="store_true", help="fake device and test picture"
    )
    parser.add_argument("--camera", type=int, default=0, help="camera number")
    parser.add_argument("--width", type=int, default=640, help="camera image width")
    parser.add_argument("--height", type=int, default=480, help="camera image height")
    parser.add_argument("--seconds", type=float, help="stop after this long")
    parser.add_argument("--gs2", action="store_true", help="start in GS2 (dithered)")
    parser.add_argument(
        "--mirror",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="mirror left-right (default: on)",
    )
    parser.add_argument(
        "--rotate", type=float, default=0.0, help="shift around the arena, 0-360 deg"
    )
    parser.add_argument(
        "--brightness", type=int, default=128, help="panel duty cycle 1-255"
    )
    parser.add_argument("--no-gui", action="store_true", help="no windows")
    args = parser.parse_args()

    camera = None
    if not args.simulate:
        camera = open_camera(args.camera, args.width, args.height)
        if not camera.isOpened():
            parser.error(f"cannot open camera {args.camera}")
        hide_native_warnings()

    with Device.open(args.port, simulate=args.simulate) as dev:
        roi = RoiSelector(
            dev.arena.pixel_shape,
            gui=not args.no_gui,
            gs2=args.gs2,
            mirror=args.mirror,
            rotate=args.rotate,
            brightness=args.brightness,
        )
        start, sent = time.monotonic(), 0
        try:
            while roi.poll():
                elapsed = time.monotonic() - start
                if args.seconds is not None and elapsed >= args.seconds:
                    break
                if camera is None:  # a 30 fps "camera"
                    time.sleep(max(0.0, start + sent / 30 - time.monotonic()))
                    frame = synthetic_frame(elapsed)
                else:
                    ok, frame = camera.read()  # waits for the next camera frame
                    if not ok:
                        raise SystemExit("the camera stopped delivering frames")
                dev.stream_image(
                    roi.update(frame),
                    gray_level=roi.gray_level,
                    duty_cycle=roi.brightness,
                )
                sent += 1
        except KeyboardInterrupt:
            pass
        finally:
            roi.close()
            if camera is not None:
                camera.release()
        elapsed = time.monotonic() - start
        print(f"sent {sent} frames in {elapsed:.1f} s ({sent / elapsed:.1f} fps)")


if __name__ == "__main__":
    main()
