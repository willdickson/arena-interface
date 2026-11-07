"""Play a movie file (.mp4, .avi, .gif, ...) on the arena through a box you move.

Shows: reading frames with OpenCV, dev.pace() keeping the movie's own frame
rate (skipping frames if the computer falls behind), and the same
crop -> shrink -> dev.stream_image() path as webcam.py. Space pauses (the box
still moves); the movie loops unless --once. See roi_view.py for the mouse and
keyboard controls.
Requires: opencv-python (``uv sync --extra camera``) and a movie file; with
--simulate and no file, a moving test picture and the simulated controller.
"""

import argparse
from pathlib import Path

import cv2
from roi_view import RoiSelector, synthetic_frame

from arena_interface import Device


def next_frame(video: cv2.VideoCapture, skip: int, loop: bool):
    """The next frame after skipping `skip` (None at the end without --loop)."""
    for _ in range(skip):
        video.grab()
    ok, frame = video.read()
    if not ok and loop:
        video.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = video.read()
    return frame if ok else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("movie", nargs="?", type=Path, help="movie file")
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="use fake device")
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed")
    parser.add_argument("--once", action="store_true", help="do not loop")
    parser.add_argument("--seconds", type=float, help="stop after this long")
    parser.add_argument("--gs2", action="store_true", help="start in GS2 (dithered)")
    parser.add_argument(
        "--mirror",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="mirror left-right (default: off)",
    )
    parser.add_argument(
        "--rotate", type=float, default=0.0, help="shift around the arena, 0-360 deg"
    )
    parser.add_argument(
        "--brightness", type=int, default=128, help="panel duty cycle 1-255"
    )
    parser.add_argument("--no-gui", action="store_true", help="no windows")
    args = parser.parse_args()

    video, fps = None, 30.0
    if args.movie is not None:
        video = cv2.VideoCapture(str(args.movie))
        if not video.isOpened():
            parser.error(f"cannot open {args.movie}")
        fps = video.get(cv2.CAP_PROP_FPS) or fps
    elif not args.simulate:
        parser.error("give a movie file (or --simulate for a test picture)")

    with Device.open(args.port, simulate=args.simulate) as dev:
        roi = RoiSelector(
            dev.arena.pixel_shape,
            gui=not args.no_gui,
            gs2=args.gs2,
            mirror=args.mirror,
            rotate=args.rotate,
            brightness=args.brightness,
        )
        pacer = dev.pace(fps * args.speed, duration=args.seconds, late="skip")
        frame = None
        try:
            for tick in pacer:
                if not roi.poll():
                    break
                if frame is None or not roi.paused:
                    if video is None:
                        frame = synthetic_frame(tick.index / fps)
                    else:
                        frame = next_frame(video, tick.skipped, loop=not args.once)
                    if frame is None:
                        break
                dev.stream_image(
                    roi.update(frame),
                    gray_level=roi.gray_level,
                    duty_cycle=roi.brightness,
                )
        except KeyboardInterrupt:
            pass
        finally:
            roi.close()
            if video is not None:
                video.release()
        print(f"{fps * args.speed:g} frames/s: {pacer.report}")


if __name__ == "__main__":
    main()
