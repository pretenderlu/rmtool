import json
import logging
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from functools import partial
from contextlib import contextmanager
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import paramiko
from PIL import Image
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5 import QtWebEngineWidgets

try:  # Optional dependency for secure credential storage
    import keyring
except Exception:  # pragma: no cover - optional dependency
    keyring = None


APP_NAME = "reMarkable 管理工具"
CONFIG_FILE = "config.json"
DEFAULT_FONT_NAME = "zwzt.ttf"
DEFAULT_FONT_DIR = "/home/root/.local/share/fonts/"
LEGACY_FONT_DIR = "/usr/share/fonts/ttf/noto/"
DOCUMENT_ROOT = "/home/root/.local/share/remarkable/xochitl"
KEYRING_SERVICE = "rmtool"

DEVICE_PROFILES = {
    "reMarkable Paper Pro": (2160, 1620),
    "reMarkable Paper Pro Move": (1696, 954),
    "reMarkable 2": (1404, 1872),
}

WALLPAPER_VARIANTS = [
    ("starting", "启动壁纸", "/usr/share/remarkable/starting.png"),
    ("suspended", "待机壁纸", "/usr/share/remarkable/suspended.png"),
    ("hibernate", "休眠壁纸", "/usr/share/remarkable/hibernate.png"),
    ("poweroff", "关机壁纸", "/usr/share/remarkable/poweroff.png"),
]


logging.basicConfig(
    filename="remarkable_tool.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def resource_path(*parts: str) -> Path:
    """Return absolute path for bundled resources.

    When packaged with PyInstaller the assets live inside ``_MEIPASS``. During
    development we fall back to the repository layout.
    """

    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path.joinpath(*parts)


def _default_config() -> Dict:
    first_device = {
        "name": "默认设备",
        "mode": "usb",
        "host": "10.11.99.1",
        "type": "reMarkable Paper Pro",
    }
    return {
        "active_device": first_device["name"],
        "devices": [first_device],
        "paths": {
            "font": DEFAULT_FONT_DIR,
            "wallpaper": "/usr/share/remarkable/suspended.png",
        },
    }


def load_config() -> Dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    else:
        config = _default_config()

    # Migration from legacy structure
    if "devices" not in config:
        connection = config.get("connection", {})
        mode = connection.get("mode", "usb")
        host = connection.get(mode, {}).get("host", "10.11.99.1")
        migrated = {
            "name": "默认设备",
            "mode": mode,
            "host": host,
            "type": "reMarkable Paper Pro",
        }
        config = {
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

    # Ensure defaults exist
    if "devices" not in config or not config["devices"]:
        config = _default_config()
    if "active_device" not in config:
        config["active_device"] = config["devices"][0]["name"]
    if "paths" not in config:
        config["paths"] = {
            "font": DEFAULT_FONT_DIR,
            "wallpaper": "/usr/share/remarkable/suspended.png",
        }
    else:
        config["paths"].setdefault("font", DEFAULT_FONT_DIR)
        config["paths"].setdefault("wallpaper", "/usr/share/remarkable/suspended.png")

    # Migrate legacy font directory to persistent location
    font_path = config.get("paths", {}).get("font")
    if not font_path or font_path == LEGACY_FONT_DIR:
        config["paths"]["font"] = DEFAULT_FONT_DIR
    return config


def save_config(config: Dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=4, ensure_ascii=False)


@dataclass
class DocumentItem:
    identifier: str
    name: str
    doc_type: str
    updated: Optional[datetime]
    available_assets: List[str]


class SSHClientWrapper(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self._client: Optional[paramiko.SSHClient] = None
        self.connection_info: Dict[str, str] = {}

    def connect(self, host: str, password: str) -> None:
        logging.info("Connecting to %s", host)
        self.close()
        self.connection_info = {"host": host, "password": password}
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, username="root", password=password)
        self._client = client
        self.connection_changed.emit(True)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
        self.connection_changed.emit(False)

    def ensure_client(self) -> paramiko.SSHClient:
        if not self._client:
            raise RuntimeError("未连接到设备")
        return self._client

    @contextmanager
    def sftp_session(self) -> Iterator[paramiko.SFTPClient]:
        client = self.ensure_client()
        sftp = client.open_sftp()
        try:
            yield sftp
        finally:
            try:
                sftp.close()
            except Exception:
                logging.exception("Failed to close SFTP session")

    def is_connected(self) -> bool:
        return self._client is not None

    def exec_command(self, command: str) -> Tuple[str, str]:
        client = self.ensure_client()
        logging.info("Executing command: %s", command)
        stdin, stdout, stderr = client.exec_command(command)
        return stdout.read().decode("utf-8"), stderr.read().decode("utf-8")

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        with self.sftp_session() as sftp:
            logging.info("Transferring %s -> %s", local_path, remote_path)
            sftp.put(local_path, remote_path)

    def file_exists(self, remote_path: str) -> bool:
        with self.sftp_session() as sftp:
            try:
                sftp.stat(remote_path)
                return True
            except IOError:
                return False

    def listdir_attr(self, remote_path: str):
        with self.sftp_session() as sftp:
            return sftp.listdir_attr(remote_path)

    def open_remote(self, remote_path: str, mode: str = "r"):
        @contextmanager
        def _remote_file() -> Iterator[paramiko.SFTPFile]:
            with self.sftp_session() as sftp:
                with sftp.open(remote_path, mode) as fh:
                    yield fh

        return _remote_file()

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        with self.sftp_session() as sftp:
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_path, local_path, callback=callback)

    def download_directory(self, remote_dir: str, local_dir: str) -> None:
        with self.sftp_session() as sftp:
            self._download_directory_recursive(sftp, remote_dir, local_dir)

    def _download_directory_recursive(
        self, sftp: paramiko.SFTPClient, remote_dir: str, local_dir: str
    ) -> None:
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        for entry in sftp.listdir_attr(remote_dir):
            remote_path = f"{remote_dir}/{entry.filename}"
            local_path = os.path.join(local_dir, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                self._download_directory_recursive(sftp, remote_path, local_path)
            else:
                sftp.get(remote_path, local_path)


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


class PreviewImageLabel(QtWidgets.QLabel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_pixmap: Optional[QtGui.QPixmap] = None
        self._aspect_ratio: Optional[float] = None
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setFrameShape(QtWidgets.QFrame.Box)
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

    def resizeEvent(self, event: QtGui.QResizeEvent):  # pragma: no cover - GUI resize
        super().resizeEvent(event)
        if self._original_pixmap and not self._original_pixmap.isNull():
            super().setPixmap(self._scaled_pixmap())

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


class ConnectionWidget(QtWidgets.QGroupBox):
    connected = QtCore.pyqtSignal()
    disconnected = QtCore.pyqtSignal()
    device_changed = QtCore.pyqtSignal(dict)

    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__("连接设置", parent)
        self.ssh_client = ssh_client
        self.config = config

        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._connection_progress: Optional[QtWidgets.QProgressDialog] = None
        self._active_connection_worker: Optional[Worker] = None

        self.device_combo = QtWidgets.QComboBox()
        self.add_device_button = QtWidgets.QToolButton()
        self.add_device_button.setText("+")
        self.remove_device_button = QtWidgets.QToolButton()
        self.remove_device_button.setText("-")
        self.save_device_button = QtWidgets.QToolButton()
        self.save_device_button.setText("💾")

        self.usb_radio = QtWidgets.QRadioButton("USB")
        self.wifi_radio = QtWidgets.QRadioButton("WiFi")
        self.host_edit = QtWidgets.QLineEdit()
        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.remember_checkbox = QtWidgets.QCheckBox("记住密码（使用系统凭证管理器）")
        if keyring is None:
            self.remember_checkbox.setEnabled(False)
            self.remember_checkbox.setToolTip("未找到 keyring 库，无法安全保存密码。")
        self.connect_button = QtWidgets.QPushButton("连接")
        self.disconnect_button = QtWidgets.QPushButton("断开")
        self.status_label = QtWidgets.QLabel("未连接")
        self.status_label.setObjectName("connectionStatusLabel")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.device_type_combo = QtWidgets.QComboBox()
        self.device_type_combo.addItems(DEVICE_PROFILES.keys())

        layout = QtWidgets.QGridLayout()
        layout.addWidget(QtWidgets.QLabel("设备"), 0, 0)
        device_row = QtWidgets.QHBoxLayout()
        device_row.addWidget(self.device_combo)
        device_row.addWidget(self.add_device_button)
        device_row.addWidget(self.remove_device_button)
        device_row.addWidget(self.save_device_button)
        layout.addLayout(device_row, 0, 1, 1, 2)
        layout.addWidget(QtWidgets.QLabel("模式"), 1, 0)
        mode_layout = QtWidgets.QHBoxLayout()
        mode_layout.addWidget(self.usb_radio)
        mode_layout.addWidget(self.wifi_radio)
        layout.addLayout(mode_layout, 1, 1, 1, 2)
        layout.addWidget(QtWidgets.QLabel("地址"), 2, 0)
        layout.addWidget(self.host_edit, 2, 1, 1, 2)
        layout.addWidget(QtWidgets.QLabel("设备类型"), 3, 0)
        layout.addWidget(self.device_type_combo, 3, 1, 1, 2)
        layout.addWidget(QtWidgets.QLabel("root 密码"), 4, 0)
        layout.addWidget(self.password_edit, 4, 1, 1, 2)
        layout.addWidget(self.remember_checkbox, 5, 1, 1, 2)
        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(self.disconnect_button)
        layout.addLayout(button_layout, 6, 0, 1, 3)
        layout.addWidget(self.status_label, 7, 0, 1, 3)
        self.setLayout(layout)

        self.disconnect_button.setEnabled(False)

        self.device_combo.currentIndexChanged.connect(self._on_device_selected)
        self.add_device_button.clicked.connect(self._add_device)
        self.remove_device_button.clicked.connect(self._remove_device)
        self.save_device_button.clicked.connect(self._save_device)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.usb_radio.toggled.connect(self._emit_device_preview)
        self.wifi_radio.toggled.connect(self._emit_device_preview)
        self.device_type_combo.currentIndexChanged.connect(self._emit_device_preview)
        ssh_client.connection_changed.connect(self._on_connection_changed)

        self._populate_devices()

    # Device management helpers -------------------------------------------------
    def _populate_devices(self):
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in self.config.get("devices", []):
            self.device_combo.addItem(device["name"])
        self.device_combo.blockSignals(False)
        active = self.config.get("active_device")
        if active:
            idx = self.device_combo.findText(active)
            if idx != -1:
                self.device_combo.setCurrentIndex(idx)
        if self.device_combo.count():
            self._on_device_selected(self.device_combo.currentIndex())

    def _current_device(self) -> Dict:
        name = self.device_combo.currentText()
        for device in self.config.get("devices", []):
            if device["name"] == name:
                return device
        return {}

    def current_device(self) -> Dict:
        """Expose the currently selected device for other widgets."""

        return self._current_device().copy()

    def _on_device_selected(self, index: int):
        if index < 0:
            return
        device = self._current_device()
        if not device:
            return
        mode = device.get("mode", "usb")
        self.usb_radio.setChecked(mode == "usb")
        self.wifi_radio.setChecked(mode == "wifi")
        self.host_edit.setText(device.get("host", "10.11.99.1"))
        device_type = device.get("type", "reMarkable Paper Pro")
        idx = self.device_type_combo.findText(device_type)
        if idx != -1:
            self.device_type_combo.setCurrentIndex(idx)
        password = self._load_password(device["name"])
        self.password_edit.setText(password)
        if keyring:
            self.remember_checkbox.blockSignals(True)
            self.remember_checkbox.setChecked(bool(password))
            self.remember_checkbox.blockSignals(False)
        self.config["active_device"] = device["name"]
        save_config(self.config)
        self._emit_device_preview()

    def _add_device(self):
        name, ok = QtWidgets.QInputDialog.getText(self, APP_NAME, "输入新设备名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        if any(device["name"] == name for device in self.config.get("devices", [])):
            QtWidgets.QMessageBox.warning(self, APP_NAME, "已存在同名设备。")
            return
        new_device = {
            "name": name,
            "mode": "usb",
            "host": "10.11.99.1",
            "type": "reMarkable Paper Pro",
        }
        self.config.setdefault("devices", []).append(new_device)
        save_config(self.config)
        self._populate_devices()
        idx = self.device_combo.findText(name)
        if idx != -1:
            self.device_combo.setCurrentIndex(idx)

    def _remove_device(self):
        if self.device_combo.count() <= 1:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "至少保留一个设备配置。")
            return
        name = self.device_combo.currentText()
        confirm = QtWidgets.QMessageBox.question(
            self,
            APP_NAME,
            f"确定删除设备“{name}”的配置？",
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        self.config["devices"] = [d for d in self.config["devices"] if d["name"] != name]
        if keyring:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:  # pragma: no cover - backend dependent
                pass
        self.config["active_device"] = self.config["devices"][0]["name"]
        save_config(self.config)
        self._populate_devices()

    def _save_device(self):
        device = self._current_device()
        if not device:
            return
        device["mode"] = "usb" if self.usb_radio.isChecked() else "wifi"
        device["host"] = self.host_edit.text().strip()
        device["type"] = self.device_type_combo.currentText()
        save_config(self.config)
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "设备配置已保存")
        self._emit_device_preview()

    # Credential helpers --------------------------------------------------------
    def _load_password(self, device_name: str) -> str:
        if not keyring:
            return ""
        try:
            stored = keyring.get_password(KEYRING_SERVICE, device_name)
            return stored or ""
        except Exception:  # pragma: no cover - backend specific
            logging.exception("Failed to load password from keyring")
            return ""

    def _store_password(self, device_name: str, password: str):
        if not keyring:
            return
        try:
            keyring.set_password(KEYRING_SERVICE, device_name, password)
        except Exception:  # pragma: no cover - backend specific
            logging.exception("Failed to store password in keyring")
            QtWidgets.QMessageBox.warning(
                self,
                APP_NAME,
                "无法保存密码到系统凭证管理器，请检查 keyring 配置。",
            )

    def _teardown_connection_progress(self):
        if self._connection_progress:
            self._connection_progress.close()
            self._connection_progress.deleteLater()
            self._connection_progress = None
        self._active_connection_worker = None
        if not self.ssh_client.is_connected():
            self.connect_button.setEnabled(True)

    def _connect(self):
        if self._active_connection_worker is not None:
            return
        host = self.host_edit.text().strip()
        password = self.password_edit.text().strip()
        if not host or not password:
            QtWidgets.QMessageBox.warning(
                self,
                APP_NAME,
                "请填写完整的连接信息（包括 root 密码）。",
            )
            return

        remember_password = self.remember_checkbox.isChecked()

        self.connect_button.setEnabled(False)
        progress = QtWidgets.QProgressDialog("正在连接到设备…", "", 0, 0, self)
        progress.setWindowTitle(APP_NAME)
        progress.setWindowModality(QtCore.Qt.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setRange(0, 0)
        progress.show()
        self._connection_progress = progress

        worker = Worker(self.ssh_client.connect, host, password)

        def on_finished(_: object):
            self._teardown_connection_progress()
            device = self._current_device()
            if device:
                device["mode"] = "usb" if self.usb_radio.isChecked() else "wifi"
                device["host"] = host
                save_config(self.config)
                if remember_password:
                    self._store_password(device["name"], password)

        def on_error(exc: Exception):
            self._teardown_connection_progress()
            logging.error("Unable to connect: %s", exc)
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"连接失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._active_connection_worker = worker
        self.thread_pool.start(worker)

    def _disconnect(self):
        self.ssh_client.close()

    def _on_connection_changed(self, connected: bool):
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.status_label.setText("🟢 已连接" if connected else "🔴 未连接")
        if connected:
            self.connected.emit()
        else:
            self.disconnected.emit()

    def _emit_device_preview(self):
        device = self._current_device().copy()
        if not device:
            return
        device["mode"] = "usb" if self.usb_radio.isChecked() else "wifi"
        device["host"] = self.host_edit.text().strip()
        device["type"] = self.device_type_combo.currentText()
        self.device_changed.emit(device)


class FontTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.config = config

        self.font_path_label = QtWidgets.QLabel("未选择文件")
        self.rename_checkbox = QtWidgets.QCheckBox(f"上传时重命名为 {DEFAULT_FONT_NAME}")
        self.rename_checkbox.setChecked(True)
        self.upload_button = QtWidgets.QPushButton("选择并上传字体")
        self.upload_button.clicked.connect(self._select_and_upload)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.font_path_label)
        layout.addWidget(self.rename_checkbox)
        layout.addWidget(self.upload_button)
        layout.addStretch()
        self.setLayout(layout)

    def _select_and_upload(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择字体文件", "", "字体文件 (*.ttf *.otf)")
        if not file_path:
            return
        self.font_path_label.setText(file_path)
        new_name = DEFAULT_FONT_NAME if self.rename_checkbox.isChecked() else os.path.basename(file_path)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                temp_font_path = os.path.join(tmpdir, new_name)
                with open(file_path, "rb") as src, open(temp_font_path, "wb") as dst:
                    dst.write(src.read())
                self._upload_font(temp_font_path, new_name)
            QtWidgets.QMessageBox.information(
                self,
                APP_NAME,
                "字体上传完成，并已刷新字体缓存。\n字体将在设备重启后生效。",
            )
            confirm = QtWidgets.QMessageBox.question(
                self,
                APP_NAME,
                "是否立即重启设备以应用新字体？",
            )
            if confirm == QtWidgets.QMessageBox.Yes:
                try:
                    stdout, stderr = self.ssh_client.exec_command("reboot")
                    if stderr.strip():
                        raise RuntimeError(stderr.strip())
                    QtWidgets.QMessageBox.information(self, APP_NAME, "已发送重启命令。")
                except Exception as exc:
                    logging.exception("Reboot after font upload failed")
                    QtWidgets.QMessageBox.critical(self, APP_NAME, f"重启失败：{exc}")
        except Exception as exc:
            logging.exception("Font upload failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"字体上传失败：{exc}")

    def _upload_font(self, local_path: str, new_name: str):
        font_dir = self.config.get("paths", {}).get("font", DEFAULT_FONT_DIR)
        remount_error: Optional[Exception] = None
        try:
            commands = [
                "mount -o remount,rw /",
                f"mkdir -p {font_dir}",
            ]
            for cmd in commands:
                stdout, stderr = self.ssh_client.exec_command(cmd)
                if stderr.strip():
                    raise RuntimeError(stderr.strip())

            remote_path = posixpath.join(font_dir, new_name)
            self.ssh_client.transfer_file(local_path, remote_path)

            stdout, stderr = self.ssh_client.exec_command(f"fc-cache -f -v {font_dir}")
            logging.info("fc-cache output: %s", stdout.strip())
            if stderr.strip():
                raise RuntimeError(stderr.strip())
        finally:
            try:
                stdout, stderr = self.ssh_client.exec_command("mount -o remount,ro /")
                if stderr.strip():
                    remount_error = RuntimeError(stderr.strip())
            except Exception as exc:
                logging.exception("Failed to remount root as read-only after font upload")
                if remount_error is None:
                    remount_error = exc

        if remount_error is not None:
            raise remount_error


class WallpaperTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.config = config
        self.image_path: Optional[str] = None
        self.current_resolution: Tuple[int, int] = DEVICE_PROFILES["reMarkable Paper Pro"]

        self.base_resolution = DEVICE_PROFILES["reMarkable Paper Pro"]
        self.orientation_combo = QtWidgets.QComboBox()
        self.orientation_combo.addItem("竖屏", "portrait")
        self.orientation_combo.addItem("横屏", "landscape")
        self.current_resolution = self._calculate_resolution(self.orientation_combo.currentData())

        self.preview_label = PreviewImageLabel("请选择图片以生成预览")
        self.preview_label.setMinimumSize(360, 480)
        self.preview_label.setStyleSheet(
            "QLabel { background-color: rgba(255, 255, 255, 30); border-radius: 6px; }"
        )

        self.info_label = QtWidgets.QLabel("未选择图片")
        self.resolution_label = QtWidgets.QLabel(self._resolution_text())
        self.choose_button = QtWidgets.QPushButton("选择图片")
        self.upload_button = QtWidgets.QPushButton("上传为壁纸")
        self.upload_button.setEnabled(False)

        self.variant_group = QtWidgets.QButtonGroup(self)
        self.variant_previews: Dict[str, PreviewImageLabel] = {}
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

        variants_group = QtWidgets.QGroupBox("当前设备壁纸")
        variants_layout = QtWidgets.QGridLayout()
        variants_layout.setContentsMargins(6, 6, 6, 6)
        for index, (variant_key, display_name, remote_path) in enumerate(WALLPAPER_VARIANTS):
            preview = PreviewImageLabel("未连接")
            preview.setMinimumSize(150, 200)
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
            container_layout.setContentsMargins(4, 4, 4, 4)
            container_layout.addWidget(preview)
            container_layout.addWidget(radio, alignment=QtCore.Qt.AlignHCenter)

            row = index // 2
            column = index % 2
            variants_layout.addWidget(container, row, column)

        variants_group.setLayout(variants_layout)

        orientation_row = QtWidgets.QHBoxLayout()
        orientation_row.addWidget(QtWidgets.QLabel("壁纸方向"))
        orientation_row.addWidget(self.orientation_combo)
        orientation_row.addStretch()

        self.target_label = QtWidgets.QLabel()

        control_container = QtWidgets.QWidget()
        control_layout = QtWidgets.QVBoxLayout(control_container)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.addWidget(variants_group)
        control_layout.addLayout(orientation_row)
        control_layout.addWidget(self.resolution_label)
        control_layout.addWidget(self.target_label)
        control_layout.addWidget(self.info_label)
        control_layout.addWidget(QtWidgets.QLabel("处理模式"))
        control_layout.addWidget(self.mode_combo)
        control_layout.addLayout(offset_layout)
        control_layout.addWidget(self.choose_button)
        control_layout.addWidget(self.upload_button)
        control_layout.addStretch()

        preview_container = QtWidgets.QWidget()
        preview_layout = QtWidgets.QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(self.preview_label, alignment=QtCore.Qt.AlignCenter)
        preview_layout.addStretch()

        main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        main_splitter.addWidget(control_container)
        main_splitter.addWidget(preview_container)
        main_splitter.setStretchFactor(0, 3)
        main_splitter.setStretchFactor(1, 4)
        main_splitter.setChildrenCollapsible(False)

        layout = QtWidgets.QHBoxLayout()
        layout.addWidget(main_splitter)
        self.setLayout(layout)

        self.choose_button.clicked.connect(self._select_image)
        self.upload_button.clicked.connect(self._upload_wallpaper)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.orientation_combo.currentIndexChanged.connect(self._on_orientation_changed)
        self.offset_x_slider.valueChanged.connect(self._render_preview)
        self.offset_y_slider.valueChanged.connect(self._render_preview)
        self.variant_group.buttonClicked.connect(self._on_variant_selected)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)

        self._select_variant_by_path(
            self.config.get("paths", {}).get(
                "wallpaper", "/usr/share/remarkable/suspended.png"
            )
        )
        self._update_target_label()

    def update_device(self, device: Dict):
        profile = device.get("type") if device else None
        self.base_resolution = DEVICE_PROFILES.get(
            profile, DEVICE_PROFILES["reMarkable Paper Pro"]
        )
        self._update_resolution()
        self._select_variant_by_path(
            self.config.get("paths", {}).get(
                "wallpaper", "/usr/share/remarkable/suspended.png"
            )
        )
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
        if connected:
            self._refresh_variant_previews()
        else:
            for preview in self.variant_previews.values():
                preview.clear_preview()
                preview.setText("未连接")

    def _refresh_variant_previews(self) -> None:
        if not self.ssh_client.is_connected():
            for preview in self.variant_previews.values():
                preview.clear_preview()
                preview.setText("未连接")
            return

        for variant_key, _display_name, remote_path in WALLPAPER_VARIANTS:
            preview = self.variant_previews[variant_key]
            try:
                pixmap = self._download_wallpaper_preview(remote_path)
            except Exception as exc:  # pragma: no cover - UI feedback
                logging.exception("Unable to load wallpaper preview: %s", remote_path)
                preview.clear_preview()
                preview.setText(f"加载失败\n{exc}")
                continue

            if pixmap and not pixmap.isNull():
                preview.setPixmap(pixmap)
            else:
                preview.clear_preview()
                preview.setText("无预览")

    def _download_wallpaper_preview(self, remote_path: str) -> Optional[QtGui.QPixmap]:
        with self.ssh_client.open_remote(remote_path, "rb") as remote_file:
            data = remote_file.read()

        pixmap = QtGui.QPixmap()
        if pixmap.loadFromData(data):
            return pixmap

        # Fallback through Pillow for uncommon formats
        with Image.open(BytesIO(data)) as image:
            buffer = BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
        if pixmap.loadFromData(buffer.getvalue(), "PNG"):
            return pixmap
        return None

    def _on_variant_selected(self, button: QtWidgets.QAbstractButton) -> None:
        remote_path = button.property("remote_path")
        if not remote_path:
            return
        self.config.setdefault("paths", {})["wallpaper"] = remote_path
        self._update_target_label()

    def _select_variant_by_path(self, remote_path: str) -> None:
        normalized = posixpath.normpath(remote_path)
        matched = False
        for variant_key, _display_name, candidate_path in WALLPAPER_VARIANTS:
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
        normalized = posixpath.normpath(remote_path)
        for _variant_key, display_name, candidate_path in WALLPAPER_VARIANTS:
            if posixpath.normpath(candidate_path) == normalized:
                return display_name
        return None

    def _update_target_label(self) -> None:
        remote_path = self.config.get("paths", {}).get(
            "wallpaper", "/usr/share/remarkable/suspended.png"
        )
        variant_label = self._variant_label_for_path(remote_path)
        if variant_label:
            self.target_label.setText(f"目标壁纸：{variant_label} ({remote_path})")
        else:
            self.target_label.setText(f"目标壁纸：{remote_path}")

    def _select_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp)")
        if not path:
            return
        self.image_path = path
        self.info_label.setText(f"选择的图片：{path}")
        self.upload_button.setEnabled(True)
        self._render_preview()

    def _on_mode_changed(self):
        crop_mode = self.mode_combo.currentData() == "crop"
        self.offset_x_slider.setEnabled(crop_mode)
        self.offset_y_slider.setEnabled(crop_mode)
        self._render_preview()

    def _on_orientation_changed(self):
        self._update_resolution()
        self._render_preview()

    def _render_preview(self):
        if not self.image_path:
            self.preview_label.clear_preview()
            self.preview_label.setText("请选择图片以生成预览")
            return
        try:
            processed = self._process_image(self.image_path)
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

    def _process_image(self, source_path: str) -> Image.Image:
        with Image.open(source_path) as img:
            img = img.convert("RGB")
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

            # crop mode
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

    def _upload_wallpaper(self):
        if not self.image_path:
            return
        try:
            processed_image = self._process_image(self.image_path)
            fd, temp_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            processed_image.save(temp_path, format="PNG")
            wallpaper_path = self.config.get("paths", {}).get("wallpaper", "/usr/share/remarkable/suspended.png")
            commands = [
                "mount -o remount,rw /",
                f"cp {wallpaper_path} {wallpaper_path}.backup",
            ]
            for cmd in commands:
                stdout, stderr = self.ssh_client.exec_command(cmd)
                if stderr:
                    raise RuntimeError(stderr.strip())

            self.ssh_client.transfer_file(temp_path, wallpaper_path)
            stdout, stderr = self.ssh_client.exec_command("mount -o remount,ro /")
            if stderr:
                raise RuntimeError(stderr.strip())

            if self.ssh_client.is_connected():
                self._refresh_variant_previews()
            self._render_preview()
            QtWidgets.QMessageBox.information(self, APP_NAME, "壁纸上传完成。")
        except Exception as exc:
            logging.exception("Wallpaper upload failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"上传壁纸失败：{exc}")
        finally:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.remove(temp_path)


class TimeTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)

        self.sync_button = QtWidgets.QPushButton("使用本地时间同步")
        self.info_button = QtWidgets.QPushButton("查看当前时间信息")
        self.tz_button = QtWidgets.QPushButton("设置为东八区")

        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addWidget(self.sync_button)
        button_layout.addWidget(self.info_button)
        button_layout.addWidget(self.tz_button)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(button_layout)
        layout.addWidget(self.output)
        self.setLayout(layout)

        self.sync_button.clicked.connect(self._sync_time)
        self.info_button.clicked.connect(self._show_time_info)
        self.tz_button.clicked.connect(self._set_timezone)

    def _append_output(self, text: str):
        self.output.appendPlainText(text)
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def _sync_time(self):
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            stdout, stderr = self.ssh_client.exec_command("mount -o remount,rw /")
            if stderr:
                raise RuntimeError(stderr.strip())
            stdout, stderr = self.ssh_client.exec_command(f"date -s \"{now}\"")
            if stderr:
                raise RuntimeError(stderr.strip())
            stdout, stderr = self.ssh_client.exec_command("hwclock -w")
            if stderr:
                raise RuntimeError(stderr.strip())
            stdout, stderr = self.ssh_client.exec_command("mount -o remount,ro /")
            if stderr:
                raise RuntimeError(stderr.strip())
            self._append_output(f"已同步设备时间到 {now}")
        except Exception as exc:
            logging.exception("Sync time failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"同步失败：{exc}")

    def _show_time_info(self):
        try:
            commands = {
                "系统时间": "date",
                "硬件时钟": "hwclock -r",
                "时区信息": "timedatectl",
            }
            for title, cmd in commands.items():
                stdout, stderr = self.ssh_client.exec_command(cmd)
                if stderr:
                    raise RuntimeError(stderr.strip())
                self._append_output(f"[{title}]\n{stdout.strip()}\n")
        except Exception as exc:
            logging.exception("Get time info failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"查询失败：{exc}")

    def _set_timezone(self):
        try:
            stdout, stderr = self.ssh_client.exec_command("mount -o remount,rw /")
            if stderr:
                raise RuntimeError(stderr.strip())
            stdout, stderr = self.ssh_client.exec_command("timedatectl set-timezone Asia/Shanghai")
            if stderr:
                raise RuntimeError(stderr.strip())
            stdout, stderr = self.ssh_client.exec_command("mount -o remount,ro /")
            if stderr:
                raise RuntimeError(stderr.strip())
            self._append_output("已将时区设置为 Asia/Shanghai")
        except Exception as exc:
            logging.exception("Set timezone failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"设置失败：{exc}")


class ControlTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client

        self.restart_button = QtWidgets.QPushButton("重启设备")
        self.enable_wifi_ssh_button = QtWidgets.QPushButton("开启 Wi-Fi SSH 通道")
        self.brightness_button = QtWidgets.QPushButton("提升前光亮度")

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.restart_button)
        layout.addWidget(self.enable_wifi_ssh_button)
        layout.addWidget(self.brightness_button)
        layout.addStretch()
        self.setLayout(layout)

        self.restart_button.clicked.connect(self._restart_device)
        self.enable_wifi_ssh_button.clicked.connect(self._enable_wifi_ssh)
        self.brightness_button.clicked.connect(self._increase_brightness)

    def _restart_device(self):
        confirm = QtWidgets.QMessageBox.question(self, APP_NAME, "确定要重启设备吗？这将断开连接。")
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        try:
            stdout, stderr = self.ssh_client.exec_command("reboot")
            if stderr:
                raise RuntimeError(stderr.strip())
            QtWidgets.QMessageBox.information(self, APP_NAME, "已发送重启命令。")
        except Exception as exc:
            logging.exception("Restart failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"重启失败：{exc}")

    def _enable_wifi_ssh(self):
        try:
            stdout, stderr = self.ssh_client.exec_command("rm-ssh-over-wlan on")
            if stderr:
                raise RuntimeError(stderr.strip())
            QtWidgets.QMessageBox.information(
                self,
                APP_NAME,
                "已开启 Wi-Fi SSH，请在断开 USB 后使用 WLAN 地址连接。",
            )
        except Exception as exc:
            logging.exception("Enable Wi-Fi SSH failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"操作失败：{exc}")

    def _increase_brightness(self):
        try:
            commands = [
                "mount -o remount,rw /",
                "cat /sys/class/backlight/rm_frontlight/max_brightness > /sys/class/backlight/rm_frontlight/brightness",
                "echo yes > /sys/class/backlight/rm_frontlight/linear_mapping",
                "umount -l /etc",
                "mount -o remount,rw /",
            ]
            for cmd in commands:
                stdout, stderr = self.ssh_client.exec_command(cmd)
                if stderr:
                    raise RuntimeError(stderr.strip())

            service_content = """
[Unit]
Description=Set frontlight linear mapping
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo yes > /sys/class/backlight/rm_frontlight/linear_mapping'
ExecStartPost=/bin/sh -c 'cat /sys/class/backlight/rm_frontlight/max_brightness > /sys/class/backlight/rm_frontlight/brightness'

[Install]
WantedBy=multi-user.target
""".strip()
            cmd = (
                "tee /etc/systemd/system/tweak-brightness-slider.service > /dev/null <<'EOF'\n"
                f"{service_content}\nEOF"
            )
            stdout, stderr = self.ssh_client.exec_command(cmd)
            if stderr:
                raise RuntimeError(stderr.strip())
            for cmd in ("systemctl daemon-reload", "systemctl enable --now tweak-brightness-slider.service", "mount -o remount,ro /"):
                stdout, stderr = self.ssh_client.exec_command(cmd)
                if stderr:
                    raise RuntimeError(stderr.strip())
            QtWidgets.QMessageBox.information(self, APP_NAME, "前光亮度已调整。")
        except Exception as exc:
            logging.exception("Brightness tweak failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"设置失败：{exc}")


class DocumentsTab(QtWidgets.QWidget):
    summary_changed = QtCore.pyqtSignal(dict)
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.documents: List[DocumentItem] = []
        self._current_preview_request: Optional[str] = None
        self._last_preview_bytes: Optional[bytes] = None
        self._active_progress: Optional[QtWidgets.QProgressDialog] = None
        self._progress_label_base: str = ""

        self.refresh_button = QtWidgets.QPushButton("刷新列表")
        self.upload_button = QtWidgets.QPushButton("上传文档")
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview_image = PreviewImageLabel("暂无预览")
        self.preview_image.setWordWrap(True)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "更新时间"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

        top_layout = QtWidgets.QHBoxLayout()
        top_layout.addWidget(self.refresh_button)
        top_layout.addWidget(self.upload_button)
        top_layout.addStretch()

        preview_tabs = QtWidgets.QTabWidget()
        preview_tabs.setDocumentMode(True)
        preview_tabs.addTab(self.preview, "元数据")
        image_container = QtWidgets.QWidget()
        image_layout = QtWidgets.QVBoxLayout(image_container)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.addWidget(self.preview_image)
        preview_tabs.addTab(image_container, "图像预览")

        preview_container = QtWidgets.QWidget()
        preview_layout = QtWidgets.QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(preview_tabs)

        left_container = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(top_layout)
        left_layout.addWidget(self.table)

        content_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        content_splitter.addWidget(left_container)
        content_splitter.addWidget(preview_container)
        content_splitter.setStretchFactor(0, 3)
        content_splitter.setStretchFactor(1, 2)
        content_splitter.setChildrenCollapsible(False)

        layout = QtWidgets.QHBoxLayout()
        layout.addWidget(content_splitter)
        self.setLayout(layout)

        self.refresh_button.clicked.connect(self.refresh)
        self.upload_button.clicked.connect(self.upload_document)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)

    def _show_progress_dialog(self, title: str, text: str) -> None:
        if self._active_progress:
            self._active_progress.close()
            self._active_progress.deleteLater()
        dialog = QtWidgets.QProgressDialog(text, None, 0, 0, self)
        dialog.setWindowTitle(title)
        dialog.setCancelButton(None)
        dialog.setWindowModality(QtCore.Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.show()
        self._active_progress = dialog
        self._progress_label_base = text

    def _update_progress_dialog(self, current: int, total: int) -> None:
        dialog = self._active_progress
        if not dialog:
            return
        if total <= 0:
            dialog.setRange(0, 0)
            dialog.setLabelText(self._progress_label_base)
            return
        dialog.setRange(0, 1000)
        ratio = max(0.0, min(float(current) / float(total), 1.0))
        dialog.setValue(int(ratio * 1000))
        dialog.setLabelText(f"{self._progress_label_base}\n已完成 {ratio * 100:.1f}%")

    def _close_progress_dialog(self) -> None:
        if self._active_progress:
            self._active_progress.close()
            self._active_progress.deleteLater()
            self._active_progress = None
            self._progress_label_base = ""

    @staticmethod
    def _safe_stat_size(sftp: paramiko.SFTPClient, remote_path: str) -> int:
        try:
            return int(sftp.stat(remote_path).st_size)
        except IOError:
            return 0

    def _collect_remote_files(
        self,
        sftp: paramiko.SFTPClient,
        remote_dir: str,
        base_dir: Optional[str] = None,
    ) -> List[Tuple[str, str, int]]:
        if base_dir is None:
            base_dir = remote_dir
        files: List[Tuple[str, str, int]] = []
        try:
            entries = sftp.listdir_attr(remote_dir)
        except IOError:
            return files
        for entry in entries:
            remote_path = f"{remote_dir}/{entry.filename}"
            if stat.S_ISDIR(entry.st_mode):
                files.extend(self._collect_remote_files(sftp, remote_path, base_dir))
            else:
                relative = remote_path[len(base_dir) :].lstrip("/")
                files.append((remote_path, relative, int(entry.st_size)))
        return files

    def refresh(self):
        worker = Worker(self._load_documents)
        worker.signals.finished.connect(self._on_documents_loaded)
        worker.signals.error.connect(self._on_error)
        self.thread_pool.start(worker)

    def _load_documents(self) -> List[DocumentItem]:
        items: List[DocumentItem] = []
        try:
            entries = self.ssh_client.listdir_attr(DOCUMENT_ROOT)
        except IOError:
            return items

        filenames = {e.filename for e in entries}
        metadata_files = [e for e in entries if e.filename.endswith(".metadata")]
        for entry in metadata_files:
            identifier = entry.filename[:-9]
            metadata_path = f"{DOCUMENT_ROOT}/{entry.filename}"
            try:
                with self.ssh_client.open_remote(metadata_path, "r") as fh:
                    metadata = json.load(fh)
            except Exception:
                metadata = {}
            visible_name = metadata.get("visibleName", identifier)
            doc_type = metadata.get("type", "document")
            available_assets = []
            for ext in ("pdf", "epub", "zip", "note", "rm"):
                remote_name = f"{identifier}.{ext}"
                if remote_name in filenames:
                    available_assets.append(ext)
            updated = None
            if entry.st_mtime:
                updated = datetime.fromtimestamp(entry.st_mtime)
            items.append(DocumentItem(identifier, visible_name, doc_type, updated, available_assets))
        items.sort(key=lambda item: item.updated or datetime.min, reverse=True)
        return items

    def _on_documents_loaded(self, documents: List[DocumentItem]):
        self.documents = documents
        self.table.setRowCount(len(documents))
        for row, item in enumerate(documents):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(item.name))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(item.doc_type))
            updated_text = item.updated.strftime("%Y-%m-%d %H:%M") if item.updated else ""
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(updated_text))
        self.preview.clear()
        self.preview_image.clear_preview()
        self.preview_image.setText("暂无预览")
        self.summary_changed.emit(self._build_summary())

    def _on_error(self, exc: Exception):
        QtWidgets.QMessageBox.critical(self, APP_NAME, f"操作失败：{exc}")

    def _on_selection_changed(self):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            self.preview.clear()
            self.preview_image.clear_preview()
            self.preview_image.setText("暂无预览")
            return
        item = self.documents[indexes[0].row()]
        meta_text = [
            f"ID: {item.identifier}",
            f"名称: {item.name}",
            f"类型: {item.doc_type}",
            f"更新时间: {item.updated.strftime('%Y-%m-%d %H:%M:%S') if item.updated else '未知'}",
            f"可用资源: {', '.join(item.available_assets) if item.available_assets else '无'}",
        ]
        self.preview.setPlainText("\n".join(meta_text))
        self.preview_image.setText("加载预览中...")
        self.preview_image.clear_preview()
        self._current_preview_request = item.identifier
        worker = Worker(self._fetch_preview_bytes, item)
        worker.signals.finished.connect(partial(self._on_preview_loaded, item.identifier))
        worker.signals.error.connect(self._on_error)
        self.thread_pool.start(worker)

    def upload_document(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择要上传的文档",
            "",
            "文档文件 (*.pdf *.epub)",
        )
        if not file_path:
            return
        worker = Worker(self._transfer_document, file_path)
        worker.kwargs["progress_callback"] = worker.signals.progress.emit

        def on_finished(_result):
            self._close_progress_dialog()
            QtWidgets.QMessageBox.information(self, APP_NAME, "上传完成，已刷新文档列表。")
            self.refresh()

        def on_error(exc: Exception):
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.progress.connect(self._update_progress_dialog)
        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("上传进度", "正在上传文档...")
        self.thread_pool.start(worker)

    def _fetch_preview_bytes(self, item: DocumentItem) -> Optional[bytes]:
        thumbnail_dir = f"{DOCUMENT_ROOT}/{item.identifier}.thumbnails"
        candidates = [
            f"{thumbnail_dir}/{item.identifier}.png",
            f"{thumbnail_dir}/{item.identifier}.thumbnail",
        ]
        for candidate in candidates:
            try:
                with self.ssh_client.open_remote(candidate, "rb") as fh:
                    data = fh.read()
                    if data:
                        return data
            except IOError:
                continue

        # Look for any available thumbnail in the thumbnails directory
        try:
            entries = self.ssh_client.listdir_attr(thumbnail_dir)
        except IOError:
            entries = []

        for entry in sorted(entries, key=lambda e: e.filename):
            name_lower = entry.filename.lower()
            if not name_lower.endswith((".png", ".jpg", ".jpeg", ".thumbnail")):
                continue
            remote_path = f"{thumbnail_dir}/{entry.filename}"
            try:
                with self.ssh_client.open_remote(remote_path, "rb") as fh:
                    data = fh.read()
                    if data:
                        return data
            except IOError:
                continue
        return None

    def _on_preview_loaded(self, identifier: str, data: Optional[bytes]):
        if identifier != self._current_preview_request:
            return
        if not data:
            self.preview_image.setText("暂无可用预览")
            self.preview_image.clear_preview()
            self._last_preview_bytes = None
            return
        image = QtGui.QImage.fromData(data)
        if image.isNull():
            self.preview_image.setText("无法解析预览图像")
            self.preview_image.clear_preview()
            self._last_preview_bytes = None
            return
        pixmap = QtGui.QPixmap.fromImage(image)
        self.preview_image.setPixmap(pixmap)
        self.preview_image.setText("")
        self._last_preview_bytes = data

    def _transfer_document(
        self, file_path: str, progress_callback: Optional[Callable[[int, int], None]] = None
    ):
        extension = os.path.splitext(file_path)[1][1:].lower()
        if extension not in {"pdf", "epub"}:
            raise RuntimeError("仅支持上传 PDF 或 EPUB 文件")

        tmpdir = tempfile.mkdtemp()
        uuid_value = str(uuid.uuid4()).lower()
        try:
            shutil.copy(file_path, os.path.join(tmpdir, f"{uuid_value}.{extension}"))

            metadata = {
                "deleted": False,
                "lastModified": f"{int(datetime.now().timestamp())}000",
                "metadatamodified": False,
                "modified": False,
                "parent": "",
                "pinned": False,
                "synced": False,
                "type": "DocumentType",
                "version": 1,
                "visibleName": os.path.splitext(os.path.basename(file_path))[0],
            }
            with open(os.path.join(tmpdir, f"{uuid_value}.metadata"), "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=4, ensure_ascii=False)

            content: Dict[str, object]
            if extension == "pdf":
                content = {
                    "extraMetadata": {},
                    "fileType": "pdf",
                    "fontName": "",
                    "lastOpenedPage": 0,
                    "lineHeight": -1,
                    "margins": 100,
                    "pageCount": 1,
                    "textScale": 1,
                    "transform": {
                        "m11": 1,
                        "m12": 1,
                        "m13": 1,
                        "m21": 1,
                        "m22": 1,
                        "m23": 1,
                        "m31": 1,
                        "m32": 1,
                        "m33": 1,
                    },
                }
                with open(os.path.join(tmpdir, f"{uuid_value}.content"), "w", encoding="utf-8") as f:
                    json.dump(content, f, indent=4)
                os.makedirs(os.path.join(tmpdir, f"{uuid_value}.cache"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, f"{uuid_value}.highlights"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, f"{uuid_value}.thumbnails"), exist_ok=True)
            else:  # epub
                content = {"fileType": "epub"}
                with open(os.path.join(tmpdir, f"{uuid_value}.content"), "w", encoding="utf-8") as f:
                    json.dump(content, f, indent=4)

            with self.ssh_client.sftp_session() as sftp:
                remote_dirs = set()
                files_to_upload: List[Tuple[str, str, int]] = []
                for root, _dirs, files in os.walk(tmpdir):
                    rel_dir = os.path.relpath(root, tmpdir)
                    if rel_dir == ".":
                        remote_dir = DOCUMENT_ROOT
                    else:
                        remote_dir = posixpath.join(
                            DOCUMENT_ROOT,
                            rel_dir.replace(os.sep, "/"),
                        )
                        remote_dirs.add(remote_dir)
                    for name in files:
                        local_path = os.path.join(root, name)
                        remote_path = posixpath.join(remote_dir, name)
                        file_size = os.path.getsize(local_path)
                        files_to_upload.append((local_path, remote_path, file_size))

                for remote_dir in sorted(remote_dirs, key=lambda value: value.count("/")):
                    try:
                        sftp.stat(remote_dir)
                    except IOError:
                        sftp.mkdir(remote_dir)

                total_size = sum(size for _local, _remote, size in files_to_upload)
                total_for_progress = total_size if total_size > 0 else 1
                if progress_callback:
                    progress_callback(0, total_for_progress)

                uploaded = 0
                for local_path, remote_path, size in files_to_upload:
                    offset = uploaded

                    def _put_callback(transferred, _total, offset=offset, size=size):
                        if progress_callback:
                            progress_callback(
                                offset + min(transferred, size), total_for_progress
                            )

                    sftp.put(local_path, remote_path, callback=_put_callback)
                    uploaded += size
                    if progress_callback:
                        progress_callback(uploaded, total_for_progress)

            if progress_callback:
                progress_callback(total_for_progress, total_for_progress)

            stdout, stderr = self.ssh_client.exec_command("systemctl restart xochitl")
            if stderr:
                raise RuntimeError(stderr.strip())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _build_summary(self) -> Dict[str, object]:
        total = len(self.documents)
        pdf_count = sum(1 for item in self.documents if "pdf" in item.available_assets)
        epub_count = sum(1 for item in self.documents if "epub" in item.available_assets)
        note_count = sum(
            1
            for item in self.documents
            if any(ext in item.available_assets for ext in ("note", "rm"))
        )
        last_updated = next(
            (item.updated for item in self.documents if item.updated is not None),
            None,
        )
        return {
            "total": total,
            "pdf": pdf_count,
            "epub": epub_count,
            "notes": note_count,
            "lastUpdated": last_updated.strftime("%Y-%m-%d %H:%M") if last_updated else "",
        }

    def current_summary(self) -> Dict[str, object]:
        return self._build_summary()


class DashboardTab(QtWidgets.QWidget):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.view = QtWebEngineWidgets.QWebEngineView(self)
        self.view.setContextMenuPolicy(QtCore.Qt.NoContextMenu)
        self._state: Dict[str, object] = {
            "connected": False,
            "lastConnectionChange": "",
            "device": {"name": "", "type": "", "mode": "", "host": ""},
            "documents": {
                "total": 0,
                "pdf": 0,
                "epub": 0,
                "notes": 0,
                "lastUpdated": "",
            },
        }
        self._loaded = False
        self._pending_script: Optional[str] = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        html_path = resource_path("web", "dashboard.html")
        self.view.setUrl(QtCore.QUrl.fromLocalFile(str(html_path)))
        self.view.loadFinished.connect(self._on_load_finished)

    def update_device(self, device: Dict):
        self._state["device"] = {
            "name": device.get("name", ""),
            "type": device.get("type", ""),
            "mode": device.get("mode", ""),
            "host": device.get("host", ""),
        }
        self._apply_state()

    def update_connection(self, connected: bool, device: Optional[Dict] = None):
        if device:
            self._state["device"] = {
                "name": device.get("name", ""),
                "type": device.get("type", ""),
                "mode": device.get("mode", ""),
                "host": device.get("host", ""),
            }
        self._state["connected"] = connected
        self._state["lastConnectionChange"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._apply_state()

    def update_documents(self, summary: Dict[str, object]):
        self._state["documents"].update(summary)
        self._apply_state()

    def _on_load_finished(self, ok: bool):
        self._loaded = ok
        if ok and self._pending_script:
            self.view.page().runJavaScript(self._pending_script)
            self._pending_script = None

    def _apply_state(self):
        script = f"window.updateDashboard({json.dumps(self._state, ensure_ascii=False)});"
        if self._loaded:
            self.view.page().runJavaScript(script)
        else:
            self._pending_script = script


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1480, 1200)
        self.resize(1860, 1640)

        self.config = load_config()
        self.ssh_client = SSHClientWrapper()

        self.connection_widget = ConnectionWidget(self.ssh_client, self.config)
        self.tabs = QtWidgets.QTabWidget()
        self.dashboard_tab = DashboardTab()
        self.font_tab = FontTab(self.ssh_client, self.config)
        self.wallpaper_tab = WallpaperTab(self.ssh_client, self.config)
        self.time_tab = TimeTab(self.ssh_client)
        self.control_tab = ControlTab(self.ssh_client)
        self.documents_tab = DocumentsTab(self.ssh_client)

        self.tabs.addTab(self.dashboard_tab, "仪表盘")
        self.tabs.addTab(self.font_tab, "字体管理")
        self.tabs.addTab(self.wallpaper_tab, "壁纸管理")
        self.tabs.addTab(self.time_tab, "时间设置")
        self.tabs.addTab(self.control_tab, "设备控制")
        self.tabs.addTab(self.documents_tab, "文档预览/上传")

        central_widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central_widget)
        layout.addWidget(self.connection_widget)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central_widget)

        self._update_tabs_enabled(False)
        self.connection_widget.connected.connect(lambda: self._update_tabs_enabled(True))
        self.connection_widget.connected.connect(self.documents_tab.refresh)
        self.connection_widget.disconnected.connect(lambda: self._update_tabs_enabled(False))
        self.connection_widget.device_changed.connect(self.wallpaper_tab.update_device)
        self.connection_widget.device_changed.connect(self._on_device_changed)
        self.connection_widget.device_changed.connect(self.dashboard_tab.update_device)
        self.connection_widget.connected.connect(self._on_connected)
        self.connection_widget.disconnected.connect(self._on_disconnected)
        self.documents_tab.summary_changed.connect(self.dashboard_tab.update_documents)

        # Initialize wallpaper profile preview
        initial_device = next(
            (d for d in self.config.get("devices", []) if d["name"] == self.config.get("active_device")),
            self.config.get("devices", [])[0],
        )
        self.wallpaper_tab.update_device(initial_device)
        self.dashboard_tab.update_device(initial_device)
        self.dashboard_tab.update_documents(self.documents_tab.current_summary())
        self.dashboard_tab.update_connection(False, initial_device)

    def _update_tabs_enabled(self, enabled: bool):
        for idx in range(self.tabs.count()):
            widget = self.tabs.widget(idx)
            if widget is self.dashboard_tab:
                continue
            widget.setEnabled(enabled)

    def _on_device_changed(self, device: Dict):
        if self.ssh_client.is_connected():
            self.documents_tab.refresh()

    def _on_connected(self):
        device = self.connection_widget.current_device()
        self.dashboard_tab.update_connection(True, device)

    def _on_disconnected(self):
        device = self.connection_widget.current_device()
        self.dashboard_tab.update_connection(False, device)


def main():
    app = QtWidgets.QApplication(sys.argv)
    QtWidgets.QApplication.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor(40, 44, 52))
    palette.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
    palette.setColor(QtGui.QPalette.Base, QtGui.QColor(30, 34, 42))
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(40, 44, 52))
    palette.setColor(QtGui.QPalette.Text, QtCore.Qt.white)
    palette.setColor(QtGui.QPalette.Button, QtGui.QColor(55, 60, 72))
    palette.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(111, 181, 255))
    palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
    app.setPalette(palette)
    default_font = app.font()
    default_font.setPointSize(11)
    app.setFont(default_font)
    app.setStyleSheet(
        """
        QWidget { font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif; font-size: 15px; }
        QGroupBox { border: 1px solid #3C3F4A; border-radius: 12px; margin-top: 18px; }
        QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 4px 10px; }
        QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6A8DFF, stop:1 #4DC3FF); color: white; border-radius: 10px; padding: 10px 18px; font-weight: 600; }
        QPushButton:disabled { background: #444a5a; color: #999; }
        QPushButton:hover:!disabled { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7FA0FF, stop:1 #62D0FF); }
        QToolButton { background-color: #3C4356; color: white; border-radius: 6px; padding: 6px; }
        QToolButton:hover { background-color: #4A5168; }
        QLineEdit, QComboBox, QPlainTextEdit, QTableWidget, QTextEdit { background-color: #2f333d; color: white; border: 1px solid #4c5266; border-radius: 8px; padding: 6px; }
        QTabWidget::pane { border: 1px solid #3C3F4A; border-radius: 12px; }
        QTabBar::tab { background: #2F3545; color: white; padding: 10px 22px; border-top-left-radius: 10px; border-top-right-radius: 10px; margin: 0 2px; }
        QTabBar::tab:selected { background: #5C6BC0; }
        QTabBar::tab:hover { background: #465272; }
        QHeaderView::section { background-color: #3c404d; color: white; padding: 8px; border: none; font-size: 14px; }
        QLabel { color: white; }
        #connectionStatusLabel { font-size: 20px; font-weight: 600; padding: 8px 0; }
        QSlider::groove:horizontal { height: 8px; background: #3C4356; border-radius: 4px; }
        QSlider::handle:horizontal { width: 18px; background: #5C6BC0; border-radius: 9px; margin: -5px 0; }
        """
    )

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
