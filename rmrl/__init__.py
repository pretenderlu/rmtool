"""Embedded rmrl renderer for rmtool.

The original `rmrl` project is an optional third-party dependency.  This file
provides a lightweight, pure-Python implementation that understands the `.rm`
file format well enough to produce high quality PDF exports without requiring
users to install extra tooling.  Only the functionality required by rmtool is
implemented and the renderer intentionally errs on the side of robustness – if a
page cannot be parsed, a descriptive :class:`RmrlError` is raised so callers can
fall back to alternative export strategies.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

__all__ = ["RmrlError", "render_notebook_to_pdf"]


class RmrlError(RuntimeError):
    """Raised when the embedded renderer fails to create a PDF."""


@dataclass
class Segment:
    x: float
    y: float
    width: float
    pressure: float
    tilt: float


@dataclass
class Stroke:
    color: int
    brush: int
    segments: List[Segment]


@dataclass
class Layer:
    strokes: List[Stroke]


# Brush definitions loosely based on the public rmrl implementation.  Values are
# tuned to keep perceived stroke widths close to the original device output.
_BRUSH_SCALE = {
    0: 1.0,  # ballpoint
    1: 1.2,  # marker
    2: 1.0,  # fineliner
    3: 1.8,  # pencil
    4: 1.6,  # mechanical pencil
    5: 1.4,  # paintbrush
    6: 1.0,  # highlighter
    7: 1.0,  # eraser
    8: 1.0,  # pen (calligraphy)
    9: 1.0,  # tilt pencil
}

_COLOR_MAP = {
    0: 0,    # black
    1: 110,  # grey
    2: 255,  # white / transparent
}


class _NotebookSource:
    """Normalise different notebook sources to a temporary working directory."""

    def __init__(self, source: str, workspace: Optional[str] = None) -> None:
        self._tempdir: Optional[str] = None
        path = Path(source)
        if path.is_file() and zipfile.is_zipfile(path):
            self._tempdir = tempfile.mkdtemp(prefix="rmrl_", dir=workspace)
            with zipfile.ZipFile(path) as archive:
                archive.extractall(self._tempdir)
            root = Path(self._tempdir)
            children = [child for child in root.iterdir() if not child.name.startswith("__MACOSX")]
            if len(children) == 1 and children[0].is_dir():
                root = children[0]
        elif path.is_dir():
            root = path
        else:
            raise RmrlError(f"不支持的 rm 源：{source}")
        self.root = root

    def cleanup(self) -> None:
        if self._tempdir and os.path.exists(self._tempdir):
            shutil.rmtree(self._tempdir, ignore_errors=True)


def _load_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _collect_pages(root: Path) -> Tuple[List[Path], Optional[Tuple[int, int]]]:
    content_files = list(root.rglob("*.content"))
    size_hint: Optional[Tuple[int, int]] = None
    if content_files:
        content = _load_json(content_files[0]) or {}
        pages = content.get("pages")
        size_info = content.get("dimensions") or content.get("pageDimensions")
        if isinstance(size_info, Sequence) and len(size_info) == 2:
            try:
                size_hint = (int(size_info[0]), int(size_info[1]))
            except (TypeError, ValueError):
                size_hint = None
        if isinstance(pages, Sequence):
            resolved: List[Path] = []
            for identifier in pages:
                candidates = [
                    next(root.rglob(f"{identifier}.rm"), None),
                    next(root.rglob(f"page-{identifier}.rm"), None),
                ]
                candidate = next((c for c in candidates if c and c.exists()), None)
                if candidate:
                    resolved.append(candidate)
            if resolved:
                return resolved, size_hint
    rm_files = sorted(root.rglob("*.rm"))
    return rm_files, size_hint


def _read_uint32(data: memoryview, offset: int) -> Tuple[int, int]:
    return struct.unpack_from("<I", data, offset)[0], offset + 4


def _read_float(data: memoryview, offset: int) -> Tuple[float, int]:
    return struct.unpack_from("<f", data, offset)[0], offset + 4


def _parse_segments(data: memoryview, count: int, offset: int) -> Tuple[List[Segment], int]:
    segments: List[Segment] = []
    for _ in range(count):
        x, offset = _read_float(data, offset)
        y, offset = _read_float(data, offset)
        _speed, offset = _read_float(data, offset)
        _direction, offset = _read_float(data, offset)
        width, offset = _read_float(data, offset)
        pressure, offset = _read_float(data, offset)
        tilt, offset = _read_float(data, offset)
        segments.append(Segment(x=x, y=y, width=width, pressure=pressure, tilt=tilt))
    return segments, offset


def _parse_rm(path: Path) -> Tuple[List[Layer], Tuple[float, float, float, float]]:
    with path.open("rb") as handle:
        raw = handle.read()
    if len(raw) < 8:
        raise RmrlError("rm 文件体积异常")
    data = memoryview(raw)
    offset = 0
    version, offset = _read_uint32(data, offset)
    if version not in {5, 6, 7, 8, 9}:
        raise RmrlError(f"不支持的 rm 版本：{version}")
    layer_count, offset = _read_uint32(data, offset)
    layers: List[Layer] = []
    min_x = float("inf")
    min_y = float("inf")
    max_x = 0.0
    max_y = 0.0
    for _ in range(layer_count):
        stroke_count, offset = _read_uint32(data, offset)
        strokes: List[Stroke] = []
        for _ in range(stroke_count):
            brush_type, offset = _read_uint32(data, offset)
            color, offset = _read_uint32(data, offset)
            _reserved, offset = _read_uint32(data, offset)
            # Brush parameters – they are not currently used but we must advance
            # over them to keep the reader aligned with the binary structure.
            _base_size, offset = _read_float(data, offset)
            _scale, offset = _read_float(data, offset)
            _rotation, offset = _read_float(data, offset)
            _unknown2, offset = _read_float(data, offset)
            segment_count, offset = _read_uint32(data, offset)
            segments, offset = _parse_segments(data, segment_count, offset)
            if not segments:
                continue
            for segment in segments:
                min_x = min(min_x, segment.x)
                min_y = min(min_y, segment.y)
                max_x = max(max_x, segment.x)
                max_y = max(max_y, segment.y)
            strokes.append(Stroke(color=color, brush=brush_type, segments=segments))
        layers.append(Layer(strokes=strokes))
    if not layers:
        raise RmrlError("rm 文件中没有可绘制图层")
    if math.isinf(min_x) or math.isinf(min_y):
        min_x, min_y = 0.0, 0.0
    return layers, (min_x, min_y, max_x, max_y)


def _render_layer(
    draw: ImageDraw.ImageDraw,
    layer: Layer,
    scale: float,
    color_scale: float,
) -> None:
    for stroke in layer.strokes:
        if len(stroke.segments) < 2:
            continue
        color_value = _COLOR_MAP.get(stroke.color, 0)
        if color_value == 255:
            continue  # white strokes are invisible on a white background
        brush_scale = _BRUSH_SCALE.get(stroke.brush, 1.0) * color_scale
        points = [(segment.x * scale, segment.y * scale) for segment in stroke.segments]
        widths = [max(0.4, segment.width) * scale * brush_scale for segment in stroke.segments]
        for start, end, width_a, width_b in zip(points, points[1:], widths, widths[1:]):
            width = max(0.4, (width_a + width_b) / 2.0)
            draw.line([start, end], fill=color_value, width=int(max(1, round(width))))


def _render_page(
    layers: List[Layer],
    bounds: Tuple[float, float, float, float],
    size_hint: Optional[Tuple[int, int]],
) -> Image.Image:
    min_x, min_y, max_x, max_y = bounds
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    if size_hint:
        target_w, target_h = size_hint
    else:
        target_w = int(math.ceil(width)) or 1404
        target_h = int(math.ceil(height)) or 1872
    scale_x = target_w / width
    scale_y = target_h / height
    scale = min(scale_x, scale_y)
    canvas_w = max(1, int(round(width * scale)))
    canvas_h = max(1, int(round(height * scale)))
    image = Image.new("L", (canvas_w, canvas_h), color=255)
    draw = ImageDraw.Draw(image)
    color_scale = max(1.0, min(scale, 4.0))
    for layer in layers:
        _render_layer(draw, layer, scale, color_scale)
    return image


def render_notebook_to_pdf(source: str, output_pdf: str, workspace: Optional[str] = None) -> None:
    """Render a notebook (directory or archive) into a multi-page PDF."""

    notebook = _NotebookSource(source, workspace)
    try:
        pages, size_hint = _collect_pages(notebook.root)
        if not pages:
            raise RmrlError("未找到任何 .rm 页面")
        images: List[Image.Image] = []
        for page in pages:
            layers, bounds = _parse_rm(page)
            image = _render_page(layers, bounds, size_hint)
            images.append(image.convert("RGB"))
        if not images:
            raise RmrlError("没有可用于导出的页面")
        first, *rest = images
        first.save(output_pdf, "PDF", resolution=264.0, save_all=bool(rest), append_images=rest)
    finally:
        notebook.cleanup()
