"""WallpaperTab extracted from rmtool.py."""

import logging
import math
import os
import posixpath
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
_MONOCHROME_DEVICE_PROFILES = {
    "reMarkable Paper Pure",
    "reMarkable 1",
    "reMarkable 2",
}
_MAX_COVER_WALL_ITEMS = 12


@dataclass(frozen=True)
class _WallpaperPreviewResult:
    data: Optional[bytes] = None
    missing: bool = False


@dataclass
class _WallpaperResourceScan:
    available_paths: Set[str]
    complete: bool


@dataclass(frozen=True)
class _CoverWallEntry:
    item: _rmtool.DocumentItem
    cover: Optional[bytes]


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


def compose_cover_wallpaper(
    covers: Sequence[bytes],
    size: Tuple[int, int],
    title: str = "我的书架",
    subtitle: str = "",
    *,
    monochrome: bool = False,
    font_path: Optional[str] = None,
) -> Image.Image:
    """Compose up to twelve document covers into one device-sized poster."""
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("壁纸尺寸无效")
    if not 1 <= len(covers) <= _MAX_COVER_WALL_ITEMS:
        raise ValueError("请选择 1 到 12 个封面")

    decoded: List[Image.Image] = []
    for cover in covers:
        try:
            with Image.open(BytesIO(cover)) as image:
                decoded.append(image.convert("RGB"))
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
    margin = max(24, int(shortest * 0.045))
    gap = max(12, int(shortest * 0.018))
    header_height = max(int(height * 0.16), margin * 3)
    canvas = Image.new("RGB", size, (245, 246, 247))
    draw = ImageDraw.Draw(canvas)

    title_text, title_font = _fit_cover_wall_text(
        draw,
        title or "我的书架",
        font_file,
        width - margin * 2,
        max(28, int(shortest * 0.055)),
        max(18, int(shortest * 0.025)),
    )
    title_box = draw.textbbox((0, 0), title_text, font=title_font)
    title_height = title_box[3] - title_box[1]
    title_y = margin
    draw.text((margin, title_y), title_text, fill=(24, 27, 31), font=title_font)

    if subtitle.strip():
        subtitle_text, subtitle_font = _fit_cover_wall_text(
            draw,
            subtitle,
            font_file,
            width - margin * 2,
            max(18, int(shortest * 0.026)),
            max(14, int(shortest * 0.018)),
        )
        subtitle_y = title_y + title_height + max(8, gap // 2)
        draw.text((margin, subtitle_y), subtitle_text, fill=(91, 96, 104), font=subtitle_font)

    rule_y = header_height - max(8, gap // 2)
    rule_width = max(48, width // 8)
    draw.rectangle(
        (margin, rule_y, margin + rule_width, rule_y + max(3, shortest // 300)),
        fill=(40, 79, 122),
    )
    draw.line(
        (margin + rule_width + gap, rule_y + 1, width - margin, rule_y + 1),
        fill=(205, 209, 214),
        width=1,
    )

    count = len(decoded)
    if width >= height:
        columns = min(5, max(1, math.ceil(math.sqrt(count * 1.5))))
    else:
        columns = min(3, max(1, math.ceil(math.sqrt(count))))
    rows = math.ceil(count / columns)
    grid_top = header_height + gap
    grid_width = width - margin * 2
    grid_height = height - grid_top - margin
    cell_width = (grid_width - gap * (columns - 1)) / columns
    cell_height = (grid_height - gap * (rows - 1)) / rows
    cover_ratio = 0.72
    cover_width = int(min(cell_width, cell_height * cover_ratio))
    cover_height = int(cover_width / cover_ratio)
    border = max(2, shortest // 450)
    shadow = max(4, shortest // 220)

    for index, image in enumerate(decoded):
        row, column = divmod(index, columns)
        row_count = min(columns, count - row * columns)
        row_width = row_count * cover_width + (row_count - 1) * gap
        row_left = (width - row_width) // 2
        x = row_left + column * (cover_width + gap)
        y = int(grid_top + row * (cover_height + gap))
        fitted = ImageOps.fit(
            image,
            (cover_width, cover_height),
            method=Image.LANCZOS,
            centering=(0.5, 0.5),
        )
        draw.rounded_rectangle(
            (x + shadow, y + shadow, x + cover_width + shadow, y + cover_height + shadow),
            radius=max(3, shortest // 250),
            fill=(205, 208, 212),
        )
        canvas.paste(fitted, (x, y))
        draw.rectangle(
            (x, y, x + cover_width - 1, y + cover_height - 1),
            outline=(255, 255, 255),
            width=border,
        )

    if monochrome:
        return ImageOps.autocontrast(ImageOps.grayscale(canvas)).convert("RGB")
    return canvas


class _CoverWallDialog(QtWidgets.QDialog):
    def __init__(self, entries: Sequence[_CoverWallEntry], parent=None):
        super().__init__(parent)
        self.entries = list(entries)
        self.setWindowTitle("生成封面墙")
        self.resize(720, 620)

        self.title_edit = QtWidgets.QLineEdit("我的书架")
        self.subtitle_edit = QtWidgets.QLineEdit()
        self.subtitle_edit.setPlaceholderText("可选，例如：最近阅读")

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

        self.base_resolution = _rmtool.DEVICE_PROFILES["reMarkable Paper Pro"]
        self.orientation_combo = QtWidgets.QComboBox()
        self.orientation_combo.addItem("竖屏", "portrait")
        self.orientation_combo.addItem("横屏", "landscape")
        self.current_resolution = self._calculate_resolution(self.orientation_combo.currentData())

        self.preview_label = _rmtool.PreviewImageLabel("请选择图片以生成预览")
        self.preview_label.setMinimumSize(200, 260)
        self.preview_label.set_corner_radius(_rmtool.INNER_PANEL_RADIUS)

        self.info_label = QtWidgets.QLabel("未选择图片")
        self.resolution_label = QtWidgets.QLabel(self._resolution_text())
        self.choose_button = QtWidgets.QPushButton("选择本地图片")
        self.choose_button.setProperty("cssClass", "secondary")
        self.cover_wall_button = QtWidgets.QPushButton("生成封面墙")
        self.cover_wall_button.setProperty("cssClass", "secondary")
        self.cover_wall_button.setEnabled(self.ssh_client.is_connected())
        self.upload_button = QtWidgets.QPushButton("上传为壁纸")
        self.upload_button.setEnabled(False)
        self.rescan_button = QtWidgets.QPushButton("重新扫描")
        self.rescan_button.setProperty("cssClass", "secondary")

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

        offset_layout = QtWidgets.QFormLayout()
        offset_layout.addRow("水平偏移", self.offset_x_slider)
        offset_layout.addRow("垂直偏移", self.offset_y_slider)

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

            container = QtWidgets.QWidget()
            container_layout = QtWidgets.QVBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
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

        orientation_row = QtWidgets.QHBoxLayout()
        orientation_row.addWidget(QtWidgets.QLabel("壁纸方向"))
        orientation_row.addWidget(self.orientation_combo)
        orientation_row.addStretch()

        self.target_label = QtWidgets.QLabel()

        self.control_inner = QtWidgets.QWidget()
        self.control_inner.setObjectName("wallpaperControlInner")
        self.control_inner.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        control_layout = QtWidgets.QVBoxLayout(self.control_inner)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        control_layout.addWidget(self.variants_section)
        control_layout.addLayout(orientation_row)
        control_layout.addWidget(self.resolution_label)
        control_layout.addWidget(self.target_label)
        control_layout.addWidget(self.info_label)
        control_layout.addWidget(QtWidgets.QLabel("处理模式"))
        control_layout.addWidget(self.mode_combo)
        control_layout.addLayout(offset_layout)
        source_buttons = QtWidgets.QHBoxLayout()
        source_buttons.setContentsMargins(0, 0, 0, 0)
        source_buttons.addWidget(self.choose_button)
        source_buttons.addWidget(self.cover_wall_button)
        control_layout.addLayout(source_buttons)
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
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        preview_layout.addWidget(self.preview_label, alignment=QtCore.Qt.AlignCenter)
        preview_layout.addStretch()

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
        self.offset_x_slider.valueChanged.connect(self._render_preview)
        self.offset_y_slider.valueChanged.connect(self._render_preview)
        self.variant_group.buttonClicked.connect(self._on_variant_selected)
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
        if connected:
            self._refresh_variant_previews()
        else:
            for preview in self.variant_previews.values():
                preview.clear_preview()
                preview.setText("未连接")

    def _refresh_variant_previews(self) -> None:
        if not self.ssh_client.is_connected():
            self._unavailable_wallpaper_paths.clear()
            for preview in self.variant_previews.values():
                preview.clear_preview()
                preview.setText("未连接")
            for button in self.variant_buttons.values():
                button.setEnabled(True)
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

        def add_pngs_from_dir(remote_dir: str, *, required: bool = False) -> None:
            nonlocal complete
            try:
                entries = self.ssh_client.listdir_attr(remote_dir)
            except Exception:
                if required:
                    complete = False
                logging.info("Wallpaper resource directory unavailable: %s", remote_dir)
                return
            for entry in entries:
                filename = getattr(entry, "filename", "")
                if filename.lower().endswith(".png"):
                    available_paths.add(posixpath.normpath(posixpath.join(remote_dir, filename)))

        add_pngs_from_dir("/usr/share/remarkable", required=True)
        add_pngs_from_dir("/usr/share/remarkable/carousel")

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

        return _WallpaperResourceScan(available_paths=available_paths, complete=complete)

    def _download_all_variant_previews(self) -> Dict[str, _WallpaperPreviewResult]:
        results: Dict[str, _WallpaperPreviewResult] = {}
        scan = self._scan_wallpaper_resource_paths()
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
    ) -> None:
        covers = [entry.cover for entry in entries if entry.cover]
        generated = compose_cover_wallpaper(
            covers,
            self.current_resolution,
            title,
            subtitle,
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
        buffer = BytesIO()
        processed.save(buffer, format="PNG")
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
                self._clear_carousel_overlays()

    def _clear_carousel_overlays(self):
        carousel_dir = "/usr/share/remarkable/carousel"
        try:
            entries = self.ssh_client.listdir_attr(carousel_dir)
        except Exception:
            return
        png_files = [
            e.filename for e in entries
            if e.filename.lower().endswith(".png")
        ]
        if not png_files:
            return

        transparent_path = None
        try:
            fd, transparent_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            img.save(transparent_path, format="PNG")

            for filename in png_files:
                remote = f"{carousel_dir}/{filename}"
                self.ssh_client.transfer_file(transparent_path, remote)
            logging.info("Cleared %d carousel overlay(s)", len(png_files))
        finally:
            if transparent_path and os.path.exists(transparent_path):
                os.remove(transparent_path)

    def _close_wallpaper_progress(self, temp_path: str):
        if hasattr(self, "_wallpaper_progress") and self._wallpaper_progress:
            self._wallpaper_progress.close()
            self._wallpaper_progress.deleteLater()
            self._wallpaper_progress = None
        self._update_upload_button_state()
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
