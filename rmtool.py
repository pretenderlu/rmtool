import json
import logging
import logging.handlers
import math
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
from typing import Callable, Dict, List, Optional, Tuple

import paramiko
from PIL import Image
from PyQt5 import QtCore, QtGui, QtSvg, QtWidgets

try:  # Optional dependency for secure credential storage
    import keyring
except Exception:  # pragma: no cover - optional dependency
    keyring = None


APP_NAME = "reMarkable 管理工具"
CONFIG_FILE = "config.json"
GITHUB_REPO_URL = "https://github.com/pretenderlu/rmtool"
GITHUB_MARK_PATH = "M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"
DEFAULT_FONT_NAME = "zwzt.ttf"
DEFAULT_FONT_DIR = "/home/root/.local/share/fonts/"
LEGACY_FONT_DIR = "/usr/share/fonts/ttf/noto/"
DOCUMENT_ROOT = "/home/root/.local/share/remarkable/xochitl"
KEYRING_SERVICE = "rmtool"
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
    "reMarkable 2": (1404, 1872),
}

DEVICE_PROFILE_LABELS = {
    "reMarkable Paper Pro": "Paper Pro",
    "reMarkable Paper Pro Move": "Paper Pro Move",
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
    if sys.platform.startswith("win"):
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = root / "rmtool"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Logging – use rotating handler to prevent unbounded log growth
# ---------------------------------------------------------------------------
_log_handler = logging.handlers.RotatingFileHandler(
    str(app_state_dir() / "remarkable_tool.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
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


def _draw_sun_icon(painter: QtGui.QPainter, color: QtGui.QColor, size: int) -> None:
    pen = QtGui.QPen(color, 1.8, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
    center = QtCore.QPointF(size / 2, size / 2)
    core_radius = size * 0.18
    painter.drawEllipse(center, core_radius, core_radius)

    inner = size * 0.33
    outer = size * 0.45
    for angle in range(0, 360, 45):
        radians = math.radians(angle)
        start = QtCore.QPointF(
            center.x() + math.cos(radians) * inner,
            center.y() + math.sin(radians) * inner,
        )
        end = QtCore.QPointF(
            center.x() + math.cos(radians) * outer,
            center.y() + math.sin(radians) * outer,
        )
        painter.drawLine(start, end)


def _draw_moon_icon(painter: QtGui.QPainter, color: QtGui.QColor, size: int) -> None:
    outer = QtGui.QPainterPath()
    outer.addEllipse(QtCore.QRectF(size * 0.2, size * 0.14, size * 0.52, size * 0.68))
    inner = QtGui.QPainterPath()
    inner.addEllipse(QtCore.QRectF(size * 0.38, size * 0.1, size * 0.48, size * 0.72))
    painter.fillPath(outer.subtracted(inner), color)


def _draw_github_icon(painter: QtGui.QPainter, color: QtGui.QColor, size: int) -> None:
    svg_markup = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
        f"<path fill='{color.name()}' d='{GITHUB_MARK_PATH}'/></svg>"
    )
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg_markup.encode("utf-8")))
    renderer.render(painter, QtCore.QRectF(0, 0, size, size))


def _draw_log_icon(painter: QtGui.QPainter, color: QtGui.QColor, size: int) -> None:
    pen = QtGui.QPen(color)
    pen.setWidthF(size * 0.11)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)

    rect = QtCore.QRectF(size * 0.18, size * 0.18, size * 0.64, size * 0.64)
    radius = size * 0.12
    painter.drawRoundedRect(rect, radius, radius)

    line_x1 = size * 0.30
    line_x2 = size * 0.70
    for i, frac in enumerate((0.36, 0.50, 0.64)):
        y = size * frac
        x2 = line_x2 if i != 2 else size * 0.58
        painter.drawLine(QtCore.QPointF(line_x1, y), QtCore.QPointF(x2, y))


def _make_sidebar_icon(kind: str, color_hex: str, size: int = 18) -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    color = QtGui.QColor(color_hex)

    if kind == "sun":
        _draw_sun_icon(painter, color, size)
    elif kind == "moon":
        _draw_moon_icon(painter, color, size)
    elif kind == "github":
        _draw_github_icon(painter, color, size)
    elif kind == "log":
        _draw_log_icon(painter, color, size)
    else:  # pragma: no cover - defensive programming
        raise ValueError(f"Unsupported sidebar icon kind: {kind}")

    painter.end()
    return QtGui.QIcon(pixmap)


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


def device_credential_key(device: Dict) -> str:
    return f"device:{device.get('id') or device.get('name', '')}"


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
        device.setdefault("id", new_device_id())

    if devices:
        active = active_device(config)
        if not active:
            active = devices[0]
        config["active_device_id"] = active["id"]
        config["active_device"] = active["name"]
    return config


def _device_merge_key(device: Dict) -> Tuple[str, str]:
    return (
        str(device.get("name", "")).strip().casefold(),
        str(device.get("host", "")).strip(),
    )


def _has_only_default_device(config: Dict) -> bool:
    devices = config.get("devices") or []
    if len(devices) != 1:
        return False
    device = devices[0]
    return (
        device.get("name") == "默认设备"
        and device.get("host") == "10.11.99.1"
        and device.get("mode", "usb") == "usb"
    )


def _merge_legacy_devices(config: Dict, legacy_config: Dict) -> bool:
    if config.get("legacy_devices_imported"):
        return False
    if not _has_only_default_device(config):
        return False
    legacy_devices = legacy_config.get("devices") or []
    if not legacy_devices:
        return False

    devices = config.setdefault("devices", [])
    seen = {_device_merge_key(device) for device in devices}
    added = False
    for legacy_device in legacy_devices:
        key = _device_merge_key(legacy_device)
        if key in seen:
            continue
        merged = {
            "id": str(legacy_device.get("id") or new_device_id()),
            "name": legacy_device.get("name", "未命名设备"),
            "mode": legacy_device.get("mode", "usb"),
            "host": legacy_device.get("host", "10.11.99.1"),
            "type": legacy_device.get("type", "reMarkable Paper Pro"),
        }
        devices.append(merged)
        seen.add(_device_merge_key(merged))
        added = True

    if not added:
        return False

    legacy_active = legacy_config.get("active_device", "")
    active = find_device_by_name(config, legacy_active)
    if active:
        config["active_device_id"] = active["id"]
        config["active_device"] = active["name"]
    config["legacy_devices_imported"] = True
    return True


def _default_config() -> Dict:
    first_device = {
        "id": new_device_id(),
        "name": "默认设备",
        "mode": "usb",
        "host": "10.11.99.1",
        "type": "reMarkable Paper Pro",
    }
    return {
        "active_device_id": first_device["id"],
        "active_device": first_device["name"],
        "devices": [first_device],
        "paths": {
            "font": DEFAULT_FONT_DIR,
            "wallpaper": "/usr/share/remarkable/suspended.png",
        },
    }


def load_config() -> Dict:
    source_path: Optional[Path] = None
    preferred_path = config_path()
    legacy_path = legacy_config_path()
    needs_save = False

    if preferred_path.exists():
        source_path = preferred_path
    elif legacy_path.exists():
        source_path = legacy_path

    legacy_config_for_merge = None
    if source_path == preferred_path and legacy_path.exists():
        try:
            with legacy_path.open("r", encoding="utf-8") as fh:
                legacy_config_for_merge = json.load(fh)
        except (OSError, ValueError):
            legacy_config_for_merge = None

    if source_path:
        with source_path.open("r", encoding="utf-8") as fh:
            config = json.load(fh)
    else:
        config = _default_config()

    # Migration from legacy structure
    if "devices" not in config:
        connection = config.get("connection", {})
        mode = connection.get("mode", "usb")
        host = connection.get(mode, {}).get("host", "10.11.99.1")
        migrated = {
            "id": new_device_id(),
            "name": "默认设备",
            "mode": mode,
            "host": host,
            "type": "reMarkable Paper Pro",
        }
        config = {
            "active_device_id": migrated["id"],
            "active_device": migrated["name"],
            "devices": [migrated],
            "paths": config.get(
                "paths",
                {
                    "font": DEFAULT_FONT_DIR,
                    "wallpaper": "/usr/share/remarkable/suspended.png",
                },
            ),
        }
        needs_save = True

    # Ensure defaults exist
    if "devices" not in config or not config["devices"]:
        config = _default_config()
        needs_save = True
    if "active_device" not in config:
        config["active_device"] = config["devices"][0]["name"]
        needs_save = True
    if "paths" not in config:
        config["paths"] = {
            "font": DEFAULT_FONT_DIR,
            "wallpaper": "/usr/share/remarkable/suspended.png",
        }
        needs_save = True
    else:
        if "font" not in config["paths"]:
            config["paths"]["font"] = DEFAULT_FONT_DIR
            needs_save = True
        if "wallpaper" not in config["paths"]:
            config["paths"]["wallpaper"] = "/usr/share/remarkable/suspended.png"
            needs_save = True

    # Migrate legacy font directory to persistent location
    font_path = config.get("paths", {}).get("font")
    if not font_path or font_path == LEGACY_FONT_DIR:
        config["paths"]["font"] = DEFAULT_FONT_DIR
        needs_save = True

    before_normalise = json.dumps(config, sort_keys=True, ensure_ascii=False)
    normalise_config(config)
    if json.dumps(config, sort_keys=True, ensure_ascii=False) != before_normalise:
        needs_save = True

    if legacy_config_for_merge and _merge_legacy_devices(config, legacy_config_for_merge):
        normalise_config(config)
        needs_save = True

    if source_path == legacy_path or (source_path and needs_save):
        save_config(config)
    return config


def save_config(config: Dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=4, ensure_ascii=False)


def config_path() -> Path:
    return app_state_dir() / CONFIG_FILE


def legacy_config_path() -> Path:
    return Path(CONFIG_FILE).resolve()


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
        pixmap_rect = QtCore.QRectF(pixmap.rect())
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
        return self._original_pixmap.scaled(
            target,
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )


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
from _tab_toolbox import (
    ControlTab,
    DashboardTab,
    FontTab,
    TimeTab,
    ToolboxTab,
)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, log_bridge=None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1280, 900)
        self.resize(1760, 1100)

        self.config = load_config()
        self._current_theme = self.config.get("theme", "dark")
        self.ssh_client = SSHClientWrapper()
        self._log_bridge = log_bridge
        self._log_panel = None

        # -- Sidebar (connection panel) --
        self.connection_widget = ConnectionWidget(self.ssh_client, self.config)
        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(320)
        sidebar_layout = QtWidgets.QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(self.connection_widget)

        # -- Tabs --
        self.tabs = QtWidgets.QTabWidget()
        self.dashboard_tab = DashboardTab()
        self.wallpaper_tab = WallpaperTab(self.ssh_client, self.config)
        self.documents_tab = DocumentsTab(self.ssh_client)
        self.toolbox_tab = ToolboxTab(self.ssh_client, self.config)

        self.tabs.addTab(self.dashboard_tab, "仪表盘")
        self.tabs.addTab(self.wallpaper_tab, "壁纸管理")
        self.tabs.addTab(self.documents_tab, "文档中心")
        self.tabs.addTab(self.toolbox_tab, "设备工具箱")

        # -- Horizontal layout: sidebar | tabs --
        upper_widget = QtWidgets.QWidget()
        upper_layout = QtWidgets.QHBoxLayout(upper_widget)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(0)
        upper_layout.addWidget(sidebar)
        upper_layout.addWidget(self.tabs, 1)

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

        # Shorthand references for sub-widgets inside the toolbox
        self.font_tab = self.toolbox_tab.font_section
        self.time_tab = self.toolbox_tab.time_section
        self.control_tab = self.toolbox_tab.control_section

        self._update_tabs_enabled(False)
        self.connection_widget.set_footer_theme(self._current_theme)
        self.connection_widget.connected.connect(lambda: self._update_tabs_enabled(True))
        self.connection_widget.connected.connect(self.documents_tab.refresh)
        self.connection_widget.disconnected.connect(lambda: self._update_tabs_enabled(False))
        self.connection_widget.connected.connect(lambda: self.documents_tab.set_connection_state(True))
        self.connection_widget.disconnected.connect(lambda: self.documents_tab.set_connection_state(False))
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

        # Initialize wallpaper profile preview
        initial_device = active_device(self.config)
        self.wallpaper_tab.update_device(initial_device)
        self.dashboard_tab.update_device(initial_device)
        self.dashboard_tab.update_documents(self.documents_tab.current_summary())
        self.dashboard_tab.set_theme(self._current_theme)
        self.dashboard_tab.update_connection(False, initial_device)
        self.documents_tab.set_connection_state(False)
        self._set_connection_chip(False, initial_device)

    def _update_tabs_enabled(self, enabled: bool):
        for idx in range(self.tabs.count()):
            widget = self.tabs.widget(idx)
            if widget is self.dashboard_tab:
                continue
            widget.setEnabled(enabled)

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
            self.documents_tab.refresh()
            self._set_connection_chip(True, device)
        else:
            self._set_connection_chip(False, device)

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

        # Sync dashboard web view theme
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
from _log_viewer import LogViewerPanel, attach_qt_log_handler




def _resolve_stylesheet(template: str) -> str:
    """Replace ``{arrow_*}`` placeholders with actual icon file paths."""
    result = template
    replacements = {
        **_ARROW_ICONS,
        "panel_radius": f"{PANEL_RADIUS}px",
        "inner_panel_radius": f"{INNER_PANEL_RADIUS}px",
        "panel_padding": f"{PANEL_PADDING}px",
    }
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", value)
    return result


# Generated once at import time so the icon files exist before any stylesheet
# is applied.  The dict maps placeholder names to forward-slash file paths.
_ARROW_ICONS = _generate_arrow_icons()


def main():
    app = QtWidgets.QApplication(sys.argv)
    QtWidgets.QApplication.setStyle("Fusion")

    # -- Global font: use point size so it scales with system DPI --
    font = QtGui.QFont("Segoe UI", 12)
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
