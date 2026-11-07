"""Shared helper for the camera examples: a box you move over the video.

Not an example on its own; ``webcam.py`` and ``movie.py`` import it. The box
keeps the arena's aspect ratio (5:1 on a 2x10 arena). What is inside it is
shrunk to the arena's pixel size, optionally rotated around the arena, and
shown, as the panels will show it, in a second window.

Controls (click the "source" window first so it has the keyboard):

| Input | Action |
|---|---|
| drag inside the box | move it |
| click outside the box | centre it there (keep holding to drag) |
| drag a corner | resize it (the opposite corner stays put) |
| mouse wheel, ``+`` / ``-`` | resize about the centre |
| arrow keys, ``w`` ``a`` ``s`` ``d`` | move the box (2% of the picture height) |
| ``r`` | reset to the largest centred box |
| ``m`` | mirror left-right |
| ``g`` | switch GS16 / GS2 (dithered) |
| ``c`` | contrast stretch on/off |
| ``[`` / ``]`` | rotate around the arena (5 columns, 9 degrees on a 2x10 arena) |
| ``,`` / ``.`` | dim / brighten the panels (duty cycle 1-255, x1.25 per press) |
| space | pause (movie.py) |
| ``q`` or Esc | quit |

Requires: opencv-python (``uv sync --extra camera``).
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from arena_interface import GrayLevel

SOURCE_WINDOW = "source"
ARENA_WINDOW = "arena"
MAX_DISPLAY_WIDTH = 960  # larger videos are shown scaled down
CORNER_GRAB = 12  # display pixels around a corner that start a resize
MIN_BOX_WIDTH = 20  # source pixels
ROTATE_STEP_COLUMNS = 5  # arena columns per [ or ] key press
BOX_COLOR = (0, 255, 0)  # BGR
CONTROLS = """\
In the source window: drag the box (or use the arrow keys or w a s d) to move
it; drag a corner (or use the mouse wheel or + / -) to resize it.
Keys: r reset, m mirror, g GS16/GS2, c contrast, [ ] rotate,
, . dim/brighten, space pause, q quit."""
MOVE_STEP = 0.02  # fraction of the picture height per key press
# Arrow-key codes from cv2.waitKeyEx(): Linux, Windows, macOS.
_ARROWS = {
    **dict.fromkeys((65361, 2424832, 63234), (-1, 0)),  # left
    **dict.fromkeys((65362, 2490368, 63232), (0, -1)),  # up
    **dict.fromkeys((65363, 2555904, 63235), (1, 0)),  # right
    **dict.fromkeys((65364, 2621440, 63233), (0, 1)),  # down
}
# 4x4 Bayer matrix: thresholds for ordered dithering in GS2.
_BAYER4 = (
    np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) + 0.5
) / 16


def to_arena_image(
    crop: np.ndarray, shape: tuple[int, int], *, gs2: bool = False, stretch: bool = True
) -> np.ndarray:
    """Turn a BGR crop into an arena image: 0-15 (GS16) or dithered 0/1 (GS2)."""
    h, w = shape
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    small = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32)
    if stretch:  # use the full brightness range: 16 levels go a long way
        lo, hi = np.percentile(small, (2, 98))
        level = (small - lo) / max(hi - lo, 1.0)
    else:
        level = small / 255
    level = np.clip(level, 0.0, 1.0)
    if gs2:
        threshold = np.tile(_BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w]
        return (level > threshold).astype(np.uint8)
    return np.round(level * 15).astype(np.uint8)


def synthetic_frame(t: float, size: tuple[int, int] = (640, 480)) -> np.ndarray:
    """A moving test picture (BGR) standing in for a camera or a movie."""
    w, h = size
    bars = (127 + 120 * np.sin(2 * np.pi * (np.arange(w) / 80 - t / 2))).astype(
        np.uint8
    )
    frame = np.repeat(np.tile(bars, (h, 1))[:, :, None], 3, axis=2)
    centre = (int(w / 2 + w / 3 * np.cos(t)), int(h / 2 + h / 4 * np.sin(2 * t)))
    cv2.circle(frame, centre, 40, (0, 0, 255), -1)
    return frame


class RoiSelector:
    """A box of fixed aspect ratio over the video, moved with the mouse.

    Args:
        arena_shape: ``(height, width)`` of the arena image, e.g.
            ``dev.arena.pixel_shape``.
        gui: Show the windows and take mouse/keyboard input.
        gs2: Start in GS2 (dithered on/off) instead of GS16.
        mirror: Flip the video left-right (natural for a webcam facing you).
        rotate: Move the image's centre around the arena by this many degrees
            (0-360; 360 is the full image width, back to the start).
        brightness: Panel brightness (duty cycle) 1-255; 128 is 50 %.
    """

    def __init__(
        self,
        arena_shape: tuple[int, int],
        *,
        gui: bool = True,
        gs2: bool = False,
        mirror: bool = False,
        rotate: float = 0.0,
        brightness: int = 128,
    ) -> None:
        """Set up the box (sized on the first frame) and the windows."""
        self.arena_shape = arena_shape
        self.aspect = arena_shape[1] / arena_shape[0]  # width / height
        self.gui, self.gs2, self.mirror = gui, gs2, mirror
        self.rotate = rotate
        if not 1 <= brightness <= 255:
            raise ValueError(f"brightness must be 1-255, got {brightness}")
        self.brightness = brightness
        """Duty cycle to send the images with (``dev.stream_image(duty_cycle=)``)."""
        self.stretch = True
        self.paused = False
        self.box = (0.0, 0.0, 0.0)  # x, y, width in source pixels
        self._frame_size = (0, 0)
        self._scale = 1.0  # display pixels per source pixel
        self._drag: tuple[str, float, float] | None = None
        self._fps = 0.0
        self._last = time.monotonic()
        if gui:
            cv2.namedWindow(SOURCE_WINDOW, cv2.WINDOW_AUTOSIZE)
            cv2.namedWindow(ARENA_WINDOW, cv2.WINDOW_AUTOSIZE)
            cv2.setMouseCallback(SOURCE_WINDOW, self._on_mouse)
            print(CONTROLS)

    @property
    def gray_level(self) -> GrayLevel:
        """Gray level to send the images with."""
        return GrayLevel.GS2 if self.gs2 else GrayLevel.GS16

    def update(self, frame: np.ndarray) -> np.ndarray:
        """Crop the box from a BGR frame and return the arena image (and show it)."""
        if self.mirror:
            frame = np.ascontiguousarray(frame[:, ::-1])
        fh, fw = frame.shape[:2]
        if (fw, fh) != self._frame_size:
            self._frame_size = (fw, fh)
            self._scale = min(1.0, MAX_DISPLAY_WIDTH / fw)
            self.reset()
        x, y, w = (round(v) for v in self.box)
        crop = frame[y : y + round(w / self.aspect), x : x + w]
        image = to_arena_image(
            crop, self.arena_shape, gs2=self.gs2, stretch=self.stretch
        )
        shift = round(self.rotate % 360 / 360 * image.shape[1])
        image = np.roll(image, shift, axis=1)  # columns wrap around the arena
        now = time.monotonic()
        self._fps = 0.9 * self._fps + 0.1 / max(now - self._last, 1e-6)
        self._last = now
        if self.gui:
            self._show(frame, image)
        return image

    def poll(self) -> bool:
        """Handle key presses; False once the user quits (always True with no GUI)."""
        if not self.gui:
            return True
        code = cv2.waitKeyEx(1)
        if code in _ARROWS:
            self._move(*_ARROWS[code])
            return True
        key = code & 0xFF
        if key in (ord("q"), 27):
            return False
        actions = {
            ord("r"): self.reset,
            ord("m"): lambda: setattr(self, "mirror", not self.mirror),
            ord("g"): lambda: setattr(self, "gs2", not self.gs2),
            ord("c"): lambda: setattr(self, "stretch", not self.stretch),
            ord(","): lambda: self._dim(1 / 1.25),
            ord("."): lambda: self._dim(1.25),
            ord("["): lambda: self._turn(-1),
            ord("]"): lambda: self._turn(+1),
            ord(" "): lambda: setattr(self, "paused", not self.paused),
            ord("+"): lambda: self._zoom(1.1),
            ord("="): lambda: self._zoom(1.1),
            ord("-"): lambda: self._zoom(1 / 1.1),
            ord("w"): lambda: self._move(0, -1),
            ord("a"): lambda: self._move(-1, 0),
            ord("s"): lambda: self._move(0, 1),
            ord("d"): lambda: self._move(1, 0),
        }
        if key in actions:
            actions[key]()
        # Closing the window with its title-bar button also quits.
        return cv2.getWindowProperty(SOURCE_WINDOW, cv2.WND_PROP_VISIBLE) >= 1

    def close(self) -> None:
        """Close the windows."""
        if self.gui:
            cv2.destroyAllWindows()

    # -- the box ------------------------------------------------------------

    def reset(self) -> None:
        """The largest box that fits, centred."""
        fw, fh = self._frame_size
        w = min(fw, fh * self.aspect)
        self.box = ((fw - w) / 2, (fh - w / self.aspect) / 2, w)

    def _place(self, x: float, y: float, w: float) -> None:
        """Set the box, limiting its size and keeping it inside the frame."""
        fw, fh = self._frame_size
        w = min(max(w, MIN_BOX_WIDTH), fw, fh * self.aspect)
        h = w / self.aspect
        self.box = (min(max(x, 0.0), fw - w), min(max(y, 0.0), fh - h), w)

    def _move(self, dx: int, dy: int) -> None:
        step = MOVE_STEP * self._frame_size[1]
        x, y, w = self.box
        self._place(x + dx * step, y + dy * step, w)

    def _dim(self, factor: float) -> None:
        b = self.brightness
        step = round(b * factor) - b
        if step == 0:  # always move by at least one at the low end
            step = 1 if factor > 1 else -1
        self.brightness = min(max(b + step, 1), 255)

    def _turn(self, direction: int) -> None:
        step = 360 * ROTATE_STEP_COLUMNS / self.arena_shape[1]
        self.rotate = (self.rotate + direction * step) % 360

    def _zoom(self, factor: float) -> None:
        x, y, w = self.box
        cx, cy = x + w / 2, y + w / self.aspect / 2
        fw, fh = self._frame_size
        w = min(max(w * factor, MIN_BOX_WIDTH), fw, fh * self.aspect)
        self._place(cx - w / 2, cy - w / self.aspect / 2, w)

    def _on_mouse(self, event: int, x: int, y: int, flags: int, _param: object) -> None:
        sx, sy = x / self._scale, y / self._scale  # source pixels
        bx, by, bw = self.box
        bh = bw / self.aspect
        if event == cv2.EVENT_LBUTTONDOWN:
            corners = [(bx, by), (bx + bw, by), (bx, by + bh), (bx + bw, by + bh)]
            grab = CORNER_GRAB / self._scale
            for i, (cx, cy) in enumerate(corners):
                if abs(sx - cx) <= grab and abs(sy - cy) <= grab:
                    self._drag = ("resize", *corners[3 - i])  # anchor: opposite
                    return
            if not (bx <= sx <= bx + bw and by <= sy <= by + bh):
                self._place(sx - bw / 2, sy - bh / 2, bw)
                bx, by, _ = self.box
            self._drag = ("move", sx - bx, sy - by)
        elif event == cv2.EVENT_MOUSEMOVE and self._drag is not None:
            kind, a, b = self._drag
            if kind == "move":
                self._place(sx - a, sy - b, bw)
            else:  # resize from anchor (a, b) towards the pointer
                # Size from the pointer's offset projected on the box's
                # diagonal, so dragging mostly sideways resizes a wide box.
                dx, dy = abs(sx - a), abs(sy - b)
                w = (dx * self.aspect + dy) / (self.aspect**2 + 1) * self.aspect
                w = max(w, MIN_BOX_WIDTH)
                h = w / self.aspect
                self._place(a if sx >= a else a - w, b if sy >= b else b - h, w)
        elif event == cv2.EVENT_LBUTTONUP:
            self._drag = None
        elif event == cv2.EVENT_MOUSEWHEEL:
            # The wheel step is the signed top 16 bits of flags (OpenCV 5 has
            # no getMouseWheelDelta); positive means away from the user.
            delta = (flags >> 16) & 0xFFFF
            self._zoom(1 / 1.1 if delta >= 0x8000 else 1.1)

    # -- display ------------------------------------------------------------

    def _show(self, frame: np.ndarray, image: np.ndarray) -> None:
        s = self._scale
        view = (
            cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            if s < 1
            else frame.copy()
        )
        x, y, w = self.box
        h = w / self.aspect
        p1 = (round(x * s), round(y * s))
        p2 = (round((x + w) * s) - 1, round((y + h) * s) - 1)
        cv2.rectangle(view, p1, p2, BOX_COLOR, 2)
        status = "  ".join(
            [
                "GS2" if self.gs2 else "GS16",
                f"rotate {self.rotate % 360:.0f} deg",
                f"brightness {self.brightness}",
                f"stretch {'on' if self.stretch else 'off'}",
                *(["PAUSED"] if self.paused else []),
                f"{self._fps:.0f} fps",
            ]
        )
        for color, thickness in (((0, 0, 0), 3), ((255, 255, 255), 1)):
            cv2.putText(
                view, status, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, thickness
            )
        cv2.imshow(SOURCE_WINDOW, view)
        # The arena as green LEDs, enlarged with square pixels.
        zoom = max(1, 800 // image.shape[1])
        levels = 1 if self.gs2 else 15
        green = (image.astype(np.uint32) * self.brightness // levels).astype(np.uint8)
        green = cv2.resize(
            green, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_NEAREST
        )
        dark = np.zeros_like(green)
        cv2.imshow(ARENA_WINDOW, cv2.merge([dark, green, dark]))
