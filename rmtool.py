import json
import logging
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import paramiko
from PIL import Image
from PyQt5 import QtCore, QtGui, QtWidgets


APP_NAME = "reMarkable 管理工具"
CONFIG_FILE = "config.json"
DEFAULT_FONT_NAME = "zwzt.ttf"
WALLPAPER_RESOLUTION = (1620, 2160)
DOCUMENT_ROOT = "/home/root/.local/share/remarkable/xochitl"


logging.basicConfig(
    filename="remarkable_tool.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def load_config() -> Dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {
        "connection": {
            "mode": "usb",
            "usb": {"host": "10.11.99.1", "password": ""},
            "wifi": {"host": "", "password": ""},
        },
        "paths": {
            "font": "/usr/share/fonts/ttf/noto/",
            "wallpaper": "/usr/share/remarkable/suspended.png",
        },
    }


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
        self._sftp: Optional[paramiko.SFTPClient] = None
        self.connection_info: Dict[str, str] = {}

    def connect(self, host: str, password: str) -> None:
        logging.info("Connecting to %s", host)
        self.close()
        self.connection_info = {"host": host, "password": password}
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, username="root", password=password)
        self._client = client
        self._sftp = client.open_sftp()
        self.connection_changed.emit(True)

    def close(self) -> None:
        if self._sftp:
            self._sftp.close()
            self._sftp = None
        if self._client:
            self._client.close()
            self._client = None
        self.connection_changed.emit(False)

    def ensure_client(self) -> paramiko.SSHClient:
        if not self._client:
            raise RuntimeError("未连接到设备")
        return self._client

    def ensure_sftp(self) -> paramiko.SFTPClient:
        if not self._sftp:
            raise RuntimeError("未连接到设备")
        return self._sftp

    def exec_command(self, command: str) -> Tuple[str, str]:
        client = self.ensure_client()
        logging.info("Executing command: %s", command)
        stdin, stdout, stderr = client.exec_command(command)
        return stdout.read().decode("utf-8"), stderr.read().decode("utf-8")

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        sftp = self.ensure_sftp()
        logging.info("Transferring %s -> %s", local_path, remote_path)
        sftp.put(local_path, remote_path)

    def file_exists(self, remote_path: str) -> bool:
        sftp = self.ensure_sftp()
        try:
            sftp.stat(remote_path)
            return True
        except IOError:
            return False

    def listdir_attr(self, remote_path: str):
        sftp = self.ensure_sftp()
        return sftp.listdir_attr(remote_path)

    def open_remote(self, remote_path: str, mode: str = "r"):
        sftp = self.ensure_sftp()
        return sftp.open(remote_path, mode)

    def download_file(self, remote_path: str, local_path: str) -> None:
        sftp = self.ensure_sftp()
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        sftp.get(remote_path, local_path)

    def download_directory(self, remote_dir: str, local_dir: str) -> None:
        sftp = self.ensure_sftp()
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        for entry in sftp.listdir_attr(remote_dir):
            remote_path = f"{remote_dir}/{entry.filename}"
            local_path = os.path.join(local_dir, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                self.download_directory(remote_path, local_path)
            else:
                sftp.get(remote_path, local_path)


class WorkerSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(Exception)


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


class ConnectionWidget(QtWidgets.QGroupBox):
    connected = QtCore.pyqtSignal()
    disconnected = QtCore.pyqtSignal()

    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__("连接设置", parent)
        self.ssh_client = ssh_client
        self.config = config

        self.usb_radio = QtWidgets.QRadioButton("USB")
        self.wifi_radio = QtWidgets.QRadioButton("WiFi")
        self.host_edit = QtWidgets.QLineEdit()
        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.connect_button = QtWidgets.QPushButton("连接")
        self.disconnect_button = QtWidgets.QPushButton("断开")
        self.status_label = QtWidgets.QLabel("未连接")

        layout = QtWidgets.QGridLayout()
        layout.addWidget(self.usb_radio, 0, 0)
        layout.addWidget(self.wifi_radio, 0, 1)
        layout.addWidget(QtWidgets.QLabel("地址"), 1, 0)
        layout.addWidget(self.host_edit, 1, 1)
        layout.addWidget(QtWidgets.QLabel("root 密码"), 2, 0)
        layout.addWidget(self.password_edit, 2, 1)
        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(self.disconnect_button)
        layout.addLayout(button_layout, 3, 0, 1, 2)
        layout.addWidget(self.status_label, 4, 0, 1, 2)
        self.setLayout(layout)

        self.disconnect_button.setEnabled(False)

        self.usb_radio.toggled.connect(self._on_mode_changed)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        ssh_client.connection_changed.connect(self._on_connection_changed)

        self._load_config()

    def _load_config(self):
        mode = self.config.get("connection", {}).get("mode", "usb")
        usb_info = self.config["connection"].get("usb", {})
        wifi_info = self.config["connection"].get("wifi", {})

        self.usb_radio.setChecked(mode == "usb")
        self.wifi_radio.setChecked(mode == "wifi")
        if mode == "usb":
            self.host_edit.setText(usb_info.get("host", "10.11.99.1"))
            self.password_edit.setText(usb_info.get("password", ""))
        else:
            self.host_edit.setText(wifi_info.get("host", ""))
            self.password_edit.setText(wifi_info.get("password", ""))

    def _on_mode_changed(self, checked: bool):
        if not checked:
            return
        if self.usb_radio.isChecked():
            info = self.config["connection"].get("usb", {})
            self.host_edit.setText(info.get("host", "10.11.99.1"))
            self.password_edit.setText(info.get("password", ""))
        else:
            info = self.config["connection"].get("wifi", {})
            self.host_edit.setText(info.get("host", ""))
            self.password_edit.setText(info.get("password", ""))

    def _connect(self):
        host = self.host_edit.text().strip()
        password = self.password_edit.text().strip()
        mode = "usb" if self.usb_radio.isChecked() else "wifi"
        if not host or not password:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "请填写完整的连接信息。")
            return

        try:
            self.ssh_client.connect(host, password)
        except Exception as exc:
            logging.exception("Unable to connect")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"连接失败：{exc}")
            return

        self.config["connection"]["mode"] = mode
        self.config["connection"][mode]["host"] = host
        self.config["connection"][mode]["password"] = password
        save_config(self.config)

    def _disconnect(self):
        self.ssh_client.close()

    def _on_connection_changed(self, connected: bool):
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.status_label.setText("已连接" if connected else "未连接")
        if connected:
            self.connected.emit()
        else:
            self.disconnected.emit()


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
            QtWidgets.QMessageBox.information(self, APP_NAME, "字体上传完成。")
        except Exception as exc:
            logging.exception("Font upload failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"字体上传失败：{exc}")

    def _upload_font(self, local_path: str, new_name: str):
        font_dir = self.config.get("paths", {}).get("font", "/usr/share/fonts/ttf/noto/")
        commands = [
            "mount -o remount,rw /",
            f"mkdir -p {font_dir}",
        ]
        for cmd in commands:
            stdout, stderr = self.ssh_client.exec_command(cmd)
            if stderr:
                raise RuntimeError(stderr.strip())

        remote_path = os.path.join(font_dir, new_name)
        self.ssh_client.transfer_file(local_path, remote_path)
        stdout, stderr = self.ssh_client.exec_command("mount -o remount,ro /")
        if stderr:
            raise RuntimeError(stderr.strip())


class WallpaperTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.config = config
        self.image_path: Optional[str] = None

        self.preview_label = QtWidgets.QLabel()
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self.preview_label.setMinimumSize(200, 260)
        self.preview_label.setFrameShape(QtWidgets.QFrame.Box)

        self.info_label = QtWidgets.QLabel("未选择图片")
        self.choose_button = QtWidgets.QPushButton("选择图片")
        self.upload_button = QtWidgets.QPushButton("上传为壁纸")
        self.upload_button.setEnabled(False)

        self.keep_ratio_checkbox = QtWidgets.QCheckBox("保持纵横比，必要时填充")
        self.keep_ratio_checkbox.setChecked(True)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.preview_label)
        layout.addWidget(self.info_label)
        layout.addWidget(self.keep_ratio_checkbox)
        layout.addWidget(self.choose_button)
        layout.addWidget(self.upload_button)
        layout.addStretch()
        self.setLayout(layout)

        self.choose_button.clicked.connect(self._select_image)
        self.upload_button.clicked.connect(self._upload_wallpaper)

    def _select_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp)")
        if not path:
            return
        self.image_path = path
        image = QtGui.QImage(path)
        pixmap = QtGui.QPixmap.fromImage(image)
        scaled = pixmap.scaled(
            self.preview_label.size() * 1.5,
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
        self.preview_label.setPixmap(scaled)
        self.info_label.setText(f"选择的图片：{path}")
        self.upload_button.setEnabled(True)

    def _process_image(self, source_path: str) -> str:
        with Image.open(source_path) as img:
            img = img.convert("RGB")
            target_w, target_h = WALLPAPER_RESOLUTION
            if self.keep_ratio_checkbox.isChecked():
                img.thumbnail(WALLPAPER_RESOLUTION, Image.LANCZOS)
                new_img = Image.new("RGB", WALLPAPER_RESOLUTION, color="white")
                offset = (
                    (target_w - img.size[0]) // 2,
                    (target_h - img.size[1]) // 2,
                )
                new_img.paste(img, offset)
                processed = new_img
            else:
                processed = img.resize(WALLPAPER_RESOLUTION, Image.LANCZOS)

            fd, temp_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            processed.save(temp_path, format="PNG")
            return temp_path

    def _upload_wallpaper(self):
        if not self.image_path:
            return
        try:
            processed_path = self._process_image(self.image_path)
            wallpaper_path = self.config.get("paths", {}).get("wallpaper", "/usr/share/remarkable/suspended.png")
            commands = [
                "mount -o remount,rw /",
                f"cp {wallpaper_path} {wallpaper_path}.backup",
            ]
            for cmd in commands:
                stdout, stderr = self.ssh_client.exec_command(cmd)
                if stderr:
                    raise RuntimeError(stderr.strip())

            self.ssh_client.transfer_file(processed_path, wallpaper_path)
            stdout, stderr = self.ssh_client.exec_command("mount -o remount,ro /")
            if stderr:
                raise RuntimeError(stderr.strip())

            QtWidgets.QMessageBox.information(self, APP_NAME, "壁纸上传完成。")
        except Exception as exc:
            logging.exception("Wallpaper upload failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"上传壁纸失败：{exc}")
        finally:
            if 'processed_path' in locals() and os.path.exists(processed_path):
                os.remove(processed_path)


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
        self.enable_ssh_button = QtWidgets.QPushButton("启用 SSH 服务")
        self.brightness_button = QtWidgets.QPushButton("提升前光亮度")

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.restart_button)
        layout.addWidget(self.enable_ssh_button)
        layout.addWidget(self.brightness_button)
        layout.addStretch()
        self.setLayout(layout)

        self.restart_button.clicked.connect(self._restart_device)
        self.enable_ssh_button.clicked.connect(self._enable_ssh)
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

    def _enable_ssh(self):
        try:
            stdout, stderr = self.ssh_client.exec_command("systemctl enable --now ssh")
            if stderr:
                raise RuntimeError(stderr.strip())
            QtWidgets.QMessageBox.information(self, APP_NAME, "SSH 服务已启用。")
        except Exception as exc:
            logging.exception("Enable SSH failed")
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"启用失败：{exc}")

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
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.documents: List[DocumentItem] = []

        self.refresh_button = QtWidgets.QPushButton("刷新列表")
        self.export_button = QtWidgets.QPushButton("导出所选")
        self.export_button.setEnabled(False)
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "更新时间"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

        top_layout = QtWidgets.QHBoxLayout()
        top_layout.addWidget(self.refresh_button)
        top_layout.addWidget(self.export_button)
        top_layout.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(top_layout)
        layout.addWidget(self.table)
        layout.addWidget(QtWidgets.QLabel("预览 / 元数据"))
        layout.addWidget(self.preview)
        self.setLayout(layout)

        self.refresh_button.clicked.connect(self.refresh)
        self.export_button.clicked.connect(self.export_selected)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)

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
        self.export_button.setEnabled(bool(documents))

    def _on_error(self, exc: Exception):
        QtWidgets.QMessageBox.critical(self, APP_NAME, f"操作失败：{exc}")

    def _on_selection_changed(self):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            self.preview.clear()
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

    def export_selected(self):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return
        item = self.documents[indexes[0].row()]
        target_dir = QtWidgets.QFileDialog.getExistingDirectory(self, "选择导出位置")
        if not target_dir:
            return
        worker = Worker(self._export_document, item, target_dir)
        worker.signals.finished.connect(lambda _: QtWidgets.QMessageBox.information(self, APP_NAME, "导出完成。"))
        worker.signals.error.connect(self._on_error)
        self.thread_pool.start(worker)

    def _export_document(self, item: DocumentItem, target_dir: str):
        base_name = f"{item.name}".replace("/", "_")
        if item.available_assets:
            for ext in item.available_assets:
                remote = f"{DOCUMENT_ROOT}/{item.identifier}.{ext}"
                local = os.path.join(target_dir, f"{base_name}.{ext}")
                try:
                    self.ssh_client.download_file(remote, local)
                except IOError:
                    continue
        else:
            remote_dir = f"{DOCUMENT_ROOT}/{item.identifier}"
            local_dir = os.path.join(target_dir, base_name)
            self.ssh_client.download_directory(remote_dir, local_dir)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(900, 700)

        self.config = load_config()
        self.ssh_client = SSHClientWrapper()

        self.connection_widget = ConnectionWidget(self.ssh_client, self.config)
        self.tabs = QtWidgets.QTabWidget()
        self.font_tab = FontTab(self.ssh_client, self.config)
        self.wallpaper_tab = WallpaperTab(self.ssh_client, self.config)
        self.time_tab = TimeTab(self.ssh_client)
        self.control_tab = ControlTab(self.ssh_client)
        self.documents_tab = DocumentsTab(self.ssh_client)

        self.tabs.addTab(self.font_tab, "字体管理")
        self.tabs.addTab(self.wallpaper_tab, "壁纸管理")
        self.tabs.addTab(self.time_tab, "时间设置")
        self.tabs.addTab(self.control_tab, "设备控制")
        self.tabs.addTab(self.documents_tab, "文档预览/导出")

        central_widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central_widget)
        layout.addWidget(self.connection_widget)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central_widget)

        self._update_tabs_enabled(False)
        self.connection_widget.connected.connect(lambda: self._update_tabs_enabled(True))
        self.connection_widget.connected.connect(self.documents_tab.refresh)
        self.connection_widget.disconnected.connect(lambda: self._update_tabs_enabled(False))

    def _update_tabs_enabled(self, enabled: bool):
        for idx in range(self.tabs.count()):
            self.tabs.widget(idx).setEnabled(enabled)


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
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(100, 149, 237))
    palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
