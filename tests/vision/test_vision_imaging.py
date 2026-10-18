"""Frame comparison and encoding: the arithmetic that has to be right.

`vertical_shift` is the one measurement in this layer that produces a NUMBER a
caller may act on ("the track list moved 320 px"), and it is easy to get wrong in
a way that only shows up at the extremes - which is what happened:

  a negative shift beyond the band length made `rows + shift` itself negative, so
  Python read `band_b[:-147]` as \"everything but the last 147 rows\" while the
  partner slice `band_a[147:]` was already empty. The result was a broadcast error
  `(73, 150) (0, 150)` deep inside a numpy call, only for large negative shifts
  (measured 2026-10-18). The sweep test below covers every shift in range, so the
  clamp cannot be lost again.

The confidence rule is pinned too, because \"the peak is not clearly better than
the rest of the curve\" is the difference between a measurement and a guess.
"""

from __future__ import annotations

import io

import pytest
from vision_support import make_shot

from reaper_reapy_mcp.vision import imaging

pytestmark = pytest.mark.skipif(not imaging.HAS_IMAGING,
                                reason=f"numpy/Pillow unavailable: {imaging.IMAGING_ERROR}")


def _shifted_shot(reference: dict, shift_rows: int):
    """THE SAME frame with its content moved DOWN by `shift_rows` rows.

    Built from the reference bytes rather than from a fresh `make_shot` call: a
    helper that regenerates its own pattern silently compares two unrelated
    pictures and reports a nonsense shift with full confidence (measured: it
    answered 80 px for a 0-row shift before this was fixed).
    """
    width, height = reference["size"]
    raw = reference["buf"].raw
    stride = width * 4
    moved = bytes(stride * shift_rows) + raw[:stride * (height - shift_rows)]
    return {"ok": True, "rect": list(reference["rect"]), "size": (width, height),
            "buf": type("B", (), {"raw": moved})(), "rows": height}


def test_the_sweep_covers_every_shift_without_a_shape_error():
    """The regression: a large negative shift must be SKIPPED, never crash."""
    band_rows = int(0.80 * 240) - int(0.25 * 240)     # the band the function uses
    for search in (50, band_rows + 50):
        result = imaging.vertical_shift(make_shot(320, 240, pattern="bars"),
                                        make_shot(320, 240, pattern="gradient"),
                                        search=search)
        assert result is not None, f"search={search} produced no curve at all"
        assert abs(result["shift_px"]) <= search


def test_a_content_shift_is_recovered_with_its_sign():
    # A NON-repeating pattern: the striped one has a 40-row period, so a shift of
    # 24 rows is mathematically indistinguishable from -16 and the assertion
    # below would be about the pattern, not about the measurement.
    reference = make_shot(320, 240, pattern="gradient")
    for moved in (0, 8, 24):
        result = imaging.vertical_shift(reference, _shifted_shot(reference, moved))
        assert result is not None
        assert result["shift_px"] == pytest.approx(moved, abs=2), (
            "content moved DOWN must be reported as a positive shift")


def test_an_identical_frame_is_a_confident_zero_shift():
    frame = make_shot(320, 240, pattern="gradient")
    result = imaging.vertical_shift(frame, frame)
    assert result["shift_px"] == 0
    assert result["cost"] == 0.0
    assert result["reliable"] is True


def test_two_unrelated_frames_report_low_confidence_instead_of_a_number():
    """The honest answer when every lag fits equally badly."""
    result = imaging.vertical_shift(make_shot(320, 240, pattern="bars"),
                                    make_shot(320, 240, pattern="gradient"))
    assert result["reliable"] is False
    assert result["confidence"] < 3.0


def test_similarity_separates_the_same_content_from_a_different_one():
    same = imaging.similarity(make_shot(200, 150), make_shot(200, 150))
    other = imaging.similarity(make_shot(200, 150), make_shot(200, 150, pattern="flat"))
    assert same[0] < 12.0, "the same frame must look the same"
    assert other[0] > 12.0, "a different frame must not"
    assert same[0] < other[0]


def test_encode_image_crops_before_resizing_and_keeps_the_aspect():
    shot = make_shot(800, 600)
    full = imaging.encode_image(shot)
    small = imaging.encode_image(shot, max_width=400)
    cropped = imaging.encode_image(shot, crop="10,20,300,200")
    assert len(full) > len(small)
    with_crop = imaging._PILImage.open(io.BytesIO(cropped))
    assert with_crop.size == (300, 200)
    resized = imaging._PILImage.open(io.BytesIO(small))
    assert resized.size == (400, 300), "the aspect ratio must survive the resize"


def test_encode_image_refuses_a_malformed_crop_with_a_readable_message():
    shot = make_shot(80, 60)
    for bad in ("10,20,30", "a,b,c,d", "0,0,0,10"):
        with pytest.raises(ValueError) as error:
            imaging.encode_image(shot, crop=bad)
        assert "crop" in str(error.value)


def test_encode_image_clamps_a_crop_that_runs_past_the_frame():
    shot = make_shot(80, 60)
    data = imaging.encode_image(shot, crop="70,50,1000,1000")
    image = imaging._PILImage.open(io.BytesIO(data))
    assert image.size == (10, 10)


def test_a_buffer_that_disagrees_with_its_size_is_refused_not_guessed():
    """A window resized between the rect and the read must not become a picture."""
    shot = {"size": (100, 100), "buf": type("B", (), {"raw": b"too short"})(),
            "rect": [0, 0, 100, 100]}
    with pytest.raises(ValueError) as error:
        imaging.encode_image(shot)
    assert "changed size" in str(error.value)


def test_the_contact_sheet_captions_each_tile_and_stacks_the_grid():
    items = [{"shot": make_shot(200, 150, origin=index), "caption": f"#{index}"}
             for index in range(4)]
    sheet = imaging.contact_sheet(items, columns=2, tile_width=100)
    image = imaging._PILImage.open(io.BytesIO(sheet))
    assert image.width == 200 and image.height > 2 * 150 * (100 / 200)
    assert imaging.contact_sheet([]) is None
    assert imaging.grid_shape(4, 2) == (2, 2)
    assert imaging.grid_shape(5, 2) == (2, 3)
    assert imaging.grid_shape(0, 2) == (1, 1)


def test_the_requested_column_count_is_the_column_count_that_is_drawn():
    """The regression: a request for 2 columns must not produce 4.

    Measured live on an 8-tile sheet: `columns=2` produced a sheet 4 tiles wide,
    because `grid_shape` answers (columns, rows) and the caller unpacked it the
    other way round. Geometry is asserted for several counts, including rows that
    are not full.
    """
    for count, columns in ((2, 2), (4, 2), (5, 3), (6, 1), (8, 4)):
        items = [{"shot": make_shot(120, 90, origin=index), "caption": str(index)}
                 for index in range(count)]
        sheet = imaging.contact_sheet(items, columns=columns, tile_width=120,
                                     caption_height=0)
        image = imaging._PILImage.open(io.BytesIO(sheet))
        expected_columns = max(1, min(columns, count))
        expected_rows = -(-count // expected_columns)
        assert image.width == expected_columns * 120, (count, columns)
        assert image.height == expected_rows * 90, (count, columns)
