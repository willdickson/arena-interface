"""Patterns from pixel arrays: build and read ``.pat`` files and stream frames.

A [`Pattern`][arena_interface.pattern.Pattern] holds one image per frame for a
whole arena: ``rows * 20`` pixels high and ``cols * 20`` wide, with values 0/1
(GS2) or 0-15 (GS16). Images may be numpy arrays or nested lists; frames come back as
numpy arrays.

```python
import numpy as np
from arena_interface import Arena, GrayLevel, Pattern

h, w = Arena.preset("G6_2x10").pixel_shape
x = np.arange(w)
stripes = [np.tile(np.where((x + f) % 20 < 10, 15, 0), (h, 1)) for f in range(20)]
pattern = Pattern.from_frames(stripes, gray_level=GrayLevel.GS16)
data = pattern.to_bytes()               # a .pat file
```

Orientation: by default row 0 of an image is the **top** of the arena
(``origin="upper"``, as in numpy and matplotlib's ``imshow``). Pass
``origin="lower"`` for row 0 at the bottom, the convention of the MATLAB and
JavaScript tools. Columns run left to right either way.

The library converts to the on-wire layout: panels in row-major order from the
bottom row of panels, and within each panel block the first 20 pixels are the
panel's top row (as the panel firmware reads them; see ``docs/PROTOCOL.md``).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from ._crc import crc8_autosar, crc16_ccitt_false
from .arena import Arena
from .errors import ProtocolError
from .protocol import FRAME_PREFIX_SIZE, PANEL_SIZE, GrayLevel, panel_block_size

__all__ = ["Origin", "Pattern"]

Origin = Literal["upper", "lower"]
#: A frame or stack of frames: uint8 pixel values, row 0 at the top.
Frames = npt.NDArray[np.uint8]

#: Panels a pattern file can describe (48-bit panel mask).
MAX_PATTERN_PANELS = 48
#: Default brightness byte of generated panel blocks (128 = 50 %).
DEFAULT_DUTY_CYCLE = 0x80

_HEADER_SIZE = 18
_PIXELS = PANEL_SIZE * PANEL_SIZE
_OPCODE = {GrayLevel.GS2: 0x10, GrayLevel.GS16: 0x30}
_MAX_VALUE = {GrayLevel.GS2: 1, GrayLevel.GS16: 15}


def _check_origin(origin: str) -> None:
    if origin not in ("upper", "lower"):
        raise ValueError(f"origin must be 'upper' or 'lower', got {origin!r}")


def _as_frames(frames: Any) -> npt.NDArray[Any]:
    """Turn a sequence of images (or a 3-D array) into one 3-D array."""
    if isinstance(frames, np.ndarray):
        arr = frames
    else:
        frame_list = list(frames)
        if not frame_list:
            raise ValueError("a pattern needs at least one frame")
        try:
            arr = np.asarray(frame_list)
        except ValueError:  # ragged nested lists
            raise ValueError(
                "all images must be the same size, with the same number of "
                "pixels in every row"
            ) from None
    if arr.ndim != 3:
        raise ValueError(
            "frames must be a sequence of 2-D images or a 3-D array, got shape "
            f"{arr.shape}"
        )
    if arr.shape[0] == 0:
        raise ValueError("a pattern needs at least one frame")
    return arr


def _validate(arr: npt.NDArray[Any], height: int, width: int, max_value: int) -> Frames:
    """Check shape, type and range; return a uint8 copy."""
    h, w = arr.shape[1:]
    if (h, w) != (height, width):
        raise ValueError(
            f"images are {h}x{w} pixels (shape {arr.shape}), expected {height}x{width}"
        )
    if arr.dtype.kind not in "biu":
        raise TypeError(f"pixel values must be integers or booleans, not {arr.dtype}")
    lo, hi = int(arr.min()), int(arr.max())
    if lo < 0 or hi > max_value:
        bad = lo if lo < 0 else hi
        raise ValueError(f"pixel values must be 0..{max_value}, got {bad}")
    return arr.astype(np.uint8)


# -- panel packing (on-wire layout) ------------------------------------------


def _encode_blocks(
    image: Frames, rows: int, cols: int, gray: GrayLevel, duty: int
) -> bytes:
    """Pack one top-origin image into panel blocks in wire order."""
    panels = (
        image.reshape(rows, PANEL_SIZE, cols, PANEL_SIZE)
        .transpose(0, 2, 1, 3)[::-1]  # panel row 0 = bottom row of panels
        .reshape(rows * cols, _PIXELS)
    )
    data: npt.NDArray[Any]
    if gray == GrayLevel.GS16:
        data = (panels[:, 0::2] << 4) | panels[:, 1::2]
    else:
        data = np.packbits(panels.astype(bool), axis=1)
    body = np.empty((rows * cols, data.shape[1] + 2), dtype=np.uint8)
    body[:, 0] = _OPCODE[gray]
    body[:, 1:-1] = data
    body[:, -1] = duty
    ones = 1 + np.unpackbits(body, axis=1).sum(axis=1)  # version bits = 0x01
    header = (0x01 | ((ones & 1) << 7)).astype(np.uint8)
    return np.concatenate([header[:, None], body], axis=1).tobytes()


def _decode_blocks(
    blocks: bytes, rows: int, cols: int, gray: GrayLevel
) -> tuple[Frames, int]:
    """Return the top-origin image and the duty byte of the first block."""
    size = panel_block_size(gray)
    raw = np.frombuffer(blocks, dtype=np.uint8, count=rows * cols * size)
    data = raw.reshape(rows * cols, size)[:, 2:-1]
    if gray == GrayLevel.GS16:
        pixels = np.empty((rows * cols, _PIXELS), dtype=np.uint8)
        pixels[:, 0::2] = data >> 4
        pixels[:, 1::2] = data & 0x0F
    else:
        pixels = np.unpackbits(data, axis=1)[:, :_PIXELS]
    image = (
        pixels.reshape(rows, cols, PANEL_SIZE, PANEL_SIZE)[::-1]
        .transpose(0, 2, 1, 3)
        .reshape(rows * PANEL_SIZE, cols * PANEL_SIZE)
    )
    return image, int(raw[size - 1])


# -- Pattern -----------------------------------------------------------------


class Pattern:
    """A sequence of arena images, convertible to a ``.pat`` file.

    Build one with [`from_frames`][arena_interface.pattern.Pattern.from_frames]
    or read a file with [`from_bytes`][arena_interface.pattern.Pattern.from_bytes].
    Patterns are immutable.
    """

    def __init__(
        self,
        frames: Frames,
        *,
        arena: Arena,
        gray_level: GrayLevel,
        duty_cycles: Sequence[int],
        observer_id: int = 0,
    ) -> None:
        """Create from validated internal frames; use the class methods instead."""
        self._frames = frames
        self._frames.flags.writeable = False
        self.arena = arena
        """Arena the images are drawn for."""
        self.gray_level = GrayLevel(gray_level)
        """GS2 (on/off) or GS16 (16 levels)."""
        self.duty_cycles = tuple(duty_cycles)
        """Brightness byte (1-255) of each frame."""
        self.observer_id = observer_id
        """Observer id stored in the file header (0-63, host metadata)."""

    # -- construction -----------------------------------------------------

    @classmethod
    def from_frames(
        cls,
        frames: Sequence[Any] | Any,
        *,
        gray_level: GrayLevel = GrayLevel.GS16,
        arena: Arena | tuple[int, int] | None = None,
        origin: Origin = "upper",
        duty_cycle: int | Sequence[int] = DEFAULT_DUTY_CYCLE,
        observer_id: int = 0,
    ) -> Pattern:
        """Build a pattern from one image per frame.

        Args:
            frames: Images, each ``rows * 20`` by ``cols * 20`` pixels, as
                2-D numpy arrays or nested lists; or one 3-D array
                ``(frames, height, width)``. Values may be any integer or
                boolean type.
            gray_level: GS2 (values 0/1) or GS16 (values 0-15).
            arena: The arena the images are drawn for: an
                [`Arena`][arena_interface.arena.Arena], ``(rows, cols)``, or
                None to infer a full arena from the image size.
            origin: ``"upper"`` if row 0 is the top of the arena,
                ``"lower"`` if it is the bottom.
            duty_cycle: Brightness 1-255, for all frames or one per frame.
            observer_id: Observer id for the file header (0-63).

        Returns:
            The pattern.

        Raises:
            ValueError: If the images have the wrong shape or values.
            TypeError: If the pixel values are not integers or booleans.

        Example:
            >>> blank = np.zeros((1, 40, 200), dtype=np.uint8)
            >>> Pattern.from_frames(blank, gray_level=GrayLevel.GS2).arena
            Arena(rows=2, cols=10, installed_columns=None, name='', arena_id=0)
        """
        _check_origin(origin)
        gray = GrayLevel(gray_level)
        if not 0 <= observer_id <= 63:
            raise ValueError(f"observer_id must be 0..63, got {observer_id}")
        arr = _as_frames(frames)
        resolved = _resolve_arena(arena, arr.shape[1:])
        height, width = resolved.pixel_shape
        images = _validate(arr, height, width, _MAX_VALUE[gray])
        if origin == "lower":
            images = np.ascontiguousarray(images[:, ::-1])
        duties = (
            [duty_cycle] * len(images)
            if isinstance(duty_cycle, int)
            else list(duty_cycle)
        )
        if len(duties) != len(images):
            raise ValueError(f"{len(duties)} duty cycles for {len(images)} frames")
        for d in duties:
            if not isinstance(d, int) or not 1 <= d <= 255:
                raise ValueError(f"duty cycles must be 1..255, got {d!r}")
        return cls(
            images,
            arena=resolved,
            gray_level=gray,
            duty_cycles=duties,
            observer_id=observer_id,
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> Pattern:
        """Read a ``.pat`` file (G6, header version 2).

        Raises:
            ProtocolError: If the file is malformed or a checksum is wrong.
        """
        data = bytes(data)
        if len(data) < _HEADER_SIZE or data[:4] != b"G6PT":
            raise ProtocolError("not a G6 pattern file (missing G6PT header)")
        if data[4] >> 4 != 2:
            raise ProtocolError(f"unsupported pattern format version {data[4] >> 4}")
        if crc8_autosar(data[:17]) != data[17]:
            raise ProtocolError("pattern header checksum mismatch")
        frame_count = struct.unpack_from("<H", data, 6)[0]
        rows, cols, gs = data[8], data[9], data[10]
        if gs not in (1, 2) or frame_count == 0 or rows * cols == 0:
            raise ProtocolError("invalid pattern header")
        gray = GrayLevel.GS16 if gs == 2 else GrayLevel.GS2
        arena_id = ((data[4] & 0x0F) << 2) | (data[5] >> 6)
        preset = Arena.from_id(arena_id)
        arena = (
            preset
            if preset is not None and (preset.rows, preset.cols) == (rows, cols)
            else Arena(rows, cols, arena_id=arena_id)
        )
        blocks_size = rows * cols * panel_block_size(gray)
        frame_size = FRAME_PREFIX_SIZE + blocks_size + 2
        if len(data) < _HEADER_SIZE + frame_count * frame_size:
            raise ProtocolError("pattern file is truncated")
        images: list[Frames] = []
        duties: list[int] = []
        for f in range(frame_count):
            start = _HEADER_SIZE + f * frame_size
            frame = data[start : start + frame_size]
            if frame[:2] != b"FR":
                raise ProtocolError(f"frame {f}: missing FR marker")
            if crc16_ccitt_false(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
                raise ProtocolError(f"frame {f}: checksum mismatch")
            image, duty = _decode_blocks(frame[FRAME_PREFIX_SIZE:-2], rows, cols, gray)
            images.append(image)
            duties.append(duty)
        return cls(
            np.stack(images),
            arena=arena,
            gray_level=gray,
            duty_cycles=duties,
            observer_id=data[5] & 0x3F,
        )

    # -- inspection -------------------------------------------------------

    def __len__(self) -> int:
        """Number of frames."""
        return len(self._frames)

    def __eq__(self, other: object) -> bool:
        """Patterns are equal if they would encode to the same file."""
        if not isinstance(other, Pattern):
            return NotImplemented
        return (
            np.array_equal(self._frames, other._frames)
            and (self.arena.rows, self.arena.cols)
            == (other.arena.rows, other.arena.cols)
            and self.gray_level == other.gray_level
            and self.duty_cycles == other.duty_cycles
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        """Short description."""
        return (
            f"<Pattern {len(self)} frames, {self.gray_level.name}, "
            f"{self.arena.rows}x{self.arena.cols} panels>"
        )

    @property
    def frame_count(self) -> int:
        """Number of frames."""
        return len(self._frames)

    @property
    def pixel_shape(self) -> tuple[int, int]:
        """``(height, width)`` of each image."""
        return self.arena.pixel_shape

    def frame(self, index: int, *, origin: Origin = "upper") -> Frames:
        """One frame as a ``(height, width)`` uint8 array (a copy).

        ``frame(i)[0, 0]`` is the top-left pixel (``origin="upper"``).
        """
        _check_origin(origin)
        image: Frames = self._frames[index]
        return (image[::-1] if origin == "lower" else image).copy()

    def to_numpy(self, *, origin: Origin = "upper") -> Frames:
        """All frames as a ``(frames, height, width)`` uint8 array (a copy)."""
        _check_origin(origin)
        frames = self._frames
        return (frames[:, ::-1] if origin == "lower" else frames).copy()

    # -- conversion -------------------------------------------------------

    def fit_to(self, rows: int, cols: int) -> Pattern:
        """Place the pattern on a controller's panel grid.

        Used when the rig is smaller than the grid the firmware is built for
        (e.g. a 3x10 rig on 4x10 firmware, or ``G6_3x12of18`` on 4x12). Grid
        column *c* goes to firmware column *c*; columns beyond the firmware's
        are dropped, so every installed column must fit. Rows are repeated
        up the frame from the bottom, so the panels light whichever row
        connector they are on (the JS tools' rule).

        Raises:
            ValueError: If the rig does not fit the grid.
        """
        arena = self.arena
        if (arena.rows, arena.cols) == (rows, cols):
            return self
        if arena.rows > rows or arena.columns[-1] >= cols:
            raise ValueError(
                f"a {arena} arena ({len(arena.columns)} columns installed) does not "
                f"fit a {rows}x{cols} panel grid"
            )
        src_h, src_w = arena.pixel_shape
        dst_h, dst_w = rows * PANEL_SIZE, cols * PANEL_SIZE
        copy_w = min(src_w, dst_w)
        # Destination row y (from the top) is bottom-origin row dst_h-1-y,
        # which repeats source rows counted up from the bottom.
        bottom_up = (dst_h - 1 - np.arange(dst_h)) % src_h
        fitted = np.zeros((self.frame_count, dst_h, dst_w), dtype=np.uint8)
        fitted[:, :, :copy_w] = self._frames[:, src_h - 1 - bottom_up, :copy_w]
        target = Arena(rows, cols, arena_id=arena.arena_id)
        return Pattern(
            fitted,
            arena=target,
            gray_level=self.gray_level,
            duty_cycles=self.duty_cycles,
            observer_id=self.observer_id,
        )

    def stream_payload(self, index: int) -> bytes:
        """One frame as a STREAM_FRAME payload (``"FR"``, index, panel blocks)."""
        arena = self.arena
        blocks = _encode_blocks(
            self._frames[index],
            arena.rows,
            arena.cols,
            self.gray_level,
            self.duty_cycles[index],
        )
        return b"FR" + struct.pack("<H", index & 0xFFFF) + blocks

    def to_bytes(self) -> bytes:
        """Encode as a ``.pat`` file (G6, header version 2, full panel mask).

        Raises:
            ValueError: If the grid has more than 48 panels (fit it to the
                controller's grid first with
                [`fit_to`][arena_interface.pattern.Pattern.fit_to]).
        """
        arena = self.arena
        panels = arena.panel_count
        if panels > MAX_PATTERN_PANELS:
            raise ValueError(
                f"{arena} has {panels} panel positions; pattern files hold at most "
                f"{MAX_PATTERN_PANELS} (use fit_to() with the controller's grid)"
            )
        if self.frame_count > 0xFFFF:
            raise ValueError("pattern files hold at most 65535 frames")
        header = bytearray(_HEADER_SIZE)
        header[0:4] = b"G6PT"
        header[4] = 0x20 | ((arena.arena_id >> 2) & 0x0F)
        header[5] = ((arena.arena_id & 0x03) << 6) | (self.observer_id & 0x3F)
        struct.pack_into("<H", header, 6, self.frame_count)
        header[8], header[9] = arena.rows, arena.cols
        header[10] = 2 if self.gray_level == GrayLevel.GS16 else 1
        header[11:17] = ((1 << panels) - 1).to_bytes(6, "little")
        header[17] = crc8_autosar(bytes(header[:17]))
        out = bytearray(header)
        for i in range(self.frame_count):
            frame = self.stream_payload(i)
            out += frame + struct.pack("<H", crc16_ccitt_false(frame))
        return bytes(out)


def _resolve_arena(
    arena: Arena | tuple[int, int] | None, shape: tuple[int, ...]
) -> Arena:
    if isinstance(arena, Arena):
        return arena
    if arena is not None:
        rows, cols = arena
        return Arena(rows, cols)
    height, width = shape
    if height == 0 or width == 0 or height % PANEL_SIZE or width % PANEL_SIZE:
        raise ValueError(
            f"image size {height}x{width} is not a whole number of 20x20 panels; "
            "pass arena= to state the geometry"
        )
    return Arena(height // PANEL_SIZE, width // PANEL_SIZE)
