"""Pattern encoding: pixel layout, file format, fitting to the firmware grid."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from arena_interface import (
    Arena,
    Capability,
    Device,
    GrayLevel,
    Pattern,
    ProtocolError,
)
from arena_interface.protocol import ArenaState
from arena_interface.testing import FakeController

DATA = Path(__file__).parent / "data"


def _image(rows: int, cols: int, value, frame: int = 0) -> list[list[int]]:
    """Bottom-origin image (row 0 = bottom), as the JS reference generator uses."""
    h, w = rows * 20, cols * 20
    return [[value(x, y, frame) for x in range(w)] for y in range(h)]


# -- byte-for-byte agreement with the JavaScript encoder ---------------------


def test_matches_js_gs16() -> None:
    frames = [
        _image(2, 10, lambda x, y, f: (x * 7 + y * 3 + f) % 16, f) for f in range(3)
    ]
    p = Pattern.from_frames(
        frames,
        arena=Arena.preset("G6_2x10"),
        origin="lower",
        duty_cycle=[128, 200, 50],
    )
    assert p.to_bytes() == (DATA / "js_gs16_2x10.pat").read_bytes()


def test_matches_js_gs2() -> None:
    value = lambda x, y, f: 1 if (x + 2 * y + f) % 3 == 0 else 0  # noqa: E731
    frames = [_image(2, 10, value, f) for f in range(2)]
    p = Pattern.from_frames(
        frames,
        gray_level=GrayLevel.GS2,
        arena=Arena.preset("G6_2x10"),
        origin="lower",
        duty_cycle=[255, 1],
    )
    assert p.to_bytes() == (DATA / "js_gs2_2x10.pat").read_bytes()


def test_matches_js_4x12() -> None:
    value = lambda x, y, f: 15 if (y < 20 and x < 20) else ((x ^ y) & 15)  # noqa: E731
    p = Pattern.from_frames([_image(4, 12, value)], origin="lower")
    assert p.to_bytes() == (DATA / "js_gs16_4x12.pat").read_bytes()


@pytest.mark.parametrize(
    "name", ["js_gs16_2x10.pat", "js_gs2_2x10.pat", "js_gs16_4x12.pat"]
)
def test_round_trip_js_files(name: str) -> None:
    data = (DATA / name).read_bytes()
    p = Pattern.from_bytes(data)
    assert p.to_bytes() == data


def test_from_bytes_recovers_pixels_and_metadata() -> None:
    p = Pattern.from_bytes((DATA / "js_gs16_2x10.pat").read_bytes())
    assert p.frame_count == 3
    assert p.arena.name == "G6_2x10"
    assert p.duty_cycles == (128, 200, 50)
    bottom_left = p.frame(1, origin="lower")[0][0]
    assert bottom_left == (0 * 7 + 0 * 3 + 1) % 16


# -- pixel layout as the panel firmware reads it ----------------------------


def _panel_firmware_view(
    payload: bytes, rows: int, cols: int
) -> dict[tuple[int, int], list[list[int]]]:
    """Decode a GS16 frame the way reference/panel/message.cpp does.

    Returns {(panel_row_from_bottom, panel_col): matrix} where matrix[i][j] has
    i = 0 at the TOP of the panel (isp_logic.h: "Row 0 = top").
    """
    blocks = payload[4:]
    out = {}
    for k in range(rows * cols):
        block = blocks[k * 203 : (k + 1) * 203]
        data = block[2:202]
        matrix = [[0] * 20 for _ in range(20)]
        pixel_num = 0
        for i in range(20):
            for j in range(20):
                byte = data[pixel_num // 2]
                matrix[i][j] = (byte >> 4) if pixel_num % 2 == 0 else (byte & 0x0F)
                pixel_num += 1
        out[divmod(k, cols)] = matrix
    return out


def test_pixel_placement_matches_panel_firmware() -> None:
    img = [[0] * 200 for _ in range(40)]  # 2x10, row 0 = top
    img[0][0] = 15  # top-left pixel of the arena
    img[39][199] = 9  # bottom-right pixel
    img[5][23] = 4  # top panel row, panel column 1, panel pixel (5, 3)
    panels = _panel_firmware_view(Pattern.from_frames([img]).stream_payload(0), 2, 10)
    assert panels[(1, 0)][0][0] == 15  # top row of panels, first column, panel top-left
    assert (
        panels[(0, 9)][19][19] == 9
    )  # bottom row of panels, last column, bottom-right
    assert panels[(1, 1)][5][3] == 4
    lit = sum(v != 0 for m in panels.values() for row in m for v in row)
    assert lit == 3


def test_origin_lower_flips_rows() -> None:
    img = [[0] * 200 for _ in range(40)]
    img[0][0] = 7
    upper = Pattern.from_frames([img])
    lower = Pattern.from_frames([img], origin="lower")
    assert upper.frame(0)[0][0] == 7
    assert lower.frame(0)[39][0] == 7
    assert lower.frame(0, origin="lower")[0][0] == 7


def test_block_headers_have_valid_parity() -> None:
    img = [[(x + y) % 16 for x in range(200)] for y in range(40)]
    payload = Pattern.from_frames([img]).stream_payload(0)
    for k in range(20):
        block = payload[4 + k * 203 : 4 + (k + 1) * 203]
        ones = bin(block[0] & 0x7F).count("1") + sum(
            bin(b).count("1") for b in block[1:]
        )
        assert (ones % 2) == (block[0] >> 7)  # even parity with the parity bit
        assert block[0] & 0x7F == 1 and block[1] == 0x30


# -- numpy inputs and outputs -----------------------------------------------


def test_numpy_input_and_output() -> None:
    frames = np.zeros((4, 40, 200), dtype=np.uint8)
    frames[:, 0, :] = 15
    p = Pattern.from_frames(frames)
    out = p.to_numpy()
    assert out.shape == (4, 40, 200)
    assert (out == frames).all()
    assert (p.to_numpy(origin="lower")[:, -1, :] == 15).all()


def test_numpy_bool_gs2() -> None:
    frame = np.zeros((40, 200), dtype=bool)
    frame[::2, ::2] = True
    p = Pattern.from_frames([frame], gray_level=GrayLevel.GS2)
    assert p.frame(0)[0][0] == 1 and p.frame(0)[0][1] == 0


def test_frames_are_independent_copies() -> None:
    p = Pattern.from_frames(np.zeros((2, 40, 200), dtype=np.uint8))
    image = p.frame(0)
    assert image.dtype == np.uint8 and image.shape == (40, 200)
    image[0, 0] = 15
    assert p.frame(0)[0, 0] == 0


def test_list_input_matches_array_input() -> None:
    img = [[(x + y) % 16 for x in range(200)] for y in range(40)]
    assert Pattern.from_frames([img]) == Pattern.from_frames(np.array([img]))


@pytest.mark.parametrize(
    ("frames", "error", "match"),
    [
        (np.zeros((1, 40, 200)), TypeError, "float"),
        ([[[0] * 200] * 39 + [[0] * 199]], ValueError, "same size"),
        (np.zeros((40, 200), dtype=np.uint8), ValueError, "3-D"),
        ([[[-1] * 200] * 40], ValueError, "got -1"),
    ],
)
def test_invalid_arrays(frames: object, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        Pattern.from_frames(frames)


# -- validation -------------------------------------------------------------


def _blank(h: int = 40, w: int = 200) -> list[list[int]]:
    return [[0] * w for _ in range(h)]


@pytest.mark.parametrize(
    ("kwargs", "frames", "match"),
    [
        ({}, [], "at least one frame"),
        ({}, [_blank(30, 200)], "whole number"),
        ({"arena": (2, 10)}, [_blank(40, 180)], "180 pixels"),
        ({"gray_level": GrayLevel.GS2}, [[[2] * 200] * 40], "0..1"),
        ({}, [[[16] * 200] * 40], "0..15"),
        ({"duty_cycle": 0}, [_blank()], "1..255"),
        ({"duty_cycle": [128, 128]}, [_blank()], "2 duty cycles"),
        ({"origin": "top"}, [_blank()], "origin"),
        ({"observer_id": 64}, [_blank()], "observer_id"),
    ],
)
def test_invalid_frames(kwargs: dict, frames: list, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Pattern.from_frames(frames, **kwargs)


def test_numpy_shape_checked() -> None:
    with pytest.raises(ValueError, match="shape"):
        Pattern.from_frames([np.zeros((40, 200))[:, :180]], arena=(2, 10))


def test_too_many_panels_for_a_file() -> None:
    rig = Arena.preset("G6_3x12of18")
    p = Pattern.from_frames([_blank(*rig.pixel_shape)], arena=rig)
    with pytest.raises(ValueError, match="at most 48"):
        p.to_bytes()
    assert len(p.fit_to(4, 12).to_bytes()) > 0


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.__setitem__(0, ord("X")), "G6PT"),
        (lambda d: d.__setitem__(17, d[17] ^ 1), "header checksum"),
        (lambda d: d.__setitem__(4, 0x10), "version"),
        (lambda d: d.__setitem__(40, d[40] ^ 1), "frame 0: checksum"),
        (lambda d: d.__delitem__(slice(-10, None)), "truncated"),
    ],
)
def test_corrupt_files(mutate, match: str) -> None:
    data = bytearray((DATA / "js_gs16_2x10.pat").read_bytes())
    mutate(data)
    with pytest.raises(ProtocolError, match=match):
        Pattern.from_bytes(bytes(data))


# -- fitting a rig onto the firmware grid ------------------------------------


def test_fit_3x10_rig_to_4x10_grid_repeats_rows() -> None:
    rig = Arena.preset("G6_3x10")
    h, w = rig.pixel_shape
    img = [
        [y // 20 + 1 for _ in range(w)] for y in range(h)
    ]  # panel row from bottom + 1
    p = Pattern.from_frames([img], arena=rig, origin="lower").fit_to(4, 10)
    fitted = p.frame(0, origin="lower")
    assert len(fitted) == 80
    assert [fitted[r][0] for r in (0, 20, 40, 60)] == [1, 2, 3, 1]


def test_fit_partial_arena_drops_uninstalled_columns() -> None:
    rig = Arena.preset("G6_3x12of18")
    h, w = rig.pixel_shape
    img = [[1 if x < 240 else 2 for x in range(w)] for _ in range(h)]
    fitted = Pattern.from_frames([img], arena=rig).fit_to(4, 12)
    assert fitted.pixel_shape == (80, 240)
    assert {v for row in fitted.frame(0) for v in row} == {1}
    assert fitted.arena.arena_id == rig.arena_id


def test_fit_rejects_rig_that_does_not_fit() -> None:
    p = Pattern.from_frames([_blank(80, 200)])  # 4x10
    with pytest.raises(ValueError, match="does not fit"):
        p.fit_to(2, 10)
    walking = Arena.preset("G6_2x8of10")  # installed columns 1..8
    p = Pattern.from_frames([_blank(*walking.pixel_shape)], arena=walking)
    with pytest.raises(ValueError, match="does not fit"):
        p.fit_to(2, 8)


def test_fit_same_grid_is_identity() -> None:
    p = Pattern.from_frames([_blank()])
    assert p.fit_to(2, 10) is p


# -- arenas -----------------------------------------------------------------


def test_presets() -> None:
    assert Arena.preset("G6_2x8of10").columns == tuple(range(1, 9))
    assert Arena.preset("G6_2x8of10").is_partial
    assert not Arena.preset("G6_4x10").is_partial
    assert Arena.from_id(6) == Arena.preset("G6_4x10")
    assert Arena.from_id(99) is None
    assert Arena.preset("G6_1x12of18").pixel_shape == (20, 360)
    assert str(Arena(2, 3)) == "2x3"
    with pytest.raises(KeyError, match="G6_2x10"):
        Arena.preset("nope")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rows": 0, "cols": 10},
        {"rows": 2, "cols": 10, "installed_columns": (10,)},
        {"rows": 2, "cols": 10, "installed_columns": ()},
        {"rows": 2, "cols": 10, "arena_id": 64},
    ],
)
def test_invalid_arena(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        Arena(**kwargs)


# -- through the Device ------------------------------------------------------


def test_stream_image() -> None:
    ctrl = FakeController()
    img = [[(x // 20) % 16 for x in range(200)] for _ in range(40)]
    with Device.open(simulate=ctrl) as dev:
        dev.stream_image(img)
        assert ctrl.state is ArenaState.STREAMING_FRAME
    sent = ctrl.received[-2]  # last message before the ALL_OFF on close
    assert sent[3:] == Pattern.from_frames([img]).stream_payload(0)


def test_stream_image_from_smaller_rig() -> None:
    ctrl = FakeController(rows=4, cols=10)
    with Device.open(simulate=ctrl) as dev:
        dev.stream_image(_blank(60, 200), arena=Arena.preset("G6_3x10"))
        assert ctrl.state is ArenaState.STREAMING_FRAME
        with pytest.raises(ValueError, match="does not fit"):
            dev.stream_image(_blank(100, 200))


def test_upload_pattern_object_is_fitted_and_playable() -> None:
    ctrl = FakeController(rows=4, cols=10, patterns={})
    rig = Arena.preset("G6_3x10")
    frames = [_blank(*rig.pixel_shape) for _ in range(5)]
    with Device.open(simulate=ctrl) as dev:
        pid = dev.patterns.upload("rig.pat", Pattern.from_frames(frames, arena=rig))
        info = dev.patterns.info(pid)
        assert (info.rows, info.cols, info.frame_count) == (4, 10, 5)
        dev.show_pattern(pid)  # the simulator checks every header and frame CRC
        dev.show_frame(4)
        assert ctrl.state is ArenaState.SHOW_FRAME


def test_stream_image_on_old_firmware_needs_20_panels() -> None:
    ctrl = FakeController(rows=4, cols=10, capabilities=Capability.G6_MODE)
    with (
        Device.open(simulate=ctrl) as dev,
        pytest.raises(ValueError, match="arena_shape"),
    ):
        dev.stream_image(_blank(80, 200))
    with Device.open(simulate=ctrl, arena_shape=(4, 10)) as dev:
        dev.stream_image(_blank(80, 200))
