"""Arena geometry: how many panels, and which of them are installed.

An [`Arena`][arena_interface.arena.Arena] is a grid of ``rows x cols`` G6
panels (20 x 20 pixels each). Partial arenas leave some columns empty; their
patterns are still drawn on the full grid, with the missing columns blank.

The rig's arena can differ from the grid the controller firmware is built
for (e.g. a ``G6_3x12of18`` rig on 4 x 12 firmware). Images are drawn for the
rig and fitted to the firmware's grid when sent; see
[`Pattern.fit_to`][arena_interface.pattern.Pattern.fit_to].

The G6 arenas registered in maDisplayTools are built in as presets:

```python
from arena_interface import Arena

walking = Arena.preset("G6_2x8of10")
print(walking.pixel_shape)      # (40, 200)
```
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import PANEL_SIZE

__all__ = ["PRESETS", "Arena"]


@dataclass(frozen=True)
class Arena:
    """Geometry of a G6 arena.

    Attributes:
        rows: Panel rows.
        cols: Panel columns of the full grid (including uninstalled ones).
        installed_columns: 0-based grid columns that hold panels, or None if
            every column does.
        name: Registry name, e.g. ``"G6_2x10"`` (empty for custom arenas).
        arena_id: Registry id stored in pattern headers (0 = unspecified).
    """

    rows: int
    cols: int
    installed_columns: tuple[int, ...] | None = None
    name: str = ""
    arena_id: int = 0

    def __post_init__(self) -> None:
        """Validate the geometry."""
        if self.rows < 1 or self.cols < 1:
            raise ValueError(
                f"an arena needs at least one panel, got {self.rows}x{self.cols}"
            )
        if not 0 <= self.arena_id <= 63:
            raise ValueError(f"arena_id must be 0..63, got {self.arena_id}")
        if self.installed_columns is not None:
            cols = tuple(sorted(set(self.installed_columns)))
            if not cols or cols[0] < 0 or cols[-1] >= self.cols:
                raise ValueError(f"installed columns must be within 0..{self.cols - 1}")
            object.__setattr__(self, "installed_columns", cols)

    @classmethod
    def preset(cls, name: str) -> Arena:
        """A registered arena by name.

        Raises:
            KeyError: If the name is unknown; the message lists the presets.
        """
        try:
            return PRESETS[name]
        except KeyError:
            raise KeyError(
                f"unknown arena {name!r}; presets: {', '.join(PRESETS)}"
            ) from None

    @classmethod
    def from_id(cls, arena_id: int) -> Arena | None:
        """The preset with this registry id, or None."""
        for arena in PRESETS.values():
            if arena.arena_id == arena_id:
                return arena
        return None

    @property
    def panel_count(self) -> int:
        """Panels in the full grid."""
        return self.rows * self.cols

    @property
    def pixel_shape(self) -> tuple[int, int]:
        """``(height, width)`` in pixels of a full-grid image."""
        return self.rows * PANEL_SIZE, self.cols * PANEL_SIZE

    @property
    def columns(self) -> tuple[int, ...]:
        """Installed columns (all of them for a full arena)."""
        return self.installed_columns or tuple(range(self.cols))

    @property
    def is_partial(self) -> bool:
        """True if some grid columns have no panels."""
        return len(self.columns) < self.cols

    def __str__(self) -> str:
        """The registry name, or ``"RxC"`` for a custom arena."""
        return self.name or f"{self.rows}x{self.cols}"


def _preset(
    name: str, arena_id: int, rows: int, cols: int, installed: range | None = None
) -> Arena:
    return Arena(rows, cols, tuple(installed) if installed else None, name, arena_id)


#: Built-in G6 arenas (from the maDisplayTools registry, as used by the JS tools).
PRESETS: dict[str, Arena] = {
    a.name: a
    for a in (
        _preset("G6_2x10", 1, 2, 10),
        _preset("G6_2x8of10", 2, 2, 10, range(1, 9)),
        _preset("G6_3x12of18", 3, 3, 18, range(0, 12)),
        _preset("G6_3x16_full", 4, 3, 16),
        _preset("G6_3x10", 5, 3, 10),
        _preset("G6_4x10", 6, 4, 10),
        _preset("G6_1x12of18", 7, 1, 18, range(0, 12)),
        _preset("G6_3x10of14", 8, 3, 14, range(0, 10)),
    )
}
