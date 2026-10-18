"""Turn a captured frame into bytes an MCP client can show, and compare frames.

numpy and PIL are imported at MODULE level on purpose. FastMCP calls a
synchronous tool directly inside its event loop, and a heavy import performed
lazily there has wedged this workspace's servers twice (documented in
`mcp-stem-splitter` and `mcp-music-analysis`: no answer, no error, no CPU).
Importing at module import time - which happens while the server is being built,
before `mcp.run()` - turns every later use into a `sys.modules` lookup.

The import is guarded: capture, statistics and every JSON field still work
without numpy/PIL, and only the steps that need an image (encoding, contact
sheet, frame comparison) report `HAS_IMAGING == False` with the import error.
"""

from __future__ import annotations

import io
import os

__all__ = [
    "HAS_IMAGING",
    "IMAGING_ERROR",
    "to_rgb_array",
    "encode_png",
    "encode_image",
    "similarity",
    "vertical_shift",
    "tile_image",
    "contact_sheet",
    "contact_sheet_from_images",
    "pil_png_bytes",
    "grid_shape",
]

IMAGING_ERROR: str | None = None
try:  # pragma: no cover - exercised by the import itself in every test run
    import numpy as _np
    from PIL import Image as _PILImage
    from PIL import ImageDraw as _ImageDraw
    from PIL import ImageFont as _ImageFont

    HAS_IMAGING = True
except Exception as exc:  # noqa: BLE001 - a missing optional dependency must not stop the server
    _np = None
    _PILImage = _ImageDraw = _ImageFont = None
    HAS_IMAGING = False
    IMAGING_ERROR = f"{type(exc).__name__}: {exc}"


def _require_imaging() -> None:
    if not HAS_IMAGING:
        raise RuntimeError(
            "image encoding is unavailable: numpy/Pillow could not be imported "
            f"({IMAGING_ERROR}). The captured bytes, the statistics and every "
            "file path are still valid; only the rendered image is missing."
        )


def _buffer_to_array(shot: dict):
    """BGRA top-down -> ``(h, w, 3)`` uint8 array."""
    _require_imaging()
    width, height = shot["size"]
    raw = shot["buf"].raw
    expected = width * height * 4
    if len(raw) != expected:
        raise ValueError(
            f"the captured buffer is {len(raw)} bytes but {width}x{height} needs "
            f"{expected}; the window changed size between the rect and the read"
        )
    # The copy is explicit: PIL refuses a negative-stride view with
    # "tile cannot extend outside image" (measured), and fancy indexing is what
    # makes the result contiguous.
    return _np.frombuffer(raw, dtype=_np.uint8).reshape(height, width, 4)[:, :, [2, 1, 0]]


def to_rgb_array(shot: dict):
    """Public form of `_buffer_to_array`, for callers that need pixels."""
    return _buffer_to_array(shot)


def _crop_box(crop, width: int, height: int):
    """Accept ``"x,y,w,h"``, a 4-item sequence, or None."""
    if crop in (None, "", "none", "full"):
        return None
    values = crop
    if isinstance(crop, str):
        parts = [p for p in crop.replace(" ", "").split(",") if p != ""]
        if len(parts) != 4:
            raise ValueError(f"crop must be 'x,y,w,h' (four integers), got {crop!r}")
        values = parts
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError(f"crop must be 'x,y,w,h' (four integers), got {crop!r}")
    try:
        x, y, w, h = (int(v) for v in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"crop must be 'x,y,w,h' (four integers), got {crop!r}") from exc
    if w <= 0 or h <= 0:
        raise ValueError(f"crop width/height must be positive, got {crop!r}")
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = min(w, width - x)
    h = min(h, height - y)
    return (x, y, x + w, y + h)


def encode_image(shot: dict, *, max_width: int | None = None, crop=None,
                 fmt: str = "png") -> bytes:
    """PNG (or JPEG) bytes of a captured frame, optionally cropped/resized.

    `max_width` is a payload/legibility tradeoff, not a detail: the whole
    REAPER window is 2582 px wide on a maximized 2560-wide display, so a
    full-resolution PNG is ~360 kB. Downscaling shrinks both the payload and
    the text; `crop` is the way to get detail back.
    """
    _require_imaging()
    array = _buffer_to_array(shot)
    height, width = array.shape[0], array.shape[1]
    box = _crop_box(crop, width, height)
    if box:
        x0, y0, x1, y1 = box
        array = array[y0:y1, x0:x1]
    image = _PILImage.fromarray(array, "RGB")
    if max_width and image.width > int(max_width):
        scale = int(max_width) / image.width
        target = (int(max_width), max(1, int(round(image.height * scale))))
        image = image.resize(target, _PILImage.LANCZOS)
    buffer = io.BytesIO()
    if fmt.lower() in ("jpg", "jpeg"):
        image.save(buffer, format="JPEG", quality=88)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def encode_png(shot: dict, *, max_width: int | None = None, crop=None) -> bytes:
    """Backwards-friendly alias for the PNG path."""
    return encode_image(shot, max_width=max_width, crop=crop, fmt="png")


def pil_png_bytes(image) -> bytes:
    """PNG bytes of an already-prepared PIL image (no captured frame involved).

    The MCP `Image` type takes bytes, not an image object, so a tile that was
    downscaled once has to be encoded without going back to the 16 MB frame.
    """
    _require_imaging()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _small(shot: dict, gw: int = 64, gh: int = 40):
    array = _buffer_to_array(shot)
    height, width = array.shape[0], array.shape[1]
    grey = array.mean(axis=2)
    rows = _np.linspace(0, height - 1, gh).astype("int32")
    cols = _np.linspace(0, width - 1, gw).astype("int32")
    return grey[_np.ix_(rows, cols)]


def similarity(shot_a: dict, shot_b: dict) -> list[float]:
    """``[mean grey difference 0-255, percent of samples differing by >32]``.

    Calibrated on the measurement this feature was built from: the same content
    scored 8.1 / 5.9 and a different application 63.5 / 71.8 in the first
    position. Below ~12 means the two frames show the same thing.
    """
    a, b = _small(shot_a), _small(shot_b)
    delta = _np.abs(a - b)
    return [round(float(delta.mean()), 2),
            round(float((delta > 32).mean() * 100), 2)]


def vertical_shift(shot_a: dict, shot_b: dict, *, search: int = 400,
                   col_step: int = 3) -> dict | None:
    """How far the content moved vertically between two frames.

    Positive means the content moved DOWN the screen. Columns 25 %-95 % are
    used because a docked mixer at the left edge does not scroll with the
    tracks, so a full-width strip averages a static band with a moving one.

    Repetitive material (a four-on-the-floor MIDI part is a comb of identical
    rows) makes the correlation curve multi-peaked, and the best lag can then be
    one period off. `confidence` is the median cost over the peak cost: a value
    under 3 means the peak is NOT clearly better than the rest of the curve, and
    `reliable` says so instead of presenting a guess as a measurement.
    """
    _require_imaging()
    a, b = _buffer_to_array(shot_a), _buffer_to_array(shot_b)
    height = min(a.shape[0], b.shape[0])
    width = min(a.shape[1], b.shape[1])
    grey_a = a[:height, :width].mean(axis=2).astype("float32")
    grey_b = b[:height, :width].mean(axis=2).astype("float32")
    x0, x1 = int(0.25 * width), int(0.95 * width)
    y0, y1 = int(0.25 * height), int(0.80 * height)
    band_a = grey_a[y0:y1, x0:x1:col_step]
    band_b = grey_b[y0:y1, x0:x1:col_step]
    rows = band_a.shape[0]
    curve: list[tuple[int, float]] = []
    for shift in range(-search, search + 1):
        # The window is built from CLAMPED bounds on purpose. Expressing it as
        # `band_b[:rows + shift]` / `band_a[-shift:]` looks equivalent and is
        # not: when `-shift` passes the end of the band, `rows + shift` is
        # NEGATIVE and Python reads it as "everything but the last N rows", so
        # one side stays non-empty while its partner is already empty - which is
        # exactly the `(73, 150) (0, 150)` broadcast error this replaced
        # (measured 2026-10-18).
        first = max(0, shift)
        last = max(0, min(rows, rows + shift))
        if last - first < rows // 3 or last <= first:
            continue
        shifted = band_b[first:last]
        reference = band_a[first - shift:last - shift]
        curve.append((shift, float(_np.abs(shifted - reference).mean())))
    if not curve:
        return None
    curve.sort(key=lambda item: item[1])
    best_shift, best_cost = curve[0]
    median_cost = float(_np.median([cost for _, cost in curve]))
    confidence = median_cost / max(best_cost, 1e-6)
    return {"shift_px": best_shift, "cost": round(best_cost, 3),
            "median_cost": round(median_cost, 3),
            "confidence": round(confidence, 2),
            "reliable": confidence > 3.0}


def grid_shape(count: int, columns: int) -> tuple[int, int]:
    columns = max(1, min(int(columns), max(1, count)))
    return columns, max(1, (count + columns - 1) // columns)


def contact_sheet(items: list[dict], *, columns: int = 2, tile_width: int = 640,
                  caption_height: int = 26, background=(18, 18, 18),
                  text_color=(235, 235, 235)) -> bytes | None:
    """One image holding several captured frames, each captioned.

    `items` are ``{"shot": <captured frame>, "caption": str}``. A sheet is how an
    agent looks at a whole arrangement cheaply: the individual frames stay on
    disk, and a single request shows their layout. The caption uses Pillow's
    scalable default font, so no font file has to be installed.

    For a scan, call `tile_image` as each frame arrives and hand the small
    images to `contact_sheet_from_images`: a full-resolution frame is 16 MB
    (2582x1550x4), so holding twenty-four of them to build one sheet is 384 MB
    of RAM for a picture that is then thrown away.
    """
    _require_imaging()
    if not items:
        return None
    images = [tile_image(item["shot"], tile_width=tile_width) for item in items]
    captions = [item.get("caption", f"tile {index}")
                for index, item in enumerate(items)]
    return contact_sheet_from_images(images, captions, columns=columns,
                                     caption_height=caption_height,
                                     background=background, text_color=text_color)


def tile_image(shot: dict, *, tile_width: int = 640, crop=None):
    """One captured frame as a small PIL image (keeps the tile, not the frame).

    `crop` is applied BEFORE the downscale, which is what makes it useful: cut the
    static track panel and mixer away and the remaining arrangement pixels are the
    ones the downscale keeps.
    """
    _require_imaging()
    array = _buffer_to_array(shot)
    box = _crop_box(crop, array.shape[1], array.shape[0])
    if box:
        x0, y0, x1, y1 = box
        array = array[y0:y1, x0:x1]
    image = _PILImage.fromarray(array, "RGB")
    if tile_width and image.width > int(tile_width):
        scale = int(tile_width) / image.width
        image = image.resize((int(tile_width), max(1, int(round(image.height * scale)))),
                             _PILImage.LANCZOS)
    return image


def contact_sheet_from_images(images: list, captions: list[str] | None = None, *,
                              columns: int = 2, caption_height: int = 26,
                              background=(18, 18, 18), text_color=(235, 235, 235)) -> bytes | None:
    """Compose already-downscaled tiles into one captioned PNG."""
    _require_imaging()
    if not images:
        return None
    captions = list(captions or [f"tile {index}" for index in range(len(images))])
    # `grid_shape` answers (columns, rows) - the same order as the `columns`
    # argument and of `sheet_shape()`. Unpacking it as (rows, cols) transposed
    # every sheet: a request for 2 columns produced 4 (caught by looking at a
    # live 8-tile sheet, 2026-10-18).
    cols, rows = grid_shape(len(images), columns)
    tile_width = max(image.width for image in images)
    tile_height = max(image.height for image in images)
    font_size = max(13, min(28, tile_height // 26))
    try:
        font = _ImageFont.load_default(size=font_size)
    except TypeError:  # pragma: no cover - older Pillow takes no size
        font = _ImageFont.load_default()
    sheet = _PILImage.new("RGB", (cols * tile_width, rows * (tile_height + caption_height)),
                          background)
    draw = _ImageDraw.Draw(sheet)
    for index, image in enumerate(images):
        column, row = index % cols, index // cols
        x = column * tile_width
        y = row * (tile_height + caption_height)
        draw.text((x + 8, y + max(2, (caption_height - font_size) // 2)),
                  str(captions[index] if index < len(captions) else f"tile {index}"),
                  fill=text_color, font=font)
        sheet.paste(image, (x, y + caption_height))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue()


def sheet_shape(count: int, columns: int) -> dict:
    """Geometry of the sheet that `contact_sheet` would build (pure math)."""
    cols, rows = grid_shape(count, columns)
    return {"columns": cols, "rows": rows}


def described_by_env() -> dict:
    """Small provenance block: which imaging stack answered this call."""
    return {"numpy": getattr(_np, "__version__", None) if HAS_IMAGING else None,
            "pillow": getattr(_PILImage, "__version__", None) if HAS_IMAGING else None,
            "available": HAS_IMAGING, "error": IMAGING_ERROR,
            "pid": os.getpid()}
