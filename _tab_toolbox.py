"""FontTab, TimeTab, ControlTab, DashboardTab, and ToolboxTab extracted from rmtool.py."""

import json
import logging
import os
import posixpath
import tempfile
from datetime import datetime
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5 import QtWebEngineWidgets

from _dialogs import ask_confirmation, show_error, show_info, show_warning
import _rmkit_cn
from _ssh import SSHClientWrapper, remount_rw, require_connection
import rmtool as _rmtool  # late-bound access to avoid circular import


FONTCONFIG_DIR = _rmkit_cn.FONTCONFIG_DIR
FONTCONFIG_FILE = _rmkit_cn.FONTCONFIG_FILE


def select_font_file(parent: QtWidgets.QWidget) -> Optional[str]:
    path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent, "选择字体文件", "", "字体文件 (*.ttf *.otf)"
    )
    return path or None


def load_font_file(file_path: str) -> tuple[int, Optional[str]]:
    font_id = QtGui.QFontDatabase.addApplicationFont(file_path)
    families = (
        QtGui.QFontDatabase.applicationFontFamilies(font_id)
        if font_id != -1
        else []
    )
    return font_id, families[0] if families else None


class FontTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.config = config
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._font_progress: Optional[QtWidgets.QProgressDialog] = None
        self._selected_font_path: Optional[str] = None
        self._selected_font_family: Optional[str] = None
        self._preview_font_id = -1

        self.font_path_label = QtWidgets.QLabel("未选择文件")
        self.rename_checkbox = QtWidgets.QCheckBox(f"上传时重命名为 {_rmtool.DEFAULT_FONT_NAME}")
        self.rename_checkbox.setChecked(True)
        self.rename_checkbox.toggled.connect(self._update_target_name_label)

        self.target_name_label = QtWidgets.QLabel()
        self.target_name_label.setObjectName("fontTargetName")

        self.preview_panel = QtWidgets.QFrame()
        self.preview_panel.setObjectName("fontPreviewPanel")
        self.preview_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        preview_layout = QtWidgets.QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        preview_layout.setSpacing(_rmtool.SUBSECTION_GAP)

        self.preview_title_label = QtWidgets.QLabel("选择字体后可在这里预览")
        self.preview_title_label.setObjectName("fontPreviewTitle")
        self.preview_sample_label = QtWidgets.QLabel()
        self.preview_sample_label.setObjectName("fontPreviewSample")
        self.preview_sample_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.preview_sample_label.setWordWrap(True)
        self.preview_sample_label.setMinimumHeight(120)
        preview_layout.addWidget(self.preview_title_label)
        preview_layout.addWidget(self.preview_sample_label)

        self.select_button = QtWidgets.QPushButton("选择字体")
        self.select_button.clicked.connect(self._select_font_file)
        self.upload_button = QtWidgets.QPushButton("上传字体")
        self.upload_button.setEnabled(False)
        self.upload_button.clicked.connect(self._upload_selected_font)

        actions_layout = QtWidgets.QHBoxLayout()
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        actions_layout.addWidget(self.select_button, 1)
        actions_layout.addWidget(self.upload_button, 1)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(self.font_path_label)
        layout.addWidget(self.rename_checkbox)
        layout.addWidget(self.target_name_label)
        layout.addWidget(self.preview_panel)
        layout.addLayout(actions_layout)
        self.setLayout(layout)
        self._reset_font_preview()
        self._update_target_name_label()

    def _select_font_file(self):
        file_path = select_font_file(self)
        if not file_path:
            return

        self._release_preview_font()
        self.font_path_label.setText(file_path)
        preview_font_id, preview_family = load_font_file(file_path)
        if preview_font_id == -1 or not preview_family:
            self._selected_font_path = None
            self._selected_font_family = None
            self._reset_font_preview("无法预览所选字体，请重新选择有效字体文件。")
            show_warning(self, _rmtool.APP_NAME, "无法加载所选字体的本地预览。")
            self._update_target_name_label()
            return

        self._selected_font_path = file_path
        self._preview_font_id = preview_font_id
        self._selected_font_family = preview_family
        self.preview_title_label.setText(f"{preview_family} 预览")
        preview_font = QtGui.QFont(preview_family, 18)
        preview_font.setStyleStrategy(QtGui.QFont.PreferAntialias)
        self.preview_sample_label.setFont(preview_font)
        self.preview_sample_label.setText(_rmtool.FONT_PREVIEW_TEXT)
        self.upload_button.setEnabled(True)
        self._update_target_name_label()

    @require_connection
    def _upload_selected_font(self):
        if not self._selected_font_path:
            show_info(self, _rmtool.APP_NAME, "请先选择需要上传的字体文件。")
            return
        file_path = self._selected_font_path
        new_name = self._target_font_name()
        font_family = self._selected_font_family or ""

        self.select_button.setEnabled(False)
        self.upload_button.setEnabled(False)
        progress = QtWidgets.QProgressDialog("正在上传字体…", "", 0, 0, self)
        progress.setWindowTitle(_rmtool.APP_NAME)
        progress.setWindowModality(QtCore.Qt.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()
        self._font_progress = progress

        worker = _rmtool.Worker(self._upload_font, file_path, new_name, font_family)

        def on_finished(_: object):
            self._close_font_progress()
            show_info(
                self,
                _rmtool.APP_NAME,
                "字体上传完成，并已刷新字体缓存。\n字体将在设备重启后生效。",
            )
            confirm = ask_confirmation(
                self,
                _rmtool.APP_NAME,
                "是否立即重启设备以应用新字体？",
                confirm_text="立即重启",
                cancel_text="稍后再说",
            )
            if confirm:
                try:
                    self.ssh_client.exec_command("reboot")
                    show_info(self, _rmtool.APP_NAME, "已发送重启命令。")
                except Exception as exc:
                    logging.exception("Reboot after font upload failed")
                    show_error(self, _rmtool.APP_NAME, f"重启失败：{exc}")

        def on_error(exc: Exception):
            self._close_font_progress()
            logging.exception("Font upload failed")
            show_error(self, _rmtool.APP_NAME, f"字体上传失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _close_font_progress(self):
        if self._font_progress:
            self._font_progress.close()
            self._font_progress.deleteLater()
            self._font_progress = None
        self.select_button.setEnabled(True)
        self.upload_button.setEnabled(bool(self._selected_font_path))

    def _target_font_name(self) -> str:
        if self.rename_checkbox.isChecked() or not self._selected_font_path:
            return _rmtool.DEFAULT_FONT_NAME
        return os.path.basename(self._selected_font_path)

    def _update_target_name_label(self):
        self.target_name_label.setText(f"上传后将保存为：{self._target_font_name()}")

    def _reset_font_preview(self, title: str = "选择字体后可在这里预览"):
        self.preview_title_label.setText(title)
        self.preview_sample_label.setFont(self.font())
        self.preview_sample_label.setText(_rmtool.FONT_PREVIEW_TEXT)
        self.upload_button.setEnabled(False)

    def _release_preview_font(self):
        if self._preview_font_id != -1:
            QtGui.QFontDatabase.removeApplicationFont(self._preview_font_id)
            self._preview_font_id = -1

    @staticmethod
    def _fontconfig_override(font_family: str) -> str:
        return _rmkit_cn.fontconfig_override(font_family)

    def _upload_font(self, file_path: str, new_name: str, font_family: str):
        font_dir = self.config.get("paths", {}).get("font", _rmtool.DEFAULT_FONT_DIR)
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_config_path = os.path.join(tmpdir, "fonts.conf")
            if font_family:
                with open(temp_config_path, "w", encoding="utf-8") as config_file:
                    config_file.write(self._fontconfig_override(font_family))
            _rmkit_cn.upload_font(
                self.ssh_client,
                file_path,
                font_dir,
                new_name,
                fontconfig_local_path=temp_config_path if font_family else None,
                fontconfig_remote_path=FONTCONFIG_FILE if font_family else None,
            )


class TimeTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)

        self.sync_button = QtWidgets.QPushButton("使用本地时间同步")
        self.sync_button.setProperty("cssClass", "secondary")
        self.info_button = QtWidgets.QPushButton("查看当前时间信息")
        self.info_button.setProperty("cssClass", "secondary")
        self.tz_button = QtWidgets.QPushButton("设置为东八区")
        self.tz_button.setProperty("cssClass", "secondary")

        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addWidget(self.sync_button)
        button_layout.addWidget(self.info_button)
        button_layout.addWidget(self.tz_button)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(button_layout)
        self.output.setMaximumHeight(200)
        layout.addWidget(self.output)
        self.setLayout(layout)

        self.sync_button.clicked.connect(self._sync_time)
        self.info_button.clicked.connect(self._show_time_info)
        self.tz_button.clicked.connect(self._set_timezone)

    def _append_output(self, text: str):
        self.output.appendPlainText(text)
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    @require_connection
    def _sync_time(self):
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with remount_rw(self.ssh_client):
                self.ssh_client.exec_checked(f'date -s "{now}"')
                self.ssh_client.exec_checked("hwclock -w")
            self._append_output(f"已同步设备时间到 {now}")
        except Exception as exc:
            logging.exception("Sync time failed")
            show_error(self, _rmtool.APP_NAME, f"同步失败：{exc}")

    @require_connection
    def _show_time_info(self):
        try:
            commands = {
                "系统时间": "date",
                "硬件时钟": "hwclock -r",
                "时区信息": "timedatectl",
            }
            for title, cmd in commands.items():
                stdout = self.ssh_client.exec_checked(cmd)
                self._append_output(f"[{title}]\n{stdout.strip()}\n")
        except Exception as exc:
            logging.exception("Get time info failed")
            show_error(self, _rmtool.APP_NAME, f"查询失败：{exc}")

    @require_connection
    def _set_timezone(self):
        try:
            with remount_rw(self.ssh_client):
                self.ssh_client.exec_checked("timedatectl set-timezone Asia/Shanghai")
            self._append_output("已将时区设置为 Asia/Shanghai")
        except Exception as exc:
            logging.exception("Set timezone failed")
            show_error(self, _rmtool.APP_NAME, f"设置失败：{exc}")


class ControlTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client

        self.restart_button = QtWidgets.QPushButton("重启设备")
        self.restart_button.setProperty("cssClass", "danger")
        self.enable_wifi_ssh_button = QtWidgets.QPushButton("开启 Wi-Fi SSH 通道")
        self.enable_wifi_ssh_button.setProperty("cssClass", "secondary")
        self.brightness_button = QtWidgets.QPushButton("提升前光亮度")
        self.brightness_button.setProperty("cssClass", "secondary")

        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.restart_button)
        layout.addWidget(self.enable_wifi_ssh_button)
        layout.addWidget(self.brightness_button)
        layout.addStretch()
        self.setLayout(layout)

        self.restart_button.clicked.connect(self._restart_device)
        self.enable_wifi_ssh_button.clicked.connect(self._enable_wifi_ssh)
        self.brightness_button.clicked.connect(self._increase_brightness)

    @require_connection
    def _restart_device(self):
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "确定要重启设备吗？这将断开连接。",
            confirm_text="重启",
            cancel_text="取消",
            danger=True,
        ):
            return
        try:
            self.ssh_client.exec_command("reboot")
            show_info(self, _rmtool.APP_NAME, "已发送重启命令。")
        except Exception as exc:
            logging.exception("Restart failed")
            show_error(self, _rmtool.APP_NAME, f"重启失败：{exc}")

    @require_connection
    def _enable_wifi_ssh(self):
        try:
            self.ssh_client.exec_checked("rm-ssh-over-wlan on")
            show_info(
                self,
                _rmtool.APP_NAME,
                "已开启 Wi-Fi SSH，请在断开 USB 后使用 WLAN 地址连接。",
            )
        except Exception as exc:
            logging.exception("Enable Wi-Fi SSH failed")
            show_error(self, _rmtool.APP_NAME, f"操作失败：{exc}")

    @require_connection
    def _increase_brightness(self):
        try:
            with remount_rw(self.ssh_client):
                self.ssh_client.exec_checked(
                    "cat /sys/class/backlight/rm_frontlight/max_brightness > /sys/class/backlight/rm_frontlight/brightness"
                )
                self.ssh_client.exec_checked(
                    "echo yes > /sys/class/backlight/rm_frontlight/linear_mapping"
                )
                self.ssh_client.exec_checked("umount -l /etc")

            with remount_rw(self.ssh_client):
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
                self.ssh_client.exec_checked(cmd)
                self.ssh_client.exec_checked("systemctl daemon-reload")
                self.ssh_client.exec_checked(
                    "systemctl enable --now tweak-brightness-slider.service"
                )
            show_info(self, _rmtool.APP_NAME, "前光亮度已调整。")
        except Exception as exc:
            logging.exception("Brightness tweak failed")
            show_error(self, _rmtool.APP_NAME, f"设置失败：{exc}")


class RmkitCnSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_rmkit_cn.LocalizationStatus] = None
        self._busy = False

        title = QtWidgets.QLabel("原生界面中文")
        title.setObjectName("rmkitCnStatus")
        title_font = QtGui.QFont(title.font())
        title_font.setPointSize(max(title_font.pointSize() + 2, 14))
        title_font.setBold(True)
        title.setFont(title_font)

        detail = QtWidgets.QLabel(
            "连接设备后会按固件版本精确匹配并下载云端汉化包。"
            "中文翻译借用法语槽位，不安装后台服务。"
        )
        detail.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel("云端汉化包：检测后显示")
        self.catalog_label.setObjectName("rmkitCnCatalog")
        self.catalog_label.setWordWrap(True)
        self.catalog_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.status_label = QtWidgets.QLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("rmkitCnDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.detect_button.setProperty("cssClass", "secondary")
        self.enable_button = QtWidgets.QPushButton("启用中文")
        self.restore_button = QtWidgets.QPushButton("还原")
        self.restore_button.setProperty("cssClass", "secondary")
        self.enable_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.project_button = QtWidgets.QPushButton("查看源码")
        self.project_button.setProperty("cssClass", "secondary")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        buttons.addWidget(self.detect_button)
        buttons.addWidget(self.enable_button)
        buttons.addWidget(self.restore_button)
        buttons.addWidget(self.project_button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.catalog_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable_localization)
        self.restore_button.clicked.connect(self._restore_localization)
        self.project_button.clicked.connect(
            lambda: self._open_external(_rmkit_cn.REPO_URL)
        )
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _open_external(self, url: str):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self.detect_button.setEnabled(connected and not self._busy)
        self._update_action_buttons()

    def _update_action_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        repair_font = bool(
            self._status
            and self._status.package is not None
            and state is _rmkit_cn.LocalizationState.ENABLED
            and not self._status.has_cjk_font
        )
        self.enable_button.setText("修复中文字体" if repair_font else "启用中文")
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and (
                repair_font
                or state
                in (
                    _rmkit_cn.LocalizationState.NOT_INSTALLED,
                    _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED,
                )
            )
        )
        self.restore_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state
            in (
                _rmkit_cn.LocalizationState.ENABLED,
                _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED,
            )
        )

    def _apply_status(self, status: _rmkit_cn.LocalizationStatus):
        self._status = status
        if status.available_packages is not None:
            if status.available_packages:
                channel_names = {"stable": "正式版", "beta": "测试版"}
                entries = [
                    f"{package.release_version} | "
                    f"{channel_names[package.channel]} | "
                    f"内部版本 {package.firmware}"
                    for package in status.available_packages
                ]
                self.catalog_label.setText(
                    "云端汉化包：\n" + "\n".join(entries)
                )
            else:
                self.catalog_label.setText("云端汉化包：当前没有可用版本")
        messages = {
            _rmkit_cn.LocalizationState.INCOMPATIBLE: (
                f"云端没有与固件 {status.firmware or '未知'} 精确匹配的汉化包，未执行任何修改"
            ),
            _rmkit_cn.LocalizationState.NOT_INSTALLED: "尚未安装中文翻译",
            _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED: (
                "已发现中文翻译，但当前未启用"
            ),
            _rmkit_cn.LocalizationState.ENABLED: "中文翻译已启用",
        }
        message = messages[status.state]
        if status.state is not _rmkit_cn.LocalizationState.INCOMPATIBLE:
            font_status = (
                "已检测到简体中文字体"
                if status.has_cjk_font
                else "未检测到简体中文字体"
            )
            message = f"{message}；{font_status}"
        self.status_label.setText(message)
        self._update_action_buttons()

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        self.detect_button.setEnabled(
            self.ssh_client.is_connected() and not busy
        )
        if message:
            self.status_label.setText(message)
        self._update_action_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        error_hint: str = "若设备界面无响应，请手动重启设备。",
    ):
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status: _rmkit_cn.LocalizationStatus):
            self._set_busy(False)
            self._apply_status(status)
            if success:
                show_info(self, _rmtool.APP_NAME, success)

        def on_error(exc: Exception):
            self._set_busy(False)
            self.status_label.setText("操作失败，请查看提示后重试")
            logging.error("Original UI localization failed: %s", exc)
            show_error(
                self,
                _rmtool.APP_NAME,
                f"操作失败：{exc}\n{error_hint}",
            )

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_worker(
            _rmkit_cn.get_cloud_localization_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在获取云端清单并检测固件与汉化状态…",
            error_hint="设备未被修改，请检查电脑网络连接后重试。",
        )

    def _choose_missing_font(self) -> Optional[tuple[str, str]]:
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle(_rmtool.APP_NAME)
        dialog.setIcon(QtWidgets.QMessageBox.Warning)
        dialog.setText("设备缺少简体中文字体，请选择用于本次汉化的字体。")
        bundled_button = dialog.addButton(
            "安装内置 Noto", QtWidgets.QMessageBox.AcceptRole
        )
        local_button = dialog.addButton(
            "选择本地字体…", QtWidgets.QMessageBox.ActionRole
        )
        dialog.addButton("取消", QtWidgets.QMessageBox.RejectRole)
        dialog.exec_()
        if dialog.clickedButton() is bundled_button:
            path = str(
                _rmtool.resource_path(
                    "assets", "fonts", _rmkit_cn.BUNDLED_FONT_NAME
                )
            )
        elif dialog.clickedButton() is local_button:
            path = select_font_file(self)
            if not path:
                return None
        else:
            return None

        font_id, family = load_font_file(path)
        if font_id != -1:
            QtGui.QFontDatabase.removeApplicationFont(font_id)
        if not family:
            show_warning(self, _rmtool.APP_NAME, "无法识别所选字体的字体族。")
            return None
        return path, family

    @require_connection
    def _enable_localization(self):
        if not self._status or not self._status.package:
            return
        repair_font = self._status.state is _rmkit_cn.LocalizationState.ENABLED
        if not repair_font and not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将停止原生界面、备份当前配置并启用中文。完成后不会自动重启设备，是否继续？",
            confirm_text="启用中文",
            cancel_text="取消",
        ):
            return

        font_path = None
        font_family = None
        if not self._status.has_cjk_font:
            selected_font = self._choose_missing_font()
            if not selected_font:
                return
            font_path, font_family = selected_font
        self._start_worker(
            _rmkit_cn.enable_cloud_localization,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            font_path,
            font_family,
            pending=(
                "正在安装并验证中文字体…"
                if repair_font
                else "正在下载并校验固件对应的汉化包，然后备份并部署…"
            ),
            success=(
                "中文字体已安装并验证，SSH 会话已关闭。\n"
                if repair_font
                else "汉化文件与语言配置已写入，原生界面已停止，SSH 会话已关闭。\n"
            )
            + (
                "请手动重启设备使修改生效。"
            ),
        )

    @require_connection
    def _restore_localization(self):
        if not self._status or not self._status.package:
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将停止原生界面并恢复汉化前的配置与翻译文件。完成后不会自动重启设备，是否继续？",
            confirm_text="还原",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _rmkit_cn.restore_localization,
            self.ssh_client,
            self._status.package,
            pending="正在还原汉化前状态…",
            success=(
                "原配置与翻译文件已还原，原生界面已停止，SSH 会话已关闭。\n"
                "请手动重启设备使修改生效。"
            ),
        )


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

        html_path = _rmtool.resource_path("web", "dashboard.html")
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

    def set_theme(self, theme: str):
        self._state["theme"] = theme
        self._apply_state()

    def _apply_state(self):
        script = f"window.updateDashboard({json.dumps(self._state, ensure_ascii=False)});"
        if self._loaded:
            self.view.page().runJavaScript(script)
        else:
            self._pending_script = script


class ToolboxTab(QtWidgets.QWidget):
    def __init__(
        self,
        ssh_client: SSHClientWrapper,
        config: Dict,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self.font_section = FontTab(ssh_client, config)
        self.time_section = TimeTab(ssh_client)
        self.control_section = ControlTab(ssh_client)
        self.rmkit_cn_section = RmkitCnSection(ssh_client)

        font_group = QtWidgets.QGroupBox("字体上传")
        font_layout = QtWidgets.QVBoxLayout()
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_layout.addWidget(self.font_section)
        font_group.setLayout(font_layout)

        time_group = QtWidgets.QGroupBox("时间管理")
        time_layout = QtWidgets.QVBoxLayout()
        time_layout.setContentsMargins(0, _rmtool.SUBSECTION_GAP, 0, 0)
        time_layout.addWidget(self.time_section)
        time_group.setLayout(time_layout)

        control_group = QtWidgets.QGroupBox("设备控制")
        control_layout = QtWidgets.QVBoxLayout()
        control_layout.setContentsMargins(0, _rmtool.SUBSECTION_GAP, 0, 0)
        control_layout.addWidget(self.control_section)
        control_group.setLayout(control_layout)

        rmkit_cn_group = QtWidgets.QGroupBox("系统汉化")
        rmkit_cn_layout = QtWidgets.QVBoxLayout()
        rmkit_cn_layout.setContentsMargins(0, _rmtool.SUBSECTION_GAP, 0, 0)
        rmkit_cn_layout.addWidget(self.rmkit_cn_section)
        rmkit_cn_group.setLayout(rmkit_cn_layout)

        koreader_group = QtWidgets.QGroupBox("KOReader / 第三方应用")
        koreader_info = QtWidgets.QLabel(
            "安装 KOReader 等第三方应用需要通过 vellum 包管理器，"
            "请参考以下项目文档：\n"
        )
        koreader_info.setWordWrap(True)

        koreader_links = QtWidgets.QLabel(
            '<a href="https://github.com/vellum-dev/vellum-cli">'
            "vellum (包管理器)</a>"
            '  |  <a href="https://github.com/asivery/rm-xovi-extensions">'
            "xovi (扩展框架)</a>"
            '  |  <a href="https://github.com/asivery/rm-appload">'
            "rm-appload (应用加载器)</a>"
            '  |  <a href="https://github.com/koreader/koreader/wiki/'
            'Installation-on-Remarkable">KOReader 安装指南</a>'
        )
        koreader_links.setOpenExternalLinks(True)
        koreader_links.setWordWrap(True)

        koreader_layout = QtWidgets.QVBoxLayout()
        koreader_layout.setContentsMargins(0, _rmtool.SUBSECTION_GAP, 0, 0)
        koreader_layout.addWidget(koreader_info)
        koreader_layout.addWidget(koreader_links)
        koreader_group.setLayout(koreader_layout)

        self.content_widget = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
        )
        content_layout.setSpacing(_rmtool.PANEL_GAP)
        content_layout.addWidget(font_group)
        content_layout.addWidget(time_group)
        content_layout.addWidget(control_group)
        content_layout.addWidget(rmkit_cn_group)
        content_layout.addWidget(koreader_group)
        content_layout.addStretch()

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(self.content_widget)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
