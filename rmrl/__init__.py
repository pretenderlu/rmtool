"""rmrl rendering helpers embedded in rmtool.

The implementation below is a focused port of the upstream `rmrl` project
(`https://github.com/rschroll/rmrl`).  Only the pieces required by rmtool's
export flow are bundled so that users always get sharp, high-quality PDFs even
when the external package is unavailable.  The rendering pipeline mirrors the
behaviour of the original tool: raw `.rm` pages are parsed, each layer is drawn
with brush-aware thickness, and the result is composited onto a correctly sized
page before being written to a multi-page PDF.
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
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

try:
    import rmscene as _rmscene
    from rmscene import scene_items as _rmscene_items
except ImportError:
    _rmscene = None
    _rmscene_items = None

__all__ = ["RmrlError", "render_notebook_to_pdf"]


class RmrlError(RuntimeError):
    """Raised when rmrl rendering fails."""


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


@dataclass
class PageInfo:
    path: Path
    width: int
    height: int


# Brush scaling factors derived from the official rmrl implementation.  The
# numeric values were copied from upstream to preserve stroke appearance.
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
    3: 210,  # yellow
    4: 150,  # green
    5: 180,  # pink
    6: 90,   # blue
    7: 70,   # red
    8: 125,  # grey overlap
    9: 205,  # highlight
    10: 150,  # green 2
    11: 120,  # cyan
    12: 160,  # magenta
    13: 215,  # yellow 2
}

_DEFAULT_PAGE_SIZE = (1404, 1872)
_SUPER_SAMPLE = 2  # draw at double resolution for smoother output
_RM_HEADER_PREFIX = b"reMarkable .lines file, version="
_RM_PAGE_V3_V5 = struct.Struct("<BBH")
_RM_LAYER = struct.Struct("<I")
_RM_STROKE_V3 = struct.Struct("<IIIfI")
_RM_STROKE_V5 = struct.Struct("<IIIfII")
_RM_SEGMENT_V3_V5 = struct.Struct("<ffffff")

_V6_TOOL_TO_BRUSH = {}
if _rmscene_items is not None:
    _V6_TOOL_TO_BRUSH = {
        int(_rmscene_items.Pen.BALLPOINT_1): 0,
        int(_rmscene_items.Pen.BALLPOINT_2): 0,
        int(_rmscene_items.Pen.MARKER_1): 1,
        int(_rmscene_items.Pen.MARKER_2): 1,
        int(_rmscene_items.Pen.FINELINER_1): 2,
        int(_rmscene_items.Pen.FINELINER_2): 2,
        int(_rmscene_items.Pen.PENCIL_1): 3,
        int(_rmscene_items.Pen.PENCIL_2): 3,
        int(_rmscene_items.Pen.MECHANICAL_PENCIL_1): 4,
        int(_rmscene_items.Pen.MECHANICAL_PENCIL_2): 4,
        int(_rmscene_items.Pen.PAINTBRUSH_1): 5,
        int(_rmscene_items.Pen.PAINTBRUSH_2): 5,
        int(_rmscene_items.Pen.HIGHLIGHTER_1): 6,
        int(_rmscene_items.Pen.HIGHLIGHTER_2): 6,
        int(_rmscene_items.Pen.ERASER): 7,
        int(_rmscene_items.Pen.ERASER_AREA): 7,
        int(_rmscene_items.Pen.CALIGRAPHY): 8,
        int(_rmscene_items.Pen.SHADER): 9,
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


def _normalise_dimensions(dimensions: Sequence[object]) -> Optional[Tuple[int, int]]:
    if len(dimensions) != 2:
        return None
    try:
        width = int(round(float(dimensions[0])))
        height = int(round(float(dimensions[1])))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _iter_content_files(root: Path) -> Iterable[Path]:
    for candidate in root.rglob("*.content"):
        if candidate.is_file():
            yield candidate


def _collect_pages(root: Path) -> List[PageInfo]:
    page_infos: List[PageInfo] = []
    default_size = _DEFAULT_PAGE_SIZE
    content_file = next(iter(_iter_content_files(root)), None)

    page_order: List[str] = []
    if content_file:
        content = _load_json(content_file) or {}
        dimensions = None
        dim_candidates = [
            content.get("dimensions"),
            content.get("pageDimensions"),
            content.get("size"),
        ]
        for candidate in dim_candidates:
            if isinstance(candidate, Sequence):
                dimensions = _normalise_dimensions(candidate)
                if dimensions:
                    break
        if dimensions:
            default_size = dimensions

        pages_field = content.get("pages")
        if isinstance(pages_field, Sequence):
            page_order = [str(identifier) for identifier in pages_field]

    if page_order:
        for identifier in page_order:
            candidates = [
                root / f"{identifier}.rm",
                root / f"page-{identifier}.rm",
                root / f"{identifier}" / f"{identifier}.rm",
            ]
            page_path = next((c for c in candidates if c.exists()), None)
            if page_path:
                page_infos.append(PageInfo(path=page_path, width=default_size[0], height=default_size[1]))

    if not page_infos:
        for page_path in sorted(root.rglob("*.rm")):
            page_infos.append(PageInfo(path=page_path, width=default_size[0], height=default_size[1]))

    return page_infos


def _read_uint32(data: memoryview, offset: int) -> Tuple[int, int]:
    if offset + 4 > len(data):
        raise RmrlError("rm 文件数据不完整（读取 uint32 越界）")
    return struct.unpack_from("<I", data, offset)[0], offset + 4


def _read_float(data: memoryview, offset: int) -> Tuple[float, int]:
    if offset + 4 > len(data):
        raise RmrlError("rm 文件数据不完整（读取 float 越界）")
    return struct.unpack_from("<f", data, offset)[0], offset + 4


def _parse_segments_fallback(data: memoryview, count: int, offset: int) -> Tuple[List[Segment], int]:
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


def _update_bounds(
    segments: Sequence[Segment],
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
) -> Tuple[float, float, float, float]:
    for segment in segments:
        min_x = min(min_x, segment.x)
        min_y = min(min_y, segment.y)
        max_x = max(max_x, segment.x)
        max_y = max(max_y, segment.y)
    return min_x, min_y, max_x, max_y


def _parse_rm_version(raw: bytes) -> Tuple[Optional[int], int]:
    if not raw.startswith(_RM_HEADER_PREFIX):
        return None, 0

    offset = len(_RM_HEADER_PREFIX)
    version_bytes = bytearray()
    while offset < len(raw) and 48 <= raw[offset] <= 57:
        version_bytes.append(raw[offset])
        offset += 1

    if not version_bytes:
        raise RmrlError("rm 文件头部格式异常：缺少版本号")

    try:
        version = int(version_bytes)
    except ValueError:
        raise RmrlError(f"rm 文件版本号异常：{bytes(version_bytes)!r}") from None

    while offset < len(raw) and raw[offset] in b" \r\n\t":
        offset += 1
    return version, offset


def _map_v6_brush(tool: int) -> int:
    return _V6_TOOL_TO_BRUSH.get(int(tool), 0)


def _parse_segments_v3_v5(data: memoryview, count: int, offset: int) -> Tuple[List[Segment], int]:
    segments: List[Segment] = []
    for _ in range(count):
        if offset + _RM_SEGMENT_V3_V5.size > len(data):
            raise RmrlError("rm 文件数据不完整（读取线段越界）")
        x, y, _speed, _direction, width, pressure = _RM_SEGMENT_V3_V5.unpack_from(data, offset)
        offset += _RM_SEGMENT_V3_V5.size
        segments.append(Segment(x=x, y=y, width=width, pressure=pressure, tilt=0.0))
    return segments, offset


def _parse_rm_fallback(data: memoryview, offset: int) -> Tuple[List[Layer], Tuple[float, float, float, float]]:
    _MAX_LAYERS = 500
    _MAX_STROKES = 100_000
    _MAX_SEGMENTS = 500_000
    layer_count, offset = _read_uint32(data, offset)
    if layer_count > _MAX_LAYERS:
        raise RmrlError(f"rm 文件图层数异常（{layer_count}），文件可能已损坏")
    layers: List[Layer] = []
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")
    for _ in range(layer_count):
        stroke_count, offset = _read_uint32(data, offset)
        if stroke_count > _MAX_STROKES:
            raise RmrlError(f"rm 文件笔画数异常（{stroke_count}），文件可能已损坏")
        strokes: List[Stroke] = []
        for _ in range(stroke_count):
            brush_type, offset = _read_uint32(data, offset)
            color, offset = _read_uint32(data, offset)
            _reserved, offset = _read_uint32(data, offset)
            _base_size, offset = _read_float(data, offset)
            _scale, offset = _read_float(data, offset)
            _rotation, offset = _read_float(data, offset)
            _unknown2, offset = _read_float(data, offset)
            segment_count, offset = _read_uint32(data, offset)
            if segment_count > _MAX_SEGMENTS:
                raise RmrlError(f"rm 文件线段数异常（{segment_count}），文件可能已损坏")
            segments, offset = _parse_segments_fallback(data, segment_count, offset)
            if not segments:
                continue
            min_x, min_y, max_x, max_y = _update_bounds(segments, min_x, min_y, max_x, max_y)
            strokes.append(Stroke(color=color, brush=brush_type, segments=segments))
        layers.append(Layer(strokes=strokes))
    if not layers:
        raise RmrlError("rm 文件中没有可绘制图层")
    if math.isinf(min_x) or math.isinf(min_y) or math.isinf(max_x) or math.isinf(max_y):
        min_x, min_y, max_x, max_y = 0.0, 0.0, float(_DEFAULT_PAGE_SIZE[0]), float(_DEFAULT_PAGE_SIZE[1])
    return layers, (min_x, min_y, max_x, max_y)


def _parse_rm_v6(path: Path) -> Tuple[List[Layer], Tuple[float, float, float, float]]:
    if _rmscene is None or _rmscene_items is None:
        raise RmrlError("检测到 rm v6 文件，但当前环境缺少 rmscene 依赖")

    try:
        with path.open("rb") as handle:
            tree = _rmscene.read_tree(handle)
    except Exception as exc:
        raise RmrlError(f"解析 rm v6 文件失败：{exc}") from exc

    strokes: List[Stroke] = []
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")

    for item in tree.walk():
        if not isinstance(item, _rmscene_items.Line):
            continue

        thickness_scale = float(getattr(item, "thickness_scale", 1.0) or 1.0)
        segments: List[Segment] = []
        for point in getattr(item, "points", ()):
            pressure = float(point.pressure)
            if pressure > 1.0:
                pressure /= 255.0
            segments.append(
                Segment(
                    x=float(point.x),
                    y=float(point.y),
                    width=max(0.1, float(point.width) / 4.0 * thickness_scale),
                    pressure=pressure,
                    tilt=0.0,
                )
            )
        if not segments:
            continue

        min_x, min_y, max_x, max_y = _update_bounds(segments, min_x, min_y, max_x, max_y)
        strokes.append(
            Stroke(
                color=int(item.color),
                brush=_map_v6_brush(int(item.tool)),
                segments=segments,
            )
        )

    if not strokes:
        raise RmrlError("rm 文件中没有可绘制图层")
    if math.isinf(min_x) or math.isinf(min_y) or math.isinf(max_x) or math.isinf(max_y):
        min_x, min_y, max_x, max_y = 0.0, 0.0, float(_DEFAULT_PAGE_SIZE[0]), float(_DEFAULT_PAGE_SIZE[1])
    return [Layer(strokes=strokes)], (min_x, min_y, max_x, max_y)


def _parse_rm(path: Path) -> Tuple[List[Layer], Tuple[float, float, float, float]]:
    with path.open("rb") as handle:
        raw = handle.read()
    if len(raw) < 8:
        raise RmrlError("rm 文件体积异常")

    version, offset = _parse_rm_version(raw)
    if version == 6:
        return _parse_rm_v6(path)

    data = memoryview(raw)
    if len(raw) - offset < 8:
        raise RmrlError("rm 文件体积异常")

    if version in (3, 5):
        if offset + _RM_PAGE_V3_V5.size > len(data):
            raise RmrlError("rm 文件数据不完整（读取页面头越界）")
        layer_count, _unused_1, _unused_2 = _RM_PAGE_V3_V5.unpack_from(data, offset)
        offset += _RM_PAGE_V3_V5.size
        layers: List[Layer] = []
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for _ in range(layer_count):
            stroke_count, offset = _read_uint32(data, offset)
            strokes: List[Stroke] = []
            for _ in range(stroke_count):
                if version == 3:
                    if offset + _RM_STROKE_V3.size > len(data):
                        raise RmrlError("rm 文件数据不完整（读取 v3 笔画越界）")
                    brush_type, color, _reserved, _base_width, segment_count = _RM_STROKE_V3.unpack_from(data, offset)
                    offset += _RM_STROKE_V3.size
                else:
                    if offset + _RM_STROKE_V5.size > len(data):
                        raise RmrlError("rm 文件数据不完整（读取 v5 笔画越界）")
                    brush_type, color, _reserved, _base_width, _unknown2, segment_count = _RM_STROKE_V5.unpack_from(data, offset)
                    offset += _RM_STROKE_V5.size
                segments, offset = _parse_segments_v3_v5(data, segment_count, offset)
                if not segments:
                    continue
                min_x, min_y, max_x, max_y = _update_bounds(segments, min_x, min_y, max_x, max_y)
                strokes.append(Stroke(color=color, brush=brush_type, segments=segments))
            layers.append(Layer(strokes=strokes))
        if not layers:
            raise RmrlError("rm 文件中没有可绘制图层")
        if math.isinf(min_x) or math.isinf(min_y) or math.isinf(max_x) or math.isinf(max_y):
            min_x, min_y, max_x, max_y = 0.0, 0.0, float(_DEFAULT_PAGE_SIZE[0]), float(_DEFAULT_PAGE_SIZE[1])
        return layers, (min_x, min_y, max_x, max_y)

    return _parse_rm_fallback(data, offset)


def _render_layer(
    draw: ImageDraw.ImageDraw,
    layer: Layer,
    scale: float,
    offset_x: float,
    offset_y: float,
) -> None:
    for stroke in layer.strokes:
        if len(stroke.segments) < 2:
            continue
        color_value = _COLOR_MAP.get(stroke.color, 0)
        if color_value >= 255:
            continue  # white strokes are invisible on a white background
        brush_scale = _BRUSH_SCALE.get(stroke.brush, 1.0)
        points = [
            (
                segment.x * scale + offset_x,
                segment.y * scale + offset_y,
            )
            for segment in stroke.segments
        ]
        widths = [max(0.35, segment.width * brush_scale) * scale for segment in stroke.segments]
        for start, end, width_a, width_b in zip(points, points[1:], widths, widths[1:]):
            width = max(1.0, (width_a + width_b) / 2.0)
            draw.line([start, end], fill=color_value, width=int(round(width)))


def _render_page(
    page: PageInfo,
    layers: List[Layer],
    bounds: Tuple[float, float, float, float],
) -> Image.Image:
    min_x, min_y, max_x, max_y = bounds
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    canvas_w = max(page.width, 1)
    canvas_h = max(page.height, 1)

    # Draw at a higher resolution to get smoother strokes before scaling down.
    super_w = canvas_w * _SUPER_SAMPLE
    super_h = canvas_h * _SUPER_SAMPLE
    image = Image.new("L", (super_w, super_h), color=255)
    draw = ImageDraw.Draw(image)

    scale_x = super_w / width
    scale_y = super_h / height
    scale = min(scale_x, scale_y)
    offset_x = (-min_x * scale) + (super_w - width * scale) / 2.0
    offset_y = (-min_y * scale) + (super_h - height * scale) / 2.0

    for layer in layers:
        _render_layer(draw, layer, scale, offset_x, offset_y)

    if _SUPER_SAMPLE > 1:
        image = image.resize((canvas_w, canvas_h), Image.LANCZOS)
    return image


def render_notebook_to_pdf(source: str, output_pdf: str, workspace: Optional[str] = None) -> None:
    """Render a notebook (directory or archive) into a multi-page PDF."""

    notebook = _NotebookSource(source, workspace)
    try:
        pages = _collect_pages(notebook.root)
        if not pages:
            raise RmrlError("未找到任何 .rm 页面")
        images: List[Image.Image] = []
        for page in pages:
            layers, bounds = _parse_rm(page.path)
            rendered = _render_page(page, layers, bounds)
            images.append(rendered.convert("RGB"))
        if not images:
            raise RmrlError("没有可用于导出的页面")
        first, *rest = images
        first.save(
            output_pdf,
            "PDF",
            resolution=300.0,
            save_all=bool(rest),
            append_images=rest,
        )
    finally:
        notebook.cleanup()
