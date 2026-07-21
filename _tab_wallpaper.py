"""WallpaperTab extracted from rmtool.py."""

import hashlib
import logging
import os
import posixpath
import random
import tempfile
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, List, Optional, Sequence, Set, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps
from PyQt5 import QtCore, QtGui, QtWidgets

from _dialogs import show_error, show_info, show_warning
from _ssh import SSHClientWrapper, remount_rw, require_connection
import rmtool as _rmtool  # late-bound access to avoid circular import

_LEGACY_WALLPAPER_PATHS = {
    "/usr/share/remarkable/hibernate.png": "/usr/share/remarkable/suspended.png",
}
_CAROUSEL_DIR = "/usr/share/remarkable/carousel"
_CAROUSEL_BACKUP_SUFFIX = ".backup"
_MONOCHROME_DEVICE_PROFILES = {
    "reMarkable Paper Pure",
    "reMarkable 1",
    "reMarkable 2",
}
_DEVICE_FRAME_PROFILES = {
    # (asset, normalized portrait screen rectangle)
    "reMarkable Paper Pro": (
        "paper-pro.png",
        (43 / 973, 44 / 1355, 930 / 973, 1226 / 1355),
    ),
    "reMarkable Paper Pro Move": (
        "paper-pro-move.png",
        (83 / 1069, 85 / 1937, 988 / 1069, 1693 / 1937),
    ),
    "reMarkable Paper Pure": (
        "paper-pure.png",
        (224 / 2003, 100 / 2456, 1912 / 2003, 2353 / 2456),
    ),
    "reMarkable 1": (
        "remarkable-1.png",
        (88 / 1634, 178 / 2365, 1548 / 1634, 2117 / 2365),
    ),
    "reMarkable 2": (
        "remarkable-2.png",
        (206 / 1850, 88 / 2428, 1763 / 1850, 2163 / 2428),
    ),
}
_MAX_COVER_WALL_ITEMS = 12
_COVER_WALL_LAYOUTS = (
    ("hero_obi", "F / 主书腰封"),
    ("poster_wall", "G / 满格海报墙"),
    ("poster_wall_tilt_left", "H / 左倾海报墙"),
    ("poster_wall_tilt_right", "H / 右倾海报墙"),
)


@dataclass(frozen=True)
class _WallpaperPreviewResult:
    data: Optional[bytes] = None
    missing: bool = False


@dataclass
class _WallpaperResourceScan:
    available_paths: Set[str]
    complete: bool
    carousel_backed_up: bool = False


def _is_transparent_placeholder(data: bytes) -> bool:
    """Detect the tiny transparent PNG written by ``_clear_carousel_overlays``.

    Uploading a suspended wallpaper rewrites every carousel overlay on the
    device to a fully transparent 1x1 PNG so the stock illustrations stop
    covering the custom sleep screen.  Such files load as valid pixmaps but
    render as an empty card, so the preview shows a note instead.
    """
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 4:
                return False
            alpha_extrema = image.convert("RGBA").getextrema()[3]
            return alpha_extrema == (0, 0)
    except Exception:
        return False


@dataclass(frozen=True)
class _CoverWallEntry:
    item: _rmtool.DocumentItem
    cover: Optional[bytes]


def compose_device_frame_preview(
    wallpaper: Image.Image,
    frame: Image.Image,
    screen_rect: Tuple[float, float, float, float],
    orientation: str,
) -> Image.Image:
    """Place a processed wallpaper beneath a device frame at native size."""
    if orientation not in {"portrait", "landscape"}:
        raise ValueError("未知壁纸方向")
    left_n, top_n, right_n, bottom_n = screen_rect
    if not (
        0 <= left_n < right_n <= 1
        and 0 <= top_n < bottom_n <= 1
    ):
        raise ValueError("真机预览屏幕区域无效")

    device_frame = frame.convert("RGBA")
    body_size = device_frame.size
    left = round(body_size[0] * left_n)
    top = round(body_size[1] * top_n)
    right = round(body_size[0] * right_n)
    bottom = round(body_size[1] * bottom_n)
    screen = wallpaper.convert("RGBA")
    if orientation == "landscape":
        screen = screen.transpose(Image.Transpose.ROTATE_90)
    screen = ImageOps.fit(
        screen,
        (right - left, bottom - top),
        method=Image.Resampling.LANCZOS,
    )

    device = Image.new("RGBA", body_size, (0, 0, 0, 0))
    device.alpha_composite(screen, (left, top))
    device.alpha_composite(device_frame)
    if orientation == "landscape":
        device = device.transpose(Image.Transpose.ROTATE_270)
    return device


def _usable_cover_data(data: Optional[bytes]) -> Optional[bytes]:
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        return data
    except Exception:
        return None


def _fit_cover_wall_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    max_width: int,
    start_size: int,
    minimum_size: int,
) -> Tuple[str, ImageFont.FreeTypeFont]:
    text = " ".join(text.split())
    for size in range(start_size, minimum_size - 1, -2):
        font = ImageFont.truetype(font_path, size)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return text, font

    font = ImageFont.truetype(font_path, minimum_size)
    ellipsis = "…"
    shortened = text
    while shortened:
        box = draw.textbbox((0, 0), shortened + ellipsis, font=font)
        if box[2] - box[0] <= max_width:
            break
        shortened = shortened[:-1]
    return (shortened + ellipsis if shortened != text else text), font


def _poster_wall_grid_shape(
    cover_count: int,
    size: Tuple[int, int],
) -> Tuple[int, int]:
    target_aspect = size[0] / size[1]
    tile_count = cover_count + 1
    candidates = []
    for rows in (3, 4):
        minimum_columns = (tile_count + rows - 1) // rows
        aspect_columns = max(1, round(target_aspect * rows / 0.72))
        columns = max(minimum_columns, aspect_columns)
        slots = rows * columns
        repeat_ratio = (slots - tile_count) / slots
        grid_aspect = columns * 0.72 / rows
        aspect_error = abs(grid_aspect - target_aspect) / target_aspect
        candidates.append((repeat_ratio + aspect_error, slots, rows, columns))
    _, _, rows, columns = min(candidates)
    return rows, columns


def _poster_wall_assignments(
    cover_count: int,
    rows: int,
    columns: int,
    title_index: int,
    seed: int,
) -> List[Optional[int]]:
    if cover_count == 2:
        parity_counts = [
            sum(
                index != title_index
                and (index // columns + index % columns) % 2 == parity
                for index in range(rows * columns)
            )
            for parity in (0, 1)
        ]
        if abs(parity_counts[0] - parity_counts[1]) <= 1:
            palette = [0, 1]
            random.Random(seed).shuffle(palette)
            return [
                None
                if index == title_index
                else palette[(index // columns + index % columns) % 2]
                for index in range(rows * columns)
            ]

    best: List[Optional[int]] = []
    best_conflicts = rows * columns
    for attempt in range(128):
        rng = random.Random(seed + attempt)
        assignments: List[Optional[int]] = [None] * (rows * columns)
        counts = [0] * cover_count
        unseen = list(range(cover_count))
        rng.shuffle(unseen)

        for index in range(rows * columns):
            if index == title_index:
                continue
            if unseen:
                candidates = unseen
            else:
                minimum_count = min(counts)
                candidates = [
                    cover_index
                    for cover_index, count in enumerate(counts)
                    if count == minimum_count
                ]
            neighbors = set()
            if index % columns:
                neighbors.add(assignments[index - 1])
            if index >= columns:
                neighbors.add(assignments[index - columns])
            nonmatching = [
                candidate for candidate in candidates if candidate not in neighbors
            ]
            if nonmatching:
                candidates = nonmatching
            choice = rng.choice(candidates)
            assignments[index] = choice
            counts[choice] += 1
            if unseen:
                unseen.remove(choice)

        conflicts = sum(
            assignments[index] is not None
            and (
                (index % columns and assignments[index] == assignments[index - 1])
                or (
                    index >= columns
                    and assignments[index] == assignments[index - columns]
                )
            )
            for index in range(rows * columns)
        )
        if conflicts < best_conflicts:
            best = assignments
            best_conflicts = conflicts
        if not conflicts:
            break

    return best

def compose_cover_wallpaper(
    covers: Sequence[bytes],
    size: Tuple[int, int],
    title: str = "我的书架",
    subtitle: str = "",
    *,
    layout: str = "hero_obi",
    monochrome: bool = False,
    font_path: Optional[str] = None,
) -> Image.Image:
    """Compose selected covers using one of the fixed cover-wall layouts."""
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("壁纸尺寸无效")
    if not 1 <= len(covers) <= _MAX_COVER_WALL_ITEMS:
        raise ValueError("请选择 1 到 12 个封面")
    if layout not in {layout_id for layout_id, _label in _COVER_WALL_LAYOUTS}:
        raise ValueError("未知封面墙排版")

    decoded: List[Image.Image] = []
    cover_digest = hashlib.sha256()
    for cover in covers:
        try:
            with Image.open(BytesIO(cover)) as image:
                decoded.append(image.convert("RGB"))
            cover_digest.update(len(cover).to_bytes(8, "big"))
            cover_digest.update(cover)
        except Exception:
            continue
    if not decoded:
        raise ValueError("所选文档没有可用封面")

    font_file = font_path or str(
        _rmtool.resource_path("assets", "fonts", "NotoSansCJKsc-Regular.otf")
    )
    if not os.path.isfile(font_file):
        raise RuntimeError("缺少封面墙中文字体")

    shortest = min(width, height)
    border = max(2, shortest // 450)

    def compose_poster_wall(angle: float) -> Image.Image:
        background = (242, 241, 237)
        gutter = max(4, round(shortest * 0.006))
        rows, columns = _poster_wall_grid_shape(len(decoded), size)
        overscan = 1.24
        base_height = max(
            (height * overscan - rows * gutter) / rows,
            ((width * overscan - columns * gutter) / columns) / 0.72,
        )
        base_width = base_height * 0.72
        cell_width = base_width + gutter
        cell_height = base_height + gutter
        wall = Image.new(
            "RGB",
            (round(columns * cell_width), round(rows * cell_height)),
            background,
        )

        title_row = round((rows - 1) * 0.30)
        title_column = round((columns - 1) * 0.30)
        title_index = title_row * columns + title_column
        assignments = _poster_wall_assignments(
            len(decoded),
            rows,
            columns,
            title_index,
            int.from_bytes(cover_digest.digest()[:8], "big"),
        )
        scale_pattern = (1.0, 0.96, 0.99, 0.94, 0.98, 0.95)
        x_alignment = (0.15, 0.75, 0.40, 0.65, 0.25, 0.85)
        y_alignment = (0.70, 0.20, 0.55, 0.10, 0.85, 0.35)

        for index, cover_index in enumerate(assignments):
            scale = 0.98 if cover_index is None else scale_pattern[index % 6]
            poster_width = max(24, round(base_width * scale))
            poster_height = max(32, round(base_height * scale))
            column = index % columns
            row = index // columns
            position = (
                round(
                    column * cell_width
                    + (base_width - poster_width) * x_alignment[index % 6]
                ),
                round(
                    row * cell_height
                    + (base_height - poster_height) * y_alignment[index % 6]
                ),
            )

            if cover_index is None:
                poster = Image.new(
                    "RGB",
                    (poster_width, poster_height),
                    (250, 248, 242),
                )
                draw = ImageDraw.Draw(poster)
                padding = max(6, int(poster_width * 0.08))
                title_text, title_font = _fit_cover_wall_text(
                    draw,
                    title or "我的书架",
                    font_file,
                    poster_width - padding * 2,
                    max(16, int(poster_height * 0.12)),
                    max(11, int(poster_height * 0.055)),
                )
                draw.text(
                    (padding, int(poster_height * 0.20)),
                    title_text,
                    fill=(24, 27, 31),
                    font=title_font,
                )

                subtitle_value = subtitle.strip()
                if subtitle_value:
                    subtitle_text, subtitle_font = _fit_cover_wall_text(
                        draw,
                        subtitle_value,
                        font_file,
                        poster_width - padding * 2,
                        max(11, int(poster_height * 0.055)),
                        max(9, int(poster_height * 0.035)),
                    )
                    draw.text(
                        (padding, int(poster_height * 0.52)),
                        subtitle_text,
                        fill=(70, 72, 75),
                        font=subtitle_font,
                    )

                count_text = f"{len(decoded):02d} 本"
                count_font = ImageFont.truetype(
                    font_file,
                    max(10, int(poster_height * 0.07)),
                )
                count_box = draw.textbbox((0, 0), count_text, font=count_font)
                count_width = count_box[2] - count_box[0]
                count_y = (
                    poster_height
                    - padding
                    - (count_box[3] - count_box[1])
                    - count_box[1]
                )
                draw.text(
                    (poster_width - padding - count_width, count_y),
                    count_text,
                    fill=(24, 27, 31),
                    font=count_font,
                )
            else:
                poster = ImageOps.fit(
                    decoded[cover_index],
                    (poster_width, poster_height),
                    method=Image.LANCZOS,
                    centering=(0.5, 0.5),
                )

            ImageDraw.Draw(poster).rectangle(
                (0, 0, poster_width - 1, poster_height - 1),
                outline=(218, 216, 210),
                width=border,
            )
            wall.paste(poster, position)

        if angle:
            wall = wall.rotate(
                angle,
                resample=Image.BICUBIC,
                expand=True,
                fillcolor=background,
            )
        left = (wall.width - width) // 2
        top = (wall.height - height) // 2
        return wall.crop((left, top, left + width, top + height))
    if layout != "hero_obi":
        angle = {
            "poster_wall": 0.0,
            "poster_wall_tilt_left": 8.0,
            "poster_wall_tilt_right": -8.0,
        }[layout]
        output = compose_poster_wall(angle)
        if monochrome:
            return ImageOps.autocontrast(ImageOps.grayscale(output)).convert("RGB")
        return output

    shadow = max(4, shortest // 240)
    hero_height = int(min(height * 0.78, width * 0.92 / 0.72))
    hero_width = int(hero_height * 0.72)
    canvas = Image.new("RGBA", size, (242, 241, 237, 255))

    def make_card(
        image: Image.Image,
        card_width: int,
        card_height: int,
        *,
        with_obi: bool = False,
    ) -> Image.Image:
        pad = shadow * 2
        layer = Image.new(
            "RGBA",
            (card_width + pad * 2, card_height + pad * 2),
            (0, 0, 0, 0),
        )
        draw = ImageDraw.Draw(layer)
        draw.rectangle(
            (
                pad + shadow,
                pad + shadow,
                pad + card_width + shadow,
                pad + card_height + shadow,
            ),
            fill=(24, 26, 29, 42),
        )
        fitted = ImageOps.fit(
            image,
            (card_width, card_height),
            method=Image.LANCZOS,
            centering=(0.5, 0.5),
        )
        layer.paste(fitted, (pad, pad))
        draw.rectangle(
            (pad, pad, pad + card_width - 1, pad + card_height - 1),
            outline=(255, 255, 255, 255),
            width=border,
        )

        if with_obi:
            band_height = max(72, int(card_height * 0.205))
            band_top = pad + int(card_height * 0.57)
            band_bottom = band_top + band_height
            band_padding = max(12, int(card_width * 0.06))
            draw.rectangle(
                (pad, band_top, pad + card_width, band_bottom),
                fill=(250, 248, 242, 255),
            )
            draw.line(
                (pad, band_top, pad + card_width, band_top),
                fill=(210, 207, 199, 255),
                width=border,
            )
            draw.line(
                (pad, band_bottom, pad + card_width, band_bottom),
                fill=(210, 207, 199, 255),
                width=border,
            )

            title_text, title_font = _fit_cover_wall_text(
                draw,
                title or "我的书架",
                font_file,
                int(card_width * 0.64),
                max(24, int(card_height * 0.075)),
                max(16, int(card_height * 0.035)),
            )
            title_box = draw.textbbox((0, 0), title_text, font=title_font)
            title_height = title_box[3] - title_box[1]
            title_y = band_top + max(8, int(band_height * 0.10))
            draw.text(
                (pad + band_padding, title_y),
                title_text,
                fill=(24, 27, 31, 255),
                font=title_font,
            )

            subtitle_value = subtitle.strip()
            if subtitle_value:
                subtitle_text, subtitle_font = _fit_cover_wall_text(
                    draw,
                    subtitle_value,
                    font_file,
                    int(card_width * 0.58),
                    max(14, int(card_height * 0.022)),
                    max(12, int(card_height * 0.014)),
                )
                subtitle_y = title_y + title_height + max(
                    4, int(band_height * 0.025)
                )
                draw.text(
                    (pad + band_padding, subtitle_y),
                    subtitle_text,
                    fill=(54, 57, 61, 255),
                    font=subtitle_font,
                )

            count_text = f"{len(decoded):02d} 本"
            count_font = ImageFont.truetype(
                font_file,
                max(14, int(card_height * 0.035)),
            )
            count_box = draw.textbbox((0, 0), count_text, font=count_font)
            count_width = count_box[2] - count_box[0]
            count_height = count_box[3] - count_box[1]
            count_x = pad + card_width - band_padding - count_width
            count_y = band_top + (band_height - count_height) // 2 - count_box[1]
            draw.text(
                (count_x, count_y),
                count_text,
                fill=(24, 27, 31, 255),
                font=count_font,
            )

        return layer

    def paste_card(
        layer: Image.Image,
        center_x: int,
        center_y: int,
        angle: float,
    ) -> None:
        rotated = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
        canvas.paste(
            rotated,
            (center_x - rotated.width // 2, center_y - rotated.height // 2),
            rotated,
        )

    background_slots = (
        (0.12, 0.06, -9.0, 0.46),
        (0.88, 0.08, 7.0, 0.44),
        (-0.02, 0.42, 8.0, 0.43),
        (1.02, 0.45, -7.0, 0.46),
        (0.12, 0.93, -8.0, 0.44),
        (0.88, 0.94, 7.0, 0.42),
        (0.50, -0.05, 3.0, 0.38),
        (0.50, 1.04, -3.0, 0.40),
        (-0.05, 0.74, -10.0, 0.38),
        (1.05, 0.76, 9.0, 0.38),
        (0.22, 0.26, 4.0, 0.34),
    )
    for image, (center_x, center_y, angle, scale) in zip(
        decoded[1:], background_slots
    ):
        card_height = int(hero_height * scale)
        card_width = int(card_height * 0.72)
        paste_card(
            make_card(image, card_width, card_height),
            int(width * center_x),
            int(height * center_y),
            angle,
        )

    hero = make_card(decoded[0], hero_width, hero_height, with_obi=True)
    paste_card(
        hero,
        int(width * 0.51),
        int(height * 0.50),
        -4.0 if height >= width else -3.0,
    )

    output = canvas.convert("RGB")
    if monochrome:
        return ImageOps.autocontrast(ImageOps.grayscale(output)).convert("RGB")
    return output


class _CoverWallDialog(QtWidgets.QDialog):
    def __init__(self, entries: Sequence[_CoverWallEntry], parent=None):
        super().__init__(parent)
        self.entries = list(entries)
        self.setWindowTitle("生成封面墙")
        self.resize(720, 620)

        self.title_edit = QtWidgets.QLineEdit("我的书架")
        self.subtitle_edit = QtWidgets.QLineEdit()
        self.subtitle_edit.setPlaceholderText("可选，例如：最近阅读")
        self.layout_combo = QtWidgets.QComboBox()
        for layout_id, label in _COVER_WALL_LAYOUTS:
            self.layout_combo.addItem(label, layout_id)

        self.table = QtWidgets.QTableWidget(len(self.entries), 3)
        self.table.setHorizontalHeaderLabels(["选择", "文档", "更新时间"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setIconSize(QtCore.QSize(44, 56))
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)

        valid_seen = 0
        for row, entry in enumerate(self.entries):
            selector = QtWidgets.QTableWidgetItem()
            selector.setData(QtCore.Qt.UserRole, row)
            if entry.cover:
                selector.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable)
                selector.setCheckState(
                    QtCore.Qt.Checked if valid_seen < 9 else QtCore.Qt.Unchecked
                )
                valid_seen += 1
            else:
                selector.setFlags(QtCore.Qt.NoItemFlags)
                selector.setCheckState(QtCore.Qt.Unchecked)
            self.table.setItem(row, 0, selector)

            name_item = QtWidgets.QTableWidgetItem(entry.item.name)
            if entry.cover:
                image = QtGui.QImage.fromData(entry.cover)
                if not image.isNull():
                    name_item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image)))
            else:
                name_item.setText(f"{entry.item.name}（无可用封面）")
                name_item.setForeground(QtGui.QBrush(QtGui.QColor("#888888")))
            self.table.setItem(row, 1, name_item)
            updated = entry.item.updated.strftime("%Y-%m-%d %H:%M") if entry.item.updated else ""
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(updated))
            self.table.setRowHeight(row, 64)

        self.selection_label = QtWidgets.QLabel()
        self.table.itemChanged.connect(self._update_selection_label)
        self._update_selection_label()

        form = QtWidgets.QFormLayout()
        form.addRow("排版", self.layout_combo)
        form.addRow("标题", self.title_edit)
        form.addRow("副标题", self.subtitle_edit)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Cancel | QtWidgets.QDialogButtonBox.Ok
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("生成")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.table)
        layout.addWidget(self.selection_label)
        layout.addWidget(buttons)

    def selected_entries(self) -> List[_CoverWallEntry]:
        selected = []
        for row, entry in enumerate(self.entries):
            selector = self.table.item(row, 0)
            if entry.cover and selector and selector.checkState() == QtCore.Qt.Checked:
                selected.append(entry)
        return selected

    def _update_selection_label(self, _item=None) -> None:
        self.selection_label.setText(
            f"已选择 {len(self.selected_entries())} / {_MAX_COVER_WALL_ITEMS} 个封面"
        )

    def accept(self) -> None:
        count = len(self.selected_entries())
        if not 1 <= count <= _MAX_COVER_WALL_ITEMS:
            show_warning(self, _rmtool.APP_NAME, "请选择 1 到 12 个有可用封面的文档。")
            return
        super().accept()


class WallpaperTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__(parent)
        self.setObjectName("wallpaperWorkspace")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.ssh_client = ssh_client
        self.config = config
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.device_profile = "reMarkable Paper Pro"
        self.image_path: Optional[str] = None
        self._cached_source_image: Optional[Image.Image] = None
        self.current_resolution: Tuple[int, int] = _rmtool.DEVICE_PROFILES["reMarkable Paper Pro"]
        self._unavailable_wallpaper_paths: Set[str] = set()
        self._carousel_blank_active = False

        self.base_resolution = _rmtool.DEVICE_PROFILES["reMarkable Paper Pro"]
        self.orientation_combo = QtWidgets.QComboBox()
        self.orientation_combo.addItem("竖屏", "portrait")
        self.orientation_combo.addItem("横屏", "landscape")
        self.current_resolution = self._calculate_resolution(self.orientation_combo.currentData())

        self.preview_label = _rmtool.PreviewImageLabel("请选择图片以生成预览")
        self.preview_label.setMinimumSize(200, 260)
        self.preview_label.set_corner_radius(_rmtool.INNER_PANEL_RADIUS)
        self.frame_preview_checkbox = QtWidgets.QCheckBox("真机预览")
        self.frame_preview_checkbox.setChecked(True)

        self.info_label = QtWidgets.QLabel("未选择图片")
        self.resolution_label = QtWidgets.QLabel(self._resolution_text())
        self.choose_button = QtWidgets.QPushButton("选择本地图片")
        self.cover_wall_button = QtWidgets.QPushButton("生成封面墙")
        self.cover_wall_button.setEnabled(self.ssh_client.is_connected())
        self.upload_button = QtWidgets.QPushButton("上传为壁纸")
        self.upload_button.setProperty("btnRole", "primary")
        self.upload_button.setEnabled(False)
        self.rescan_button = QtWidgets.QPushButton("重新扫描")

        self.variant_group = QtWidgets.QButtonGroup(self)
        self.variant_previews: Dict[str, _rmtool.PreviewImageLabel] = {}
        self.variant_buttons: Dict[str, QtWidgets.QRadioButton] = {}

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("智能填充（留白）", "pad")
        self.mode_combo.addItem("裁剪铺满", "crop")
        self.mode_combo.addItem("直接拉伸", "stretch")

        self.offset_x_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.offset_x_slider.setRange(-100, 100)
        self.offset_x_slider.setValue(0)
        self.offset_y_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.offset_y_slider.setRange(-100, 100)
        self.offset_y_slider.setValue(0)
        self.offset_x_slider.setEnabled(False)
        self.offset_y_slider.setEnabled(False)

        # Processing settings share one form so labels and controls align.
        settings_form = QtWidgets.QFormLayout()
        settings_form.setContentsMargins(0, 0, 0, 0)
        settings_form.setSpacing(8)
        settings_form.addRow("壁纸方向", self.orientation_combo)
        settings_form.addRow("处理模式", self.mode_combo)
        settings_form.addRow("水平偏移", self.offset_x_slider)
        settings_form.addRow("垂直偏移", self.offset_y_slider)

        variants_layout = QtWidgets.QGridLayout()
        variants_layout.setContentsMargins(0, 0, 0, 0)
        variants_layout.setHorizontalSpacing(_rmtool.SUBSECTION_GAP)
        variants_layout.setVerticalSpacing(_rmtool.SUBSECTION_GAP)
        for index, (variant_key, display_name, remote_path) in enumerate(_rmtool.WALLPAPER_VARIANTS):
            preview = _rmtool.PreviewImageLabel("未连接")
            preview.setMinimumSize(100, 130)
            preview.set_corner_radius(_rmtool.INNER_PANEL_RADIUS)
            preview.setToolTip(remote_path)
            radio = QtWidgets.QRadioButton(display_name)
            radio.setProperty("variant_key", variant_key)
            radio.setProperty("remote_path", remote_path)
            radio.setToolTip(remote_path)
            self.variant_group.addButton(radio)
            self.variant_previews[variant_key] = preview
            self.variant_buttons[variant_key] = radio

            container = QtWidgets.QFrame()
            container.setObjectName("wallpaperVariantCard")
            container_layout = QtWidgets.QVBoxLayout(container)
            container_layout.setContentsMargins(8, 8, 8, 8)
            container_layout.setSpacing(8)
            container_layout.addWidget(preview)
            container_layout.addWidget(radio, alignment=QtCore.Qt.AlignHCenter)

            row = index // 2
            column = index % 2
            variants_layout.addWidget(container, row, column)

        self.variants_section = QtWidgets.QWidget()
        variants_section_layout = QtWidgets.QVBoxLayout(self.variants_section)
        variants_section_layout.setContentsMargins(0, 0, 0, 0)
        variants_section_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        self.variants_section_label = QtWidgets.QLabel("当前设备壁纸")
        self.variants_section_label.setObjectName("panelSectionLabel")
        variants_header = QtWidgets.QHBoxLayout()
        variants_header.setContentsMargins(0, 0, 0, 0)
        variants_header.addWidget(self.variants_section_label)
        variants_header.addStretch()
        variants_header.addWidget(self.rescan_button)
        variants_section_layout.addLayout(variants_header)
        variants_section_layout.addLayout(variants_layout)

        self.blank_carousel_checkbox = QtWidgets.QCheckBox("使用空白壁纸替换休眠轮播")
        self.blank_carousel_checkbox.setToolTip(
            "开启后把设备上的休眠轮播插图替换为空白图"
            "（原始插图会先备份到设备上的同名 .backup 文件）；\n"
            "关闭时从备份恢复原始插图。"
        )
        self.blank_carousel_checkbox.setEnabled(False)
        variants_section_layout.addWidget(self.blank_carousel_checkbox)

        self.target_label = QtWidgets.QLabel()

        # -- Source section: pick an image, then see what was picked --
        source_section_label = QtWidgets.QLabel("图片来源")
        source_section_label.setObjectName("panelSectionLabel")
        source_buttons = QtWidgets.QHBoxLayout()
        source_buttons.setContentsMargins(0, 0, 0, 0)
        source_buttons.setSpacing(8)
        source_buttons.addWidget(self.choose_button, 1)
        source_buttons.addWidget(self.cover_wall_button, 1)

        settings_section_label = QtWidgets.QLabel("处理设置")
        settings_section_label.setObjectName("panelSectionLabel")

        self.control_inner = QtWidgets.QWidget()
        self.control_inner.setObjectName("wallpaperControlInner")
        self.control_inner.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        control_layout = QtWidgets.QVBoxLayout(self.control_inner)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        control_layout.addWidget(self.variants_section)
        control_layout.addWidget(source_section_label)
        control_layout.addLayout(source_buttons)
        control_layout.addWidget(self.info_label)
        control_layout.addWidget(settings_section_label)
        control_layout.addLayout(settings_form)
        control_layout.addWidget(self.resolution_label)
        control_layout.addWidget(self.target_label)
        control_layout.addWidget(self.upload_button)

        self.control_scroll = QtWidgets.QScrollArea()
        self.control_scroll.setObjectName("wallpaperControlScroll")
        self.control_scroll.setWidgetResizable(True)
        self.control_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.control_scroll.setWidget(self.control_inner)
        self.control_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.control_scroll.viewport().setObjectName("wallpaperControlViewport")
        self.control_scroll.viewport().setAttribute(QtCore.Qt.WA_StyledBackground, True)

        self.control_panel = QtWidgets.QFrame()
        self.control_panel.setObjectName("wallpaperControlPanel")
        self.control_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        control_panel_layout = QtWidgets.QVBoxLayout(self.control_panel)
        control_panel_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        control_panel_layout.setSpacing(0)
        control_panel_layout.addWidget(self.control_scroll)

        self.preview_panel = QtWidgets.QFrame()
        self.preview_panel.setObjectName("wallpaperPreviewPanel")
        preview_layout = QtWidgets.QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(
            _rmtool.SUBSECTION_GAP,
            _rmtool.SUBSECTION_GAP,
            _rmtool.SUBSECTION_GAP,
            _rmtool.SUBSECTION_GAP,
        )
        preview_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        preview_layout.addWidget(
            self.frame_preview_checkbox,
            alignment=QtCore.Qt.AlignLeft,
        )
        preview_layout.addWidget(self.preview_label, stretch=1)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.main_splitter.addWidget(self.control_panel)
        self.main_splitter.addWidget(self.preview_panel)
        self.main_splitter.setStretchFactor(0, 5)
        self.main_splitter.setStretchFactor(1, 4)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(_rmtool.PANEL_GAP)

        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
        )
        layout.setSpacing(0)
        layout.addWidget(self.main_splitter)
        self.setLayout(layout)

        self.choose_button.clicked.connect(self._select_image)
        self.cover_wall_button.clicked.connect(self._open_cover_wall)
        self.rescan_button.clicked.connect(self._refresh_variant_previews)
        self.upload_button.clicked.connect(self._upload_wallpaper)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.orientation_combo.currentIndexChanged.connect(self._on_orientation_changed)
        self.frame_preview_checkbox.toggled.connect(self._render_preview)
        self.offset_x_slider.valueChanged.connect(self._render_preview)
        self.offset_y_slider.valueChanged.connect(self._render_preview)
        self.variant_group.buttonClicked.connect(self._on_variant_selected)
        self.blank_carousel_checkbox.toggled.connect(self._on_blank_carousel_toggled)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        QtCore.QTimer.singleShot(0, self._apply_initial_splitter_sizes)

        self._select_variant_by_path(self._configured_wallpaper_path())
        self._update_target_label()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self.ssh_client.is_connected():
            self._refresh_variant_previews()

    def _apply_initial_splitter_sizes(self):
        control_target = max(self.control_inner.sizeHint().width() + 24, 860)
        preview_target = max(self.preview_panel.sizeHint().width(), 660)
        self.main_splitter.setSizes([control_target, preview_target])

    def update_device(self, device: Dict):
        profile = device.get("type") if device else None
        self.device_profile = profile or "reMarkable Paper Pro"
        self.base_resolution = _rmtool.DEVICE_PROFILES.get(
            profile, _rmtool.DEVICE_PROFILES["reMarkable Paper Pro"]
        )
        self._update_resolution()
        self._select_variant_by_path(self._configured_wallpaper_path())
        self._refresh_variant_previews()
        self._render_preview()

    def _resolution_text(self) -> str:
        return (
            f"目标分辨率：{self.current_resolution[0]} × {self.current_resolution[1]}"
        )

    def _calculate_resolution(self, orientation: str) -> Tuple[int, int]:
        width, height = self.base_resolution
        if orientation == "portrait":
            return (min(width, height), max(width, height))
        return (max(width, height), min(width, height))

    def _update_resolution(self) -> None:
        orientation = self.orientation_combo.currentData()
        self.current_resolution = self._calculate_resolution(orientation)
        self.resolution_label.setText(self._resolution_text())
        self._update_target_label()

    def _on_connection_changed(self, connected: bool) -> None:
        self.cover_wall_button.setEnabled(connected)
        if not connected:
            self._carousel_blank_active = False
        self._sync_blank_carousel_checkbox()
        if connected:
            self._refresh_variant_previews()
        else:
            for preview in self.variant_previews.values():
                preview.clear_preview()
                preview.setText("未连接")

    def _sync_blank_carousel_checkbox(self) -> None:
        connected = self.ssh_client.is_connected()
        self.blank_carousel_checkbox.blockSignals(True)
        self.blank_carousel_checkbox.setChecked(connected and self._carousel_blank_active)
        self.blank_carousel_checkbox.setEnabled(connected)
        self.blank_carousel_checkbox.blockSignals(False)

    def _on_blank_carousel_toggled(self, checked: bool) -> None:
        if not self.ssh_client.is_connected():
            self._sync_blank_carousel_checkbox()
            return
        self.blank_carousel_checkbox.setEnabled(False)
        if checked:
            worker = _rmtool.Worker(self._blank_carousel_overlays)
            worker.signals.finished.connect(self._on_blank_carousel_done)
        else:
            worker = _rmtool.Worker(self._restore_carousel_overlays)
            worker.signals.finished.connect(self._on_restore_carousel_done)
        worker.signals.error.connect(self._on_carousel_option_error)
        self.thread_pool.start(worker)

    def _on_blank_carousel_done(self, count: int) -> None:
        if count:
            show_info(
                self,
                _rmtool.APP_NAME,
                "休眠轮播插图已替换为空白壁纸，原始插图已备份到设备。",
            )
        else:
            show_info(self, _rmtool.APP_NAME, "设备上没有找到休眠轮播插图。")
        self._refresh_variant_previews()

    def _on_restore_carousel_done(self, count: int) -> None:
        if count:
            show_info(self, _rmtool.APP_NAME, "已从备份恢复休眠轮播插图。")
            self._refresh_variant_previews()
        else:
            show_info(
                self,
                _rmtool.APP_NAME,
                "设备上没有找到原始插图的备份，无法恢复；原始插图只能随固件重置找回。",
            )
            self._sync_blank_carousel_checkbox()

    def _on_carousel_option_error(self, exc: Exception) -> None:
        logging.exception("Failed to update carousel overlays")
        show_error(self, _rmtool.APP_NAME, f"休眠轮播设置失败：{exc}")
        self._sync_blank_carousel_checkbox()

    def _blank_carousel_overlays(self) -> int:
        """Worker entry: back up originals (once), then blank every overlay."""
        with remount_rw(self.ssh_client):
            return self._blank_carousel_overlays_locked()

    def _blank_carousel_overlays_locked(self) -> int:
        """Blank carousel overlays; caller must hold a read-write mount."""
        try:
            entries = self.ssh_client.listdir_attr(_CAROUSEL_DIR)
        except Exception:
            return 0
        png_files = [
            entry.filename
            for entry in entries
            if entry.filename.lower().endswith(".png")
        ]
        if not png_files:
            return 0

        fd, transparent_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(transparent_path, format="PNG")
            for filename in png_files:
                remote = f"{_CAROUSEL_DIR}/{filename}"
                backup = remote + _CAROUSEL_BACKUP_SUFFIX
                if not self.ssh_client.file_exists(backup):
                    # Never back up an already-blanked placeholder as if it
                    # were the original artwork.
                    try:
                        with self.ssh_client.open_remote(remote, "rb") as remote_file:
                            current = remote_file.read()
                    except Exception:
                        current = b""
                    if current and not _is_transparent_placeholder(current):
                        self.ssh_client.exec_checked(f"cp {remote} {backup}")
                self.ssh_client.transfer_file(transparent_path, remote)
            logging.info("Blanked %d carousel overlay(s)", len(png_files))
            return len(png_files)
        finally:
            if os.path.exists(transparent_path):
                os.remove(transparent_path)

    def _restore_carousel_overlays(self) -> int:
        """Worker entry: restore backed-up overlays. Returns restored count."""
        with remount_rw(self.ssh_client):
            try:
                entries = self.ssh_client.listdir_attr(_CAROUSEL_DIR)
            except Exception:
                return 0
            backups = [
                entry.filename
                for entry in entries
                if entry.filename.lower().endswith(f".png{_CAROUSEL_BACKUP_SUFFIX}")
            ]
            for filename in backups:
                original = filename[: -len(_CAROUSEL_BACKUP_SUFFIX)]
                self.ssh_client.exec_checked(
                    f"mv {_CAROUSEL_DIR}/{filename} {_CAROUSEL_DIR}/{original}"
                )
            logging.info("Restored %d carousel overlay(s)", len(backups))
            return len(backups)

    def _refresh_variant_previews(self) -> None:
        if not self.ssh_client.is_connected():
            self._unavailable_wallpaper_paths.clear()
            for preview in self.variant_previews.values():
                preview.clear_preview()
                preview.setText("未连接")
            for button in self.variant_buttons.values():
                button.setEnabled(True)
            self._sync_blank_carousel_checkbox()
            self._update_target_label()
            self._update_upload_button_state()
            return
        for preview in self.variant_previews.values():
            preview.setText("加载中…")
        worker = _rmtool.Worker(self._download_all_variant_previews)
        worker.signals.finished.connect(self._apply_variant_previews)
        worker.signals.error.connect(self._on_variant_preview_error)
        self.thread_pool.start(worker)

    def _scan_wallpaper_resource_paths(self) -> _WallpaperResourceScan:
        available_paths: Set[str] = set()
        complete = True
        carousel_backed_up = False

        def add_pngs_from_dir(remote_dir: str, *, required: bool = False) -> None:
            nonlocal complete, carousel_backed_up
            try:
                entries = self.ssh_client.listdir_attr(remote_dir)
            except Exception:
                if required:
                    complete = False
                logging.info("Wallpaper resource directory unavailable: %s", remote_dir)
                return
            for entry in entries:
                filename = getattr(entry, "filename", "")
                lowered = filename.lower()
                if remote_dir == _CAROUSEL_DIR and lowered.endswith(
                    f".png{_CAROUSEL_BACKUP_SUFFIX}"
                ):
                    carousel_backed_up = True
                if lowered.endswith(".png"):
                    available_paths.add(posixpath.normpath(posixpath.join(remote_dir, filename)))

        add_pngs_from_dir("/usr/share/remarkable", required=True)
        add_pngs_from_dir(_CAROUSEL_DIR)

        try:
            with self.ssh_client.open_remote(
                "/home/root/.config/remarkable/xochitl.conf", "r"
            ) as config_file:
                config_data = config_file.read()
            if isinstance(config_data, bytes):
                config_text = config_data.decode("utf-8", errors="ignore")
            else:
                config_text = str(config_data)
            for line in config_text.splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip() == "SleepScreenPath" and value.strip():
                    available_paths.add(posixpath.normpath(value.strip()))
        except Exception:
            pass

        return _WallpaperResourceScan(
            available_paths=available_paths,
            complete=complete,
            carousel_backed_up=carousel_backed_up,
        )

    def _download_all_variant_previews(self) -> Dict[str, _WallpaperPreviewResult]:
        results: Dict[str, _WallpaperPreviewResult] = {}
        scan = self._scan_wallpaper_resource_paths()
        self._carousel_blank_active = scan.carousel_backed_up
        for variant_key, _display_name, remote_path in _rmtool.WALLPAPER_VARIANTS:
            try:
                normalized_path = posixpath.normpath(remote_path)
                if scan.complete:
                    if normalized_path not in scan.available_paths:
                        results[variant_key] = _WallpaperPreviewResult(missing=True)
                        continue
                elif hasattr(self.ssh_client, "file_exists") and not self.ssh_client.file_exists(remote_path):
                    results[variant_key] = _WallpaperPreviewResult(missing=True)
                    continue
                with self.ssh_client.open_remote(remote_path, "rb") as remote_file:
                    results[variant_key] = _WallpaperPreviewResult(data=remote_file.read())
            except Exception:
                logging.exception("Unable to load wallpaper preview: %s", remote_path)
                results[variant_key] = _WallpaperPreviewResult()
        return results

    def _apply_variant_previews(self, results: Dict[str, _WallpaperPreviewResult]) -> None:
        for variant_key, result in results.items():
            preview = self.variant_previews.get(variant_key)
            button = self.variant_buttons.get(variant_key)
            if not preview:
                continue
            remote_path = button.property("remote_path") if button else ""
            normalized_path = posixpath.normpath(remote_path) if remote_path else ""
            if result.missing:
                if normalized_path:
                    self._unavailable_wallpaper_paths.add(normalized_path)
                preview.clear_preview()
                preview.setText("当前设备不存在")
                tooltip = (
                    f"{remote_path}\n当前连接的设备没有这个文件；旧固件设备可能可用。"
                    if remote_path
                    else "当前连接的设备没有这个文件；旧固件设备可能可用。"
                )
                preview.setToolTip(tooltip)
                if button:
                    button.setToolTip(tooltip)
                    button.setEnabled(False)
                continue

            if normalized_path:
                self._unavailable_wallpaper_paths.discard(normalized_path)
            if button:
                button.setToolTip(remote_path)
                button.setEnabled(True)
            preview.setToolTip(remote_path)
            data = result.data
            if not data:
                preview.clear_preview()
                preview.setText("加载失败")
                continue
            if _is_transparent_placeholder(data):
                preview.clear_preview()
                preview.setText("已被透明覆盖")
                placeholder_tooltip = (
                    f"{remote_path}\n上传休眠壁纸时已将该轮播覆盖图清空为透明，"
                    "自定义休眠壁纸才会显示。"
                )
                preview.setToolTip(placeholder_tooltip)
                if button:
                    button.setToolTip(placeholder_tooltip)
                continue
            pixmap = QtGui.QPixmap()
            if pixmap.loadFromData(data):
                preview.setPixmap(pixmap)
            else:
                try:
                    with Image.open(BytesIO(data)) as image:
                        buffer = BytesIO()
                        image.convert("RGB").save(buffer, format="PNG")
                    if pixmap.loadFromData(buffer.getvalue(), "PNG"):
                        preview.setPixmap(pixmap)
                    else:
                        preview.clear_preview()
                        preview.setText("无预览")
                except Exception:
                    preview.clear_preview()
                    preview.setText("无预览")
        # The checkbox state follows the device: originals backed up, or the
        # carousel files themselves are transparent placeholders (legacy
        # devices blanked before backups existed).
        carousel_results = [
            results[key]
            for key, _name, path in _rmtool.WALLPAPER_VARIANTS
            if path.startswith(_CAROUSEL_DIR + "/") and key in results
        ]
        if any(r.data and not _is_transparent_placeholder(r.data) for r in carousel_results):
            self._carousel_blank_active = False
        elif any(r.data and _is_transparent_placeholder(r.data) for r in carousel_results):
            self._carousel_blank_active = True
        self._sync_blank_carousel_checkbox()
        self._update_target_label()
        self._update_upload_button_state()

    def _on_variant_preview_error(self, _exc: Exception) -> None:
        for preview in self.variant_previews.values():
            preview.clear_preview()
            preview.setText("加载失败")
        self._update_upload_button_state()

    def _on_variant_selected(self, button: QtWidgets.QAbstractButton) -> None:
        remote_path = button.property("remote_path")
        if not remote_path:
            return
        self.config.setdefault("paths", {})["wallpaper"] = self._normalise_wallpaper_path(remote_path)
        self._update_target_label()

    def _normalise_wallpaper_path(self, remote_path: str) -> str:
        normalized = posixpath.normpath(remote_path)
        return _LEGACY_WALLPAPER_PATHS.get(normalized, normalized)

    def _configured_wallpaper_path(self) -> str:
        paths = self.config.setdefault("paths", {})
        remote_path = paths.get("wallpaper", "/usr/share/remarkable/suspended.png")
        normalized = self._normalise_wallpaper_path(remote_path)
        if normalized != remote_path:
            paths["wallpaper"] = normalized
        return normalized

    def _select_variant_by_path(self, remote_path: str) -> None:
        normalized = self._normalise_wallpaper_path(remote_path)
        matched = False
        for variant_key, _display_name, candidate_path in _rmtool.WALLPAPER_VARIANTS:
            if posixpath.normpath(candidate_path) == normalized:
                button = self.variant_buttons.get(variant_key)
                if button:
                    button.setChecked(True)
                matched = True
                break

        if not matched:
            self.variant_group.setExclusive(False)
            for button in self.variant_buttons.values():
                button.setChecked(False)
            self.variant_group.setExclusive(True)

    def _variant_label_for_path(self, remote_path: str) -> Optional[str]:
        normalized = self._normalise_wallpaper_path(remote_path)
        for _variant_key, display_name, candidate_path in _rmtool.WALLPAPER_VARIANTS:
            if posixpath.normpath(candidate_path) == normalized:
                return display_name
        return None

    def _wallpaper_path_available(self, remote_path: str) -> bool:
        normalized = self._normalise_wallpaper_path(remote_path)
        return normalized not in self._unavailable_wallpaper_paths

    def _update_upload_button_state(self) -> None:
        self.upload_button.setEnabled(
            self._cached_source_image is not None
            and self._wallpaper_path_available(self._configured_wallpaper_path())
        )

    def _update_target_label(self) -> None:
        remote_path = self._configured_wallpaper_path()
        variant_label = self._variant_label_for_path(remote_path)
        suffix = "（当前设备不存在）" if not self._wallpaper_path_available(remote_path) else ""
        if variant_label:
            self.target_label.setText(f"目标壁纸：{variant_label} ({remote_path}){suffix}")
        else:
            self.target_label.setText(f"目标壁纸：{remote_path}{suffix}")

    @require_connection
    def _open_cover_wall(self):
        self.cover_wall_button.setEnabled(False)
        self._cover_wall_progress = QtWidgets.QProgressDialog(
            "正在读取书库封面…", "", 0, 0, self
        )
        self._cover_wall_progress.setWindowTitle(_rmtool.APP_NAME)
        self._cover_wall_progress.setWindowModality(QtCore.Qt.ApplicationModal)
        self._cover_wall_progress.setCancelButton(None)
        self._cover_wall_progress.setMinimumDuration(0)
        self._cover_wall_progress.show()

        worker = _rmtool.Worker(self._load_cover_wall_entries)
        worker.signals.finished.connect(self._on_cover_wall_entries_loaded)
        worker.signals.error.connect(self._on_cover_wall_entries_error)
        self.thread_pool.start(worker)

    def _load_cover_wall_entries(self) -> List[_CoverWallEntry]:
        with self.ssh_client.sftp_session() as sftp:
            entries = []
            for item in _rmtool.load_document_items(sftp):
                cover = _usable_cover_data(_rmtool.read_document_cover(sftp, item))
                entries.append(_CoverWallEntry(item, cover))
            return entries

    def _on_cover_wall_entries_loaded(self, entries: List[_CoverWallEntry]) -> None:
        self._close_cover_wall_progress()
        if not any(entry.cover for entry in entries):
            show_warning(self, _rmtool.APP_NAME, "设备上没有可用于生成封面墙的文档缩略图。")
            return

        dialog = _CoverWallDialog(entries, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            self._apply_cover_wall(
                dialog.selected_entries(),
                dialog.title_edit.text(),
                dialog.subtitle_edit.text(),
                dialog.layout_combo.currentData(),
            )
        except Exception as exc:
            logging.exception("Unable to compose cover wall")
            show_error(self, _rmtool.APP_NAME, f"生成封面墙失败：{exc}")

    def _on_cover_wall_entries_error(self, exc: Exception) -> None:
        self._close_cover_wall_progress()
        logging.error("Unable to load document covers: %s", exc)
        show_error(self, _rmtool.APP_NAME, f"读取书库封面失败：{exc}")

    def _close_cover_wall_progress(self) -> None:
        progress = getattr(self, "_cover_wall_progress", None)
        if progress:
            progress.close()
            progress.deleteLater()
            self._cover_wall_progress = None
        self.cover_wall_button.setEnabled(self.ssh_client.is_connected())

    def _apply_cover_wall(
        self,
        entries: Sequence[_CoverWallEntry],
        title: str,
        subtitle: str,
        layout: str = "hero_obi",
    ) -> None:
        covers = [entry.cover for entry in entries if entry.cover]
        generated = compose_cover_wallpaper(
            covers,
            self.current_resolution,
            title,
            subtitle,
            layout=layout,
            monochrome=self.device_profile in _MONOCHROME_DEVICE_PROFILES,
        )
        pad_index = self.mode_combo.findData("pad")
        if pad_index >= 0:
            self.mode_combo.setCurrentIndex(pad_index)
        self.offset_x_slider.setValue(0)
        self.offset_y_slider.setValue(0)
        self.image_path = None
        self._set_source_image(generated, f"已生成封面墙：{len(covers)} 本文档")

    def _set_source_image(self, image: Image.Image, description: str) -> None:
        previous = self._cached_source_image
        self._cached_source_image = image
        if previous is not None and previous is not image:
            try:
                previous.close()
            except Exception:
                pass
        self.info_label.setText(description)
        self._update_upload_button_state()
        self._render_preview()

    def _select_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp)"
        )
        if not path:
            return
        with Image.open(path) as image:
            source = image.convert("RGB")
        self.image_path = path
        self._set_source_image(source, f"选择的图片：{path}")

    def _on_mode_changed(self):
        crop_mode = self.mode_combo.currentData() == "crop"
        self.offset_x_slider.setEnabled(crop_mode)
        self.offset_y_slider.setEnabled(crop_mode)
        self._render_preview()

    def _on_orientation_changed(self):
        self._update_resolution()
        self._render_preview()

    def _render_preview(self):
        if self._cached_source_image is None:
            self.preview_label.clear_preview()
            self.preview_label.setText("请选择图片以生成预览")
            return
        try:
            processed = self._process_image()
        except Exception as exc:
            logging.exception("Unable to render wallpaper preview")
            self.preview_label.clear_preview()
            self.preview_label.setText(f"预览失败：{exc}")
            return
        if processed.mode != "RGB":
            processed = processed.convert("RGB")
        preview = processed
        if self.frame_preview_checkbox.isChecked():
            frame_profile = _DEVICE_FRAME_PROFILES.get(self.device_profile)
            if frame_profile:
                frame_filename, screen_rect = frame_profile
                try:
                    with Image.open(
                        _rmtool.resource_path(
                            "assets",
                            "device_frames",
                            frame_filename,
                        )
                    ) as frame:
                        preview = compose_device_frame_preview(
                            processed,
                            frame,
                            screen_rect,
                            self.orientation_combo.currentData(),
                        )
                except (OSError, ValueError):
                    logging.warning(
                        "Unable to load device frame preview for %s",
                        self.device_profile,
                        exc_info=True,
                    )
        buffer = BytesIO()
        preview.save(buffer, format="PNG")
        pixmap = QtGui.QPixmap()
        if not pixmap.loadFromData(buffer.getvalue(), "PNG"):
            raise RuntimeError("无法加载图片预览数据")
        self.preview_label.setPixmap(pixmap)

    def _process_image(self) -> Image.Image:
        if self._cached_source_image is None:
            raise RuntimeError("未选择图片")
        img = self._cached_source_image
        target_w, target_h = self.current_resolution
        mode = self.mode_combo.currentData()
        if mode == "pad":
            image = img.copy()
            image.thumbnail((target_w, target_h), Image.LANCZOS)
            new_img = Image.new("RGB", (target_w, target_h), color="white")
            offset = (
                (target_w - image.size[0]) // 2,
                (target_h - image.size[1]) // 2,
            )
            new_img.paste(image, offset)
            return new_img
        if mode == "stretch":
            return img.resize((target_w, target_h), Image.LANCZOS)

        scale = max(target_w / img.width, target_h / img.height)
        new_size = (int(img.width * scale), int(img.height * scale))
        resized = img.resize(new_size, Image.LANCZOS)
        range_x = max(new_size[0] - target_w, 0)
        range_y = max(new_size[1] - target_h, 0)
        norm_x = (self.offset_x_slider.value() + 100) / 200
        norm_y = (self.offset_y_slider.value() + 100) / 200
        left = int(range_x * norm_x)
        top = int(range_y * norm_y)
        box = (
            left,
            top,
            left + target_w,
            top + target_h,
        )
        return resized.crop(box)

    @require_connection
    def _upload_wallpaper(self):
        if self._cached_source_image is None:
            return
        wallpaper_path = self._configured_wallpaper_path()
        if not self._wallpaper_path_available(wallpaper_path):
            show_warning(
                self,
                _rmtool.APP_NAME,
                "当前连接的设备没有这个壁纸文件，无法上传到该目标。请改选一个当前设备存在的壁纸资源。",
            )
            return
        try:
            processed_image = self._process_image()
        except Exception as exc:
            logging.exception("Wallpaper processing failed")
            show_error(self, _rmtool.APP_NAME, f"图片处理失败：{exc}")
            return
        fd, temp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        processed_image.save(temp_path, format="PNG")

        self.upload_button.setEnabled(False)
        self._wallpaper_progress = QtWidgets.QProgressDialog(
            "正在上传壁纸…", "", 0, 0, self
        )
        self._wallpaper_progress.setWindowTitle(_rmtool.APP_NAME)
        self._wallpaper_progress.setWindowModality(QtCore.Qt.ApplicationModal)
        self._wallpaper_progress.setCancelButton(None)
        self._wallpaper_progress.setMinimumDuration(0)
        self._wallpaper_progress.show()

        worker = _rmtool.Worker(self._do_upload_wallpaper, temp_path, wallpaper_path)

        def on_finished(_result):
            self._close_wallpaper_progress(temp_path)
            if self.ssh_client.is_connected():
                self._refresh_variant_previews()
            self._render_preview()
            show_info(self, _rmtool.APP_NAME, "壁纸上传完成。")

        def on_error(exc: Exception):
            self._close_wallpaper_progress(temp_path)
            logging.exception("Wallpaper upload failed")
            show_error(self, _rmtool.APP_NAME, f"上传壁纸失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _do_upload_wallpaper(self, temp_path: str, wallpaper_path: str):
        with remount_rw(self.ssh_client):
            self.ssh_client.exec_checked(f"cp {wallpaper_path} {wallpaper_path}.backup")
            self.ssh_client.transfer_file(temp_path, wallpaper_path)

            if wallpaper_path.endswith("suspended.png"):
                self._blank_carousel_overlays_locked()

    def _close_wallpaper_progress(self, temp_path: str):
        if hasattr(self, "_wallpaper_progress") and self._wallpaper_progress:
            self._wallpaper_progress.close()
            self._wallpaper_progress.deleteLater()
            self._wallpaper_progress = None
        self._update_upload_button_state()
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
