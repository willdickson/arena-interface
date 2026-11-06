"""Patterns from pixel arrays: build and read ``.pat`` files and stream frames.

A [`Pattern`][arena_interface.pattern.Pattern] holds one image per frame for a
whole arena: ``rows * 20`` pixels high and ``cols * 20`` wide, with values 0/1
(GS2) or 0-15 (GS16). Images may be nested lists or, if numpy is installed,
numpy arrays.

```python
from arena_interface import Arena, GrayLevel, Pattern

h, w = Arena.preset("G6_2x10").pixel_shape
stripes = [[[15 if (x + f) % 20 < 10 else 0 for x in range(w)] for _ in range(h)]
           for f in range(20)]
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

import importlib
import struct
from collections.abc import Sequence
from typing import Any, Literal

from ._crc import crc8_autosar, crc16_ccitt_false
from .arena import Arena
from .errors import ProtocolError
from .protocol import FRAME_PREFIX_SIZE, PANEL_SIZE, GrayLevel, panel_block_size

_np: Any
try:  # optional fast path
    _np = importlib.import_module("numpy")
except ImportError:  # pragma: no cover - exercised by monkeypatching in tests
    _np = None

__all__ = ["Origin", "Pattern"]

Origin = Literal["upper", "lower"]

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


# -- image <-> internal bytes (row-major, row 0 = top) -----------------------


def _image_bytes(
    image: Any, height: int, width: int, max_value: int, origin: str
) -> bytes:
    """Validate one image and return it as top-origin row-major bytes."""
    if _np is not None and isinstance(image, _np.ndarray):
        arr = image
        if arr.shape != (height, width):
            raise ValueError(f"image shape {arr.shape} != expected {(height, width)}")
        if arr.dtype.kind not in "biu":
            raise TypeError(f"image must hold integers or booleans, not {arr.dtype}")
        if arr.size and (arr.min() < 0 or arr.max() > max_value):
            raise ValueError(f"pixel values must be 0..{max_value}")
        if origin == "lower":
            arr = arr[::-1]
        return bytes(_np.ascontiguousarray(arr, dtype=_np.uint8).tobytes())
    rows = list(image)
    if len(rows) != height:
        raise ValueError(f"image has {len(rows)} rows, expected {height}")
    if origin == "lower":
        rows.reverse()
    out = bytearray()
    for y, row in enumerate(rows):
        values = list(row)
        if len(values) != width:
            raise ValueError(
                f"image row {y} has {len(values)} pixels, expected {width}"
            )
        for v in values:
            if not isinstance(v, int) or not 0 <= v <= max_value:
                raise ValueError(
                    f"pixel values must be integers 0..{max_value}, got {v!r}"
                )
        out += bytes(values)
    return bytes(out)


def _to_rows(data: bytes, width: int, origin: str) -> list[list[int]]:
    rows = [list(data[i : i + width]) for i in range(0, len(data), width)]
    return rows[::-1] if origin == "lower" else rows


# -- panel packing -----------------------------------------------------------


def _panel_pixels(image: bytes, rows: int, cols: int) -> list[bytes]:
    """Split a top-origin image into 400-pixel panels in wire order."""
    width = cols * PANEL_SIZE
    height = rows * PANEL_SIZE
    panels = []
    for pr in range(rows):  # 0 = bottom row of panels
        top = height - (pr + 1) * PANEL_SIZE
        for pc in range(cols):
            left = pc * PANEL_SIZE
            panels.append(
                b"".join(
                    image[
                        (top + y) * width + left : (top + y) * width + left + PANEL_SIZE
                    ]
                    for y in range(PANEL_SIZE)
                )
            )
    return panels


def _pack_pixels(pixels: bytes, gray: GrayLevel) -> bytes:
    if gray == GrayLevel.GS16:
        return bytes((pixels[i] << 4) | pixels[i + 1] for i in range(0, _PIXELS, 2))
    out = bytearray(50)
    for i, v in enumerate(pixels):
        if v:
            out[i >> 3] |= 0x80 >> (i & 7)
    return bytes(out)


def _unpack_pixels(data: bytes, gray: GrayLevel) -> bytes:
    if gray == GrayLevel.GS16:
        return bytes(v for b in data for v in (b >> 4, b & 0x0F))
    return bytes((data[i >> 3] >> (7 - (i & 7))) & 1 for i in range(_PIXELS))


def _block(data: bytes, gray: GrayLevel, duty: int) -> bytes:
    body = bytes([_OPCODE[gray]]) + data + bytes([duty])
    ones = 1 + sum(bin(b).count("1") for b in body)  # version bits = 0x01
    return bytes([0x01 | ((ones & 1) << 7)]) + body


def _encode_blocks(
    image: bytes, rows: int, cols: int, gray: GrayLevel, duty: int
) -> bytes:
    if _np is None:
        return b"".join(
            _block(_pack_pixels(p, gray), gray, duty)
            for p in _panel_pixels(image, rows, cols)
        )
    img = _np.frombuffer(image, dtype=_np.uint8).reshape(
        rows, PANEL_SIZE, cols, PANEL_SIZE
    )
    panels = img.transpose(0, 2, 1, 3)[::-1].reshape(rows * cols, _PIXELS)
    if gray == GrayLevel.GS16:
        data = (panels[:, 0::2] << 4) | panels[:, 1::2]
    else:
        data = _np.packbits(panels.astype(bool), axis=1)
    n = rows * cols
    body = _np.empty((n, data.shape[1] + 2), dtype=_np.uint8)
    body[:, 0] = _OPCODE[gray]
    body[:, 1:-1] = data
    body[:, -1] = duty
    ones = 1 + _np.unpackbits(body, axis=1).sum(axis=1)
    header = (0x01 | ((ones & 1) << 7)).astype(_np.uint8)
    return bytes(_np.concatenate([header[:, None], body], axis=1).tobytes())


def _decode_blocks(
    blocks: bytes, rows: int, cols: int, gray: GrayLevel
) -> tuple[bytes, int]:
    """Return the top-origin image and the duty byte of the first block."""
    size = panel_block_size(gray)
    width, height = cols * PANEL_SIZE, rows * PANEL_SIZE
    image = bytearray(width * height)
    for n in range(rows * cols):
        block = blocks[n * size : (n + 1) * size]
        pixels = _unpack_pixels(block[2:-1], gray)
        pr, pc = divmod(n, cols)
        top = height - (pr + 1) * PANEL_SIZE
        left = pc * PANEL_SIZE
        for y in range(PANEL_SIZE):
            start = (top + y) * width + left
            image[start : start + PANEL_SIZE] = pixels[
                y * PANEL_SIZE : (y + 1) * PANEL_SIZE
            ]
    return bytes(image), blocks[size - 1]


# -- Pattern -----------------------------------------------------------------


class Pattern:
    """A sequence of arena images, convertible to a ``.pat`` file.

    Build one with [`from_frames`][arena_interface.pattern.Pattern.from_frames]
    or read a file with [`from_bytes`][arena_interface.pattern.Pattern.from_bytes].
    Patterns are immutable.
    """

    def __init__(
        self,
        frames: Sequence[bytes],
        *,
        arena: Arena,
        gray_level: GrayLevel,
        duty_cycles: Sequence[int],
        observer_id: int = 0,
    ) -> None:
        """Create from validated internal frames; use the class methods instead."""
        self._frames = tuple(frames)
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
                nested lists or 2-D numpy arrays; or one 3-D numpy array
                ``(frames, height, width)``.
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

        Example:
            >>> blank = [[0] * 200 for _ in range(40)]
            >>> Pattern.from_frames([blank], gray_level=GrayLevel.GS2).arena
            Arena(rows=2, cols=10, installed_columns=None, name='', arena_id=0)
        """
        _check_origin(origin)
        gray = GrayLevel(gray_level)
        frame_list = list(frames)
        if not frame_list:
            raise ValueError("a pattern needs at least one frame")
        if not 0 <= observer_id <= 63:
            raise ValueError(f"observer_id must be 0..63, got {observer_id}")
        resolved = _resolve_arena(arena, frame_list[0])
        height, width = resolved.pixel_shape
        images = [
            _image_bytes(f, height, width, _MAX_VALUE[gray], origin) for f in frame_list
        ]
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
        images, duties = [], []
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
            images,
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
            self._frames == other._frames
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

    def frame(self, index: int, *, origin: Origin = "upper") -> list[list[int]]:
        """One frame as a list of pixel rows."""
        _check_origin(origin)
        return _to_rows(self._frames[index], self.pixel_shape[1], origin)

    def to_numpy(self, *, origin: Origin = "upper") -> Any:
        """All frames as a ``(frames, height, width)`` uint8 numpy array.

        Raises:
            ImportError: If numpy is not installed.
        """
        _check_origin(origin)
        if _np is None:
            raise ImportError(
                "to_numpy() needs numpy: pip install arena-interface[numpy]"
            )
        h, w = self.pixel_shape
        arr = _np.frombuffer(b"".join(self._frames), dtype=_np.uint8).reshape(-1, h, w)
        return arr[:, ::-1].copy() if origin == "lower" else arr.copy()

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
        fitted = []
        for image in self._frames:
            out = bytearray(dst_h * dst_w)
            for yb in range(dst_h):  # bottom-origin destination row
                src_top = src_h - 1 - (yb % src_h)
                dst_top = dst_h - 1 - yb
                out[dst_top * dst_w : dst_top * dst_w + copy_w] = image[
                    src_top * src_w : src_top * src_w + copy_w
                ]
            fitted.append(bytes(out))
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


def _resolve_arena(arena: Arena | tuple[int, int] | None, first: Any) -> Arena:
    if isinstance(arena, Arena):
        return arena
    if arena is not None:
        rows, cols = arena
        return Arena(rows, cols)
    if _np is not None and isinstance(first, _np.ndarray):
        height, width = first.shape[:2] if first.ndim >= 2 else (0, 0)
    else:
        rows_list = list(first)
        height = len(rows_list)
        width = len(list(rows_list[0])) if rows_list else 0
    if height == 0 or width == 0 or height % PANEL_SIZE or width % PANEL_SIZE:
        raise ValueError(
            f"image size {height}x{width} is not a whole number of 20x20 panels; "
            "pass arena= to state the geometry"
        )
    return Arena(height // PANEL_SIZE, width // PANEL_SIZE)
