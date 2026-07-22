import json
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import uuid

# When run as a script (__main__), submodules that `import rmtool` would
# trigger a second import of this file.  Register early so they get the
# already-loading module object instead.
sys.modules.setdefault("rmtool", sys.modules[__name__])
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import paramiko
from PIL import Image
from PyQt5 import QtCore, QtGui, QtSvg, QtWidgets, sip


APP_NAME = "reMarkable 管理工具"
CONFIG_FILE = "devices.json"
GITHUB_REPO_URL = "https://github.com/pretenderlu/rmtool"
GITHUB_MARK_PATH = "M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"
DEFAULT_FONT_NAME = "zwzt.ttf"
DEFAULT_FONT_DIR = "/home/root/.local/share/fonts/"
DOCUMENT_ROOT = "/home/root/.local/share/remarkable/xochitl"
KNOWN_HOSTS_FILE = "known_hosts"
TAB_PAGE_MARGIN = 16
PANEL_GAP = 16
PANEL_PADDING = 16
PANEL_RADIUS = 16
INNER_PANEL_RADIUS = 12
SUBSECTION_GAP = 12

DEVICE_PROFILES = {
    "reMarkable Paper Pro": (2160, 1620),
    "reMarkable Paper Pro Move": (1696, 954),
    "reMarkable Paper Pure": (1404, 1872),
    "reMarkable 1": (1404, 1872),
    "reMarkable 2": (1404, 1872),
}

DEVICE_PROFILE_LABELS = {
    "reMarkable Paper Pro": "Paper Pro",
    "reMarkable Paper Pro Move": "Paper Pro Move",
    "reMarkable Paper Pure": "Paper Pure",
    "reMarkable 1": "reMarkable 1",
    "reMarkable 2": "reMarkable 2",
}

WALLPAPER_VARIANTS = [
    ("starting", "启动壁纸", "/usr/share/remarkable/starting.png"),
    ("suspended", "休眠壁纸", "/usr/share/remarkable/suspended.png"),
    ("sleeping", "旧版休眠壁纸", "/usr/share/remarkable/sleeping.png"),
    ("sleep_carousel_1", "休眠轮播 1", "/usr/share/remarkable/carousel/sleep_Illustration_01.png"),
    ("sleep_carousel_2", "休眠轮播 2", "/usr/share/remarkable/carousel/sleep_Illustration_02.png"),
    ("sleep_carousel_3", "休眠轮播 3", "/usr/share/remarkable/carousel/sleep_Illustration_03.png"),
    ("poweroff", "关机壁纸", "/usr/share/remarkable/poweroff.png"),
]

FONT_PREVIEW_TEXT = "字体预览\nAaBbCc 1234567890\n你好，reMarkable"


def app_state_dir() -> Path:
    frozen = getattr(sys, "frozen", False)
    if frozen and sys.platform == "darwin":
        # A .app launched from Finder may sit on a read-only mount (Gatekeeper
        # app translocation) or in a folder the user cannot write to, so keep
        # state in the per-user Application Support directory instead of
        # beside the bundle.
        path = Path.home() / "Library" / "Application Support" / "rmtool"
    else:
        anchor = Path(sys.executable if frozen else __file__).resolve()
        path = anchor.parent / ".rmtool"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Logging – use rotating handler to prevent unbounded log growth
# ---------------------------------------------------------------------------
# Logging must never abort startup: if the state directory is not writable
# (read-only mount, translocated .app), fall back to stderr logging.
try:
    _log_handler = logging.handlers.RotatingFileHandler(
        str(app_state_dir() / "remarkable_tool.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
except OSError:
    _log_handler = logging.StreamHandler()
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
)
_root_logger = logging.getLogger()
_root_logger.addHandler(_log_handler)
_root_logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def resource_path(*parts: str) -> Path:
    """Return absolute path for bundled resources, relative to this file."""

    return Path(__file__).resolve().parent.joinpath(*parts)


def _generate_arrow_icons() -> Dict[str, str]:
    """Generate small arrow PNGs for QComboBox and return file paths.

    Qt QSS ``image: url(...)`` is the only reliable way to draw custom arrows
    in combo boxes -- the CSS border-trick renders inconsistently.
    """
    from PIL import ImageDraw as _ImageDraw

    icons_dir = Path(tempfile.gettempdir()) / "rmtool_icons"
    icons_dir.mkdir(exist_ok=True)
    paths: Dict[str, str] = {}
    for name, color, direction in [
        ("arrow_dark", "#C0C8E0", "down"),
        ("arrow_dark_up", "#C0C8E0", "up"),
        ("arrow_light", "#6B7080", "down"),
        ("arrow_light_up", "#6B7080", "up"),
    ]:
        path = icons_dir / f"{name}.png"
        if not path.exists():
            img = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
            draw = _ImageDraw.Draw(img)
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            if direction == "down":
                draw.polygon([(4, 7), (20, 7), (12, 18)], fill=(r, g, b, 255))
            else:
                draw.polygon([(4, 18), (20, 18), (12, 7)], fill=(r, g, b, 255))
            img.save(str(path), "PNG")
        paths[name] = str(path).replace("\\", "/")
    return paths


# Inline SVG sources for the sidebar footer icons. ``{color}`` is substituted
# per state so the same shapes serve normal and hover variants. sun/moon/log
# share a 24px grid with stroke width 2 for a consistent visual weight; the
# GitHub mark keeps its official solid silhouette.
_SIDEBAR_ICON_SVGS = {
    "sun": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{color}' stroke-width='2' stroke-linecap='round'>"
        "<circle cx='12' cy='12' r='5'/>"
        "<path d='M12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2"
        "M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4'/></svg>"
    ),
    "moon": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{color}' stroke-width='2' stroke-linecap='round'"
        " stroke-linejoin='round'>"
        "<path d='M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z'/></svg>"
    ),
    "log": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{color}' stroke-width='2' stroke-linecap='round'>"
        "<rect x='4.3' y='4.3' width='15.4' height='15.4' rx='2.9'/>"
        "<path d='M7.2 8.6h9.6M7.2 12h9.6M7.2 15.4h6.7'/></svg>"
    ),
    "github": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
        f"<path fill='{{color}}' d='{GITHUB_MARK_PATH}'/></svg>"
    ),
}

# Oversampling factor: icons are rasterized 3x and carry a matching
# devicePixelRatio, so they stay sharp on high-DPI screens.
_SIDEBAR_ICON_SCALE = 3


def _sidebar_icon_pixmap(kind: str, color_hex: str, size: int) -> QtGui.QPixmap:
    svg_markup = _SIDEBAR_ICON_SVGS[kind].replace("{color}", color_hex)
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg_markup.encode("utf-8")))
    pixels = size * _SIDEBAR_ICON_SCALE
    pixmap = QtGui.QPixmap(pixels, pixels)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    renderer.render(painter, QtCore.QRectF(0, 0, pixels, pixels))
    painter.end()
    pixmap.setDevicePixelRatio(_SIDEBAR_ICON_SCALE)
    return pixmap


def _make_sidebar_icon(
    kind: str, color_hex: str, hover_hex: Optional[str] = None, size: int = 18
) -> QtGui.QIcon:
    """Build a HiDPI-crisp sidebar icon from inline SVG.

    ``hover_hex`` adds a brighter ``QIcon.Active`` variant used on hover;
    without it the icon has a single normal state.
    """
    icon = QtGui.QIcon(_sidebar_icon_pixmap(kind, color_hex, size))
    if hover_hex:
        icon.addPixmap(_sidebar_icon_pixmap(kind, hover_hex, size), QtGui.QIcon.Active)
    return icon


# SSH transport (SSHClientWrapper, UnknownHostKeyError, remount_rw,
# require_connection) lives in _ssh.py.  Imported here after
# known_hosts_path / host_key_fingerprint are defined so _ssh.py can
# resolve them via its lazy getters.

def friendly_mode_label(mode: str) -> str:
    if mode == "wifi":
        return "Wi-Fi"
    if mode == "usb":
        return "USB"
    return mode or "Unknown"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def new_device_id() -> str:
    return str(uuid.uuid4())


def find_device_by_id(config: Dict, device_id: str) -> Dict:
    for device in config.get("devices", []):
        if device.get("id") == device_id:
            return device
    return {}


def find_device_by_name(config: Dict, name: str) -> Dict:
    for device in config.get("devices", []):
        if device.get("name") == name:
            return device
    return {}


def active_device(config: Dict) -> Dict:
    devices = config.get("devices", [])
    if not devices:
        return {}
    device = find_device_by_id(config, config.get("active_device_id", ""))
    if not device:
        device = find_device_by_name(config, config.get("active_device", ""))
    return device or devices[0]


def normalise_config(config: Dict) -> Dict:
    devices = config.get("devices") or []
    for device in devices:
        if not device.get("id"):
            device["id"] = new_device_id()

    if devices:
        active = active_device(config) or devices[0]
        config["active_device_id"] = active["id"]
        config["active_device"] = active.get("name", "")
    else:
        config["active_device_id"] = ""
        config["active_device"] = ""
    return config


def _default_config() -> Dict:
    return {
        "active_device_id": "",
        "active_device": "",
        "devices": [],
        "paths": {
            "font": DEFAULT_FONT_DIR,
            "wallpaper": "/usr/share/remarkable/suspended.png",
        },
        "theme": "dark",
    }


def load_config() -> Dict:
    path = Path(__file__).resolve().parent / ".rmtool" / CONFIG_FILE
    try:
        path = config_path()
        exists = path.exists()
    except OSError as exc:
        raise RuntimeError(f"Could not read configuration {path}: {exc}") from exc

    if not exists:
        config = _default_config()
        save_config(config)
        return config

    try:
        with path.open("r", encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read configuration {path}: {exc}") from exc

    if not isinstance(config, dict):
        raise RuntimeError(f"Invalid configuration in {path}: root must be an object")
    if "devices" in config and not isinstance(config["devices"], list):
        raise RuntimeError(f"Invalid configuration in {path}: devices must be a list")
    devices = config.get("devices", [])
    if any(not isinstance(device, dict) for device in devices):
        raise RuntimeError(f"Invalid configuration in {path}: every device must be an object")
    for index, device in enumerate(devices):
        name = device.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(
                f"Invalid configuration in {path}: device {index} name must be a non-empty string"
            )
    if "paths" in config and not isinstance(config["paths"], dict):
        raise RuntimeError(f"Invalid configuration in {path}: paths must be an object")

    before_normalise = json.dumps(config, sort_keys=True, ensure_ascii=False)
    config.setdefault("devices", [])
    paths = config.setdefault("paths", {})
    paths.setdefault("font", DEFAULT_FONT_DIR)
    paths.setdefault("wallpaper", "/usr/share/remarkable/suspended.png")
    config.setdefault("theme", "dark")
    normalise_config(config)
    if json.dumps(config, sort_keys=True, ensure_ascii=False) != before_normalise:
        save_config(config)
    return config


def save_config(config: Dict) -> None:
    path = Path(__file__).resolve().parent / ".rmtool" / CONFIG_FILE
    fd = None
    temp_path = None
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        fh = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        fd = None
        with fh:
            json.dump(config, fh, indent=4, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Could not save configuration {path}: {exc}") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def config_path() -> Path:
    return app_state_dir() / CONFIG_FILE


def known_hosts_path() -> Path:
    return app_state_dir() / KNOWN_HOSTS_FILE


def host_key_fingerprint(host_key: paramiko.PKey) -> str:
    return ":".join(f"{byte:02x}" for byte in host_key.get_fingerprint())


from _ssh import (
    remount_rw,
    require_connection,
    UnknownHostKeyError,
    SSHClientWrapper,
)


def pdf_page_count(file_path: str) -> int:
    with open(file_path, "rb") as fh:
        data = fh.read()

    page_tree_counts = [
        int(match.group(1))
        for match in re.finditer(
            rb"/Type\s*/Pages\b[\s\S]{0,4096}?/Count\s+(\d+)",
            data,
        )
    ]
    if page_tree_counts:
        return max(page_tree_counts)

    page_objects = len(re.findall(rb"/Type\s*/Page\b", data))
    if page_objects > 0:
        return page_objects

    logging.warning("Falling back to single-page PDF metadata for %s", file_path)
    return 1



# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class DocumentItem:
    identifier: str
    name: str
    doc_type: str
    updated: Optional[datetime]
    available_assets: List[str]


def is_active_document_metadata(metadata: object) -> bool:
    """Return whether parsed metadata represents a visible document."""
    if not isinstance(metadata, dict):
        return True
    parent = metadata.get("parent")
    return metadata.get("deleted") is not True and not (
        isinstance(parent, str) and parent.strip().casefold() == "trash"
    )


def load_document_items(sftp: paramiko.SFTPClient) -> List[DocumentItem]:
    """Read and sort document metadata from an existing SFTP session."""
    try:
        entries = sftp.listdir_attr(DOCUMENT_ROOT)
    except IOError:
        return []

    filenames = {entry.filename for entry in entries}
    items = []
    for entry in (entry for entry in entries if entry.filename.endswith(".metadata")):
        identifier = entry.filename[:-9]
        metadata_path = f"{DOCUMENT_ROOT}/{entry.filename}"
        try:
            with sftp.open(metadata_path, "r") as file_handle:
                metadata = json.load(file_handle)
        except Exception:
            metadata = {}

        if not is_active_document_metadata(metadata):
            continue
        if not isinstance(metadata, dict):
            metadata = {}

        available_assets = [
            extension
            for extension in ("pdf", "epub", "zip", "note")
            if f"{identifier}.{extension}" in filenames
        ]
        if identifier in filenames:
            available_assets.append("rm")

        updated = datetime.fromtimestamp(entry.st_mtime) if entry.st_mtime else None
        items.append(
            DocumentItem(
                identifier,
                metadata.get("visibleName", identifier),
                metadata.get("type", "document"),
                updated,
                available_assets,
            )
        )

    items.sort(key=lambda item: item.updated or datetime.min, reverse=True)
    return items


def read_document_cover(
    sftp: paramiko.SFTPClient, item: DocumentItem
) -> Optional[bytes]:
    """Read the first supported thumbnail from an existing SFTP session."""
    thumbnail_dir = f"{DOCUMENT_ROOT}/{item.identifier}.thumbnails"
    try:
        entries = sftp.listdir_attr(thumbnail_dir)
    except IOError:
        return None

    image_entries = [
        entry
        for entry in entries
        if entry.filename.lower().endswith((".png", ".jpg", ".jpeg", ".thumbnail"))
    ]
    if not image_entries:
        return None

    image_entries.sort(key=lambda entry: entry.filename)
    try:
        with sftp.open(f"{thumbnail_dir}/{image_entries[0].filename}", "rb") as file_handle:
            return file_handle.read() or None
    except IOError:
        return None

# ---------------------------------------------------------------------------
# SSH Client
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Threading helpers
# ---------------------------------------------------------------------------
class WorkerSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(Exception)
    progress = QtCore.pyqtSignal(int, int)


class Worker(QtCore.QRunnable):
    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @QtCore.pyqtSlot()
    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.signals.finished.emit(result)
        except Exception as exc:  # pragma: no cover - emitted to UI
            logging.exception("Background task failed")
            self.signals.error.emit(exc)


# ---------------------------------------------------------------------------
# Reusable widgets
# ---------------------------------------------------------------------------
class PreviewImageLabel(QtWidgets.QLabel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_pixmap: Optional[QtGui.QPixmap] = None
        self._aspect_ratio: Optional[float] = None
        self._corner_radius = 0.0
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setObjectName("previewImage")
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Expanding)
        policy.setVerticalPolicy(QtWidgets.QSizePolicy.Expanding)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:  # pragma: no cover - layout hint
        return self._aspect_ratio is not None

    def heightForWidth(self, width: int) -> int:  # pragma: no cover - layout hint
        if not self._aspect_ratio:
            return super().heightForWidth(width)
        return max(1, int(width / self._aspect_ratio))

    def sizeHint(self) -> QtCore.QSize:  # pragma: no cover - layout hint
        if self._aspect_ratio:
            base = super().sizeHint()
            height = max(1, int(base.width() / self._aspect_ratio))
            return QtCore.QSize(base.width(), height)
        return super().sizeHint()

    def setPixmap(self, pixmap: QtGui.QPixmap):  # type: ignore[override]
        if pixmap and not pixmap.isNull():
            self._original_pixmap = QtGui.QPixmap(pixmap)
            self._aspect_ratio = pixmap.width() / max(1, pixmap.height())
            super().setPixmap(self._scaled_pixmap())
            self.setText("")
        else:
            self._original_pixmap = None
            self._aspect_ratio = None
            super().setPixmap(QtGui.QPixmap())

    def set_corner_radius(self, radius: float) -> None:
        self._corner_radius = max(0.0, float(radius))
        self.update()

    def corner_radius(self) -> float:
        return self._corner_radius

    def resizeEvent(self, event: QtGui.QResizeEvent):  # pragma: no cover - GUI resize
        super().resizeEvent(event)
        if self._original_pixmap and not self._original_pixmap.isNull():
            super().setPixmap(self._scaled_pixmap())

    def paintEvent(self, event: QtGui.QPaintEvent):  # pragma: no cover - custom preview painting
        if self._corner_radius <= 0 or self.pixmap() is None or self.pixmap().isNull():
            super().paintEvent(event)
            return

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        option = QtWidgets.QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(QtWidgets.QStyle.PE_Widget, option, painter, self)

        rect = QtCore.QRectF(self.contentsRect())
        radius = min(self._corner_radius, rect.width() / 2.0, rect.height() / 2.0)
        clip_path = QtGui.QPainterPath()
        clip_path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip_path)

        pixmap = self.pixmap()
        assert pixmap is not None
        # Qt5 QPixmap.rect() is in physical pixels; derive the logical size
        # manually so DPR-tagged pixmaps are placed at their logical size.
        logical_size = QtCore.QSizeF(pixmap.size()) / pixmap.devicePixelRatioF()
        pixmap_rect = QtCore.QRectF(QtCore.QPointF(0, 0), logical_size)
        pixmap_rect.moveCenter(rect.center())
        painter.drawPixmap(pixmap_rect.topLeft(), pixmap)

    def clear_preview(self):
        self._original_pixmap = None
        self._aspect_ratio = None
        super().setPixmap(QtGui.QPixmap())

    def _scaled_pixmap(self) -> QtGui.QPixmap:
        assert self._original_pixmap is not None
        rect = self.contentsRect()
        target = rect.size()
        if not target.isValid():
            target = self.size()
        # Render at physical pixels and tag the pixmap, otherwise Qt upscales
        # the logical-size pixmap on high-DPI screens and previews look soft.
        dpr = self.devicePixelRatioF()
        scaled = self._original_pixmap.scaled(
            target * dpr,
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        return scaled


class CompactComboBox(QtWidgets.QComboBox):
    def __init__(self, *args, maximum_hint_width: Optional[int] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._maximum_hint_width = maximum_hint_width

    def _apply_hint_cap(self, hint: QtCore.QSize) -> QtCore.QSize:
        if self._maximum_hint_width:
            hint.setWidth(min(hint.width(), self._maximum_hint_width))
        return hint

    def sizeHint(self) -> QtCore.QSize:  # pragma: no cover - Qt layout hint
        return self._apply_hint_cap(super().sizeHint())

    def minimumSizeHint(self) -> QtCore.QSize:  # pragma: no cover - Qt layout hint
        return self._apply_hint_cap(super().minimumSizeHint())


# ---------------------------------------------------------------------------
# Connection panel
# ---------------------------------------------------------------------------
from _tab_connection import ConnectionWidget



# ---------------------------------------------------------------------------
# Extracted tab modules — re-exported here for backwards-compatible access
# via ``rmtool.WallpaperTab``, ``rmtool.FontTab``, etc.
# ---------------------------------------------------------------------------
from _tab_wallpaper import WallpaperTab
from _tab_documents import DocumentsTab
from _tab_koreader import KOReaderTab
from _tab_dashboard import DashboardTab
from _tab_toolbox import (
    ControlTab,
    FontPage,
    FontTab,
    TimeTab,
    ToolboxTab,
)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    DEFAULT_WIDTH = 1760
    DEFAULT_HEIGHT = 1100
    MIN_WIDTH = 1280
    MIN_HEIGHT = 900

    def __init__(self, log_bridge=None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(self._default_window_size())

        self.config = load_config()
        self._current_theme = self.config.get("theme", "dark")
        self.ssh_client = SSHClientWrapper()
        self._log_bridge = log_bridge
        self._log_panel = None
        self._post_connect_active = False

        # -- Sidebar (connection panel + page navigation) --
        self.connection_widget = ConnectionWidget(self.ssh_client, self.config)
        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(272)
        sidebar_layout = QtWidgets.QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(self.connection_widget)

        # -- Pages --
        self.pages = QtWidgets.QStackedWidget()
        self.dashboard_tab = DashboardTab()
        self.wallpaper_tab = WallpaperTab(self.ssh_client, self.config)
        self.documents_tab = DocumentsTab(self.ssh_client)
        self.koreader_tab = KOReaderTab(self.ssh_client)
        self.font_page = FontPage(self.ssh_client, self.config)
        self.toolbox_tab = ToolboxTab(self.ssh_client, self.config)

        for page in (
            self.dashboard_tab,
            self.wallpaper_tab,
            self.documents_tab,
            self.koreader_tab,
            self.font_page,
            self.toolbox_tab,
        ):
            self.pages.addWidget(page)

        # -- Sidebar navigation (switches stacked pages) --
        nav_container = QtWidgets.QWidget()
        nav_container.setObjectName("sidebarNavSection")
        nav_layout = QtWidgets.QVBoxLayout(nav_container)
        nav_layout.setContentsMargins(0, 12, 0, 0)
        nav_layout.setSpacing(6)
        nav_label = QtWidgets.QLabel("导航")
        nav_label.setObjectName("sidebarSectionLabel")
        nav_layout.addWidget(nav_label)
        # Plain checkable buttons in a vertical layout: the layout sizes them
        # natively, so no fixed-height math and no scroll area can ever clip.
        self.nav_buttons = []
        self.nav_button_group = QtWidgets.QButtonGroup(self)
        self.nav_button_group.setExclusive(True)
        nav_widget = QtWidgets.QWidget()
        nav_widget.setObjectName("sidebarNav")
        nav_buttons_layout = QtWidgets.QVBoxLayout(nav_widget)
        nav_buttons_layout.setContentsMargins(0, 0, 0, 0)
        nav_buttons_layout.setSpacing(6)
        for idx, title in enumerate(("仪表盘", "壁纸管理", "文档中心", "KOReader", "字体管理", "设备工具")):
            button = QtWidgets.QPushButton(title)
            button.setCheckable(True)
            self.nav_button_group.addButton(button, idx)
            nav_buttons_layout.addWidget(button)
            self.nav_buttons.append(button)
        self.nav_button_group.idClicked.connect(self.pages.setCurrentIndex)
        self.pages.currentChanged.connect(self._on_page_changed)
        self.nav_buttons[0].setChecked(True)
        nav_layout.addWidget(nav_widget)
        self.connection_widget.add_sidebar_section(nav_container)

        # -- Horizontal layout: sidebar | pages --
        upper_widget = QtWidgets.QWidget()
        upper_layout = QtWidgets.QHBoxLayout(upper_widget)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(0)
        upper_layout.addWidget(sidebar)
        upper_layout.addWidget(self.pages, 1)

        # -- Vertical splitter: main area on top, log panel on bottom --
        if self._log_bridge is not None:
            self._log_panel = LogViewerPanel(
                self._log_bridge,
                log_file=app_state_dir() / "remarkable_tool.log",
            )
            self._log_panel.close_requested.connect(self._hide_log_panel)
            self._log_panel.setVisible(self.config.get("log_panel_visible", False))

        self._main_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self._main_splitter.setObjectName("mainSplitter")
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(4)
        self._main_splitter.addWidget(upper_widget)
        if self._log_panel is not None:
            self._main_splitter.addWidget(self._log_panel)
            self._main_splitter.setStretchFactor(0, 1)
            self._main_splitter.setStretchFactor(1, 0)
            saved_height = int(self.config.get("log_panel_height", 220))
            QtCore.QTimer.singleShot(
                0, lambda: self._restore_splitter_sizes(saved_height)
            )
        self.setCentralWidget(self._main_splitter)

        status_bar = QtWidgets.QStatusBar(self)
        status_bar.setObjectName("appStatusBar")
        status_bar.setSizeGripEnabled(False)
        self.setStatusBar(status_bar)
        self.connection_status_chip = QtWidgets.QLabel("未连接")
        self.connection_status_chip.setObjectName("appConnectionChip")
        status_bar.addPermanentWidget(self.connection_status_chip)

        # Shorthand references for tool sections
        self.font_tab = self.font_page.font_section
        self.time_tab = self.toolbox_tab.time_section
        self.control_tab = self.toolbox_tab.control_section

        self._update_tabs_enabled(False)
        self.connection_widget.set_footer_theme(self._current_theme)
        self.connection_widget.connected.connect(lambda: self._update_tabs_enabled(True))
        # Post-connect background refreshes run strictly in series via the
        # coordinator: concurrent SSH channels from simultaneous tab refreshes
        # have made the device's dropbear server drop the connection
        # (2026-07-22 incident).
        self.connection_widget.connected.connect(self._start_post_connect_refresh)
        self.connection_widget.disconnected.connect(lambda: self._update_tabs_enabled(False))
        self.connection_widget.connected.connect(lambda: self.documents_tab.set_connection_state(True))
        self.connection_widget.disconnected.connect(lambda: self.documents_tab.set_connection_state(False))
        self.connection_widget.connected.connect(lambda: self.koreader_tab.set_connection_state(True))
        self.connection_widget.disconnected.connect(lambda: self.koreader_tab.set_connection_state(False))
        # KOReader loads lazily: refreshing on connect would add SSH channels
        # to the post-connect background burst, which the device's dropbear
        # server has dropped connections under (see KOReaderTab.ensure_loaded).
        self.connection_widget.connected.connect(
            lambda: self._on_page_changed(self.pages.currentIndex())
        )
        self.connection_widget.device_changed.connect(self.wallpaper_tab.update_device)
        self.connection_widget.device_changed.connect(self._on_device_changed)
        self.connection_widget.device_changed.connect(self.dashboard_tab.update_device)
        self.connection_widget.connected.connect(self._on_connected)
        self.connection_widget.disconnected.connect(self._on_disconnected)
        self.connection_widget.theme_button.clicked.connect(self._toggle_theme)
        self.connection_widget.log_button.clicked.connect(self._toggle_log_panel)
        self.connection_widget.status_message.connect(self._show_status_message)
        self.documents_tab.status_message.connect(self._show_status_message)
        self.documents_tab.summary_changed.connect(self.dashboard_tab.update_documents)
        self.koreader_tab.status_message.connect(self._show_status_message)

        # Initialize wallpaper profile preview
        initial_device = active_device(self.config)
        self.wallpaper_tab.update_device(initial_device)
        self.dashboard_tab.update_device(initial_device)
        self.dashboard_tab.update_documents(self.documents_tab.current_summary())
        self.dashboard_tab.set_theme(self._current_theme)
        self.dashboard_tab.update_connection(False, initial_device)
        self.documents_tab.set_connection_state(False)
        self._set_connection_chip(False, initial_device)

    def _default_window_size(self) -> QtCore.QSize:
        """Size the window to ~80% of the usable screen, within design bounds."""
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return QtCore.QSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        available = screen.availableGeometry()
        width = max(self.MIN_WIDTH, min(self.DEFAULT_WIDTH, int(available.width() * 0.8)))
        height = max(self.MIN_HEIGHT, min(self.DEFAULT_HEIGHT, int(available.height() * 0.85)))
        return QtCore.QSize(width, height)

    def _update_tabs_enabled(self, enabled: bool):
        for idx in range(self.pages.count()):
            widget = self.pages.widget(idx)
            if widget is self.dashboard_tab:
                continue
            widget.setEnabled(enabled)
            self.nav_buttons[idx].setEnabled(enabled)

    def _on_page_changed(self, index: int) -> None:
        if self.pages.widget(index) is self.koreader_tab:
            self.koreader_tab.ensure_loaded()

    def _show_status_message(self, level: str, text: str, timeout: int = 4000) -> None:
        status_bar = self.statusBar()
        status_bar.setProperty("level", level)
        status_bar.style().unpolish(status_bar)
        status_bar.style().polish(status_bar)
        status_bar.showMessage(text, timeout)

    def _toggle_log_panel(self) -> None:
        if self._log_panel is None:
            return
        if self._log_panel.isVisible():
            self._hide_log_panel()
        else:
            self._log_panel.setVisible(True)
            self.config["log_panel_visible"] = True
            save_config(self.config)
            self._restore_splitter_sizes(int(self.config.get("log_panel_height", 220)))

    def _hide_log_panel(self) -> None:
        if self._log_panel is None or not self._log_panel.isVisible():
            return
        self._capture_log_panel_height()
        self._log_panel.setVisible(False)
        self.config["log_panel_visible"] = False
        save_config(self.config)

    def _capture_log_panel_height(self) -> None:
        """Snapshot the current panel height into config (without saving to disk)."""
        if self._log_panel is None or not self._log_panel.isVisible():
            return
        height = self._main_splitter.sizes()[1]
        if height > 0:
            self.config["log_panel_height"] = height

    def _restore_splitter_sizes(self, panel_height: int) -> None:
        total = self._main_splitter.height() or self.height()
        if total <= 0:
            return
        panel_height = max(self._log_panel.minimumHeight(), min(panel_height, total - 200))
        self._main_splitter.setSizes([total - panel_height, panel_height])

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._log_panel is not None and self._log_panel.isVisible():
            self._capture_log_panel_height()
            save_config(self.config)
        super().closeEvent(event)

    def _set_connection_chip(self, connected: bool, device: Optional[Dict] = None) -> None:
        if connected and device:
            mode = friendly_mode_label(device.get("mode", "usb"))
            name = device.get("name", "当前设备")
            self.connection_status_chip.setText(f"{mode} · {name}")
            self.connection_status_chip.setProperty("connected", True)
        else:
            self.connection_status_chip.setText("未连接")
            self.connection_status_chip.setProperty("connected", False)
        self.connection_status_chip.style().unpolish(self.connection_status_chip)
        self.connection_status_chip.style().polish(self.connection_status_chip)

    def _on_device_changed(self, device: Dict):
        if self.ssh_client.is_connected():
            self._start_post_connect_refresh()
            self._set_connection_chip(True, device)
        else:
            self._set_connection_chip(False, device)

    def _start_post_connect_refresh(self) -> None:
        """Run the post-connect background refreshes strictly in series.

        Steps run in a fixed order — documents, wallpaper previews, fonts —
        and each step starts only after the previous one called its
        ``on_done`` callback, so at most one background task uses SSH at any
        moment. Concurrent post-connect refreshes opened several SSH channels
        within milliseconds, under which the device's dropbear server has
        dropped the connection (2026-07-22 incident). A failing step never
        blocks the remaining steps. Reentrant while a sequence is active.
        """
        if self._post_connect_active:
            logging.info("Post-connect refresh already running; skipping duplicate start")
            return
        self._post_connect_active = True
        steps = (
            self.documents_tab.refresh_quiet,
            self.wallpaper_tab.refresh_previews_quiet,
            self.font_tab.refresh_fonts_quiet,
        )

        def run_step(index: int) -> None:
            if sip.isdeleted(self):
                return
            if index >= len(steps):
                self._post_connect_active = False
                return
            step = steps[index]
            done_called = False

            def on_done() -> None:
                nonlocal done_called
                if sip.isdeleted(self):
                    return
                if done_called:
                    return
                done_called = True
                run_step(index + 1)

            try:
                step(on_done)
            except Exception as exc:
                logging.error("Post-connect refresh step failed: %s", exc)
                on_done()

        run_step(0)

    def _on_connected(self):
        device = self.connection_widget.current_device()
        self.dashboard_tab.update_connection(True, device)
        self._set_connection_chip(True, device)

    def _on_disconnected(self):
        device = self.connection_widget.current_device()
        self.dashboard_tab.update_connection(False, device)
        self._set_connection_chip(False, device)

    def _toggle_theme(self):
        current = getattr(self, "_current_theme", "dark")
        new_theme = "light" if current == "dark" else "dark"
        self._current_theme = new_theme

        app = QtWidgets.QApplication.instance()
        if new_theme == "dark":
            app.setPalette(_dark_palette())
            app.setStyleSheet(_resolve_stylesheet(_DARK_STYLESHEET))
        else:
            app.setPalette(_light_palette())
            app.setStyleSheet(_resolve_stylesheet(_LIGHT_STYLESHEET))
        self.connection_widget.set_footer_theme(new_theme)

        # Persist preference
        self.config["theme"] = new_theme
        save_config(self.config)

        # Dashboard re-styles via the global QSS; set_theme is a compat no-op
        self.dashboard_tab.set_theme(new_theme)

        # Force status dot to re-apply its dynamic property style
        dot = self.connection_widget.status_dot
        dot.style().unpolish(dot)
        dot.style().polish(dot)


# ---------------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------------

# Stylesheets and palette definitions live in _styles.py to keep this file
# focused on application logic.
from _styles import _DARK_STYLESHEET, _LIGHT_STYLESHEET, _dark_palette, _light_palette
from _tokens import FONT_BASE
from _log_viewer import LogViewerPanel, attach_qt_log_handler




_STYLESHEET_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_stylesheet(template: str) -> str:
    """Substitute named ``{placeholder}`` fields in a stylesheet template.

    Resolves the runtime-generated ``{arrow_*}`` combo box icon paths (and,
    for backward compatibility, the legacy ``{panel_*}`` layout fields, which
    are now normally baked in from ``_tokens.py``).  Only ``{identifier}``
    fields are touched; the literal braces of QSS rules are left intact.
    """
    replacements = {
        **_ARROW_ICONS,
        "panel_radius": f"{PANEL_RADIUS}px",
        "inner_panel_radius": f"{INNER_PANEL_RADIUS}px",
        "panel_padding": f"{PANEL_PADDING}px",
    }
    return _STYLESHEET_PLACEHOLDER_RE.sub(
        lambda match: replacements.get(match.group(1), match.group(0)),
        template,
    )


# Generated once at import time so the icon files exist before any stylesheet
# is applied.  The dict maps placeholder names to forward-slash file paths.
_ARROW_ICONS = _generate_arrow_icons()


def main():
    # -- High-DPI: let Qt scale the UI by the OS display factor (must be set
    # before QApplication is created) so text stays readable on 4K screens --
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QtWidgets.QApplication(sys.argv)
    QtWidgets.QApplication.setStyle("Fusion")

    # -- Global font: px size from the shared type scale (single unit) --
    font = QtGui.QFont("Segoe UI")
    font.setPixelSize(FONT_BASE)
    font.setStyleHint(QtGui.QFont.SansSerif)
    app.setFont(font)

    # -- Load saved theme preference, default to dark --
    config = load_config()
    theme = config.get("theme", "dark")

    app.setPalette(_dark_palette() if theme == "dark" else _light_palette())
    app.setStyleSheet(
        _resolve_stylesheet(_DARK_STYLESHEET if theme == "dark" else _LIGHT_STYLESHEET)
    )

    # In-app log bridge: routes records into the runtime log viewer dialog.
    log_bridge = attach_qt_log_handler(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )

    window = MainWindow(log_bridge=log_bridge)
    window._current_theme = theme
    window.connection_widget.set_footer_theme(theme)
    window.dashboard_tab.set_theme(theme)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
