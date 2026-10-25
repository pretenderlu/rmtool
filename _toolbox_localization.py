"""Original-UI localization, native Chinese and pinyin input sections."""

import logging
import os
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets, sip

import _rmkit_cn
import _native_chinese
import _pinyin_input
import _capacity_recovery
from _ssh import SSHClientWrapper, require_connection
# Dialog helpers are looked up on _toolbox_common at call time so a single
# patch point covers every toolbox section.
import _toolbox_common as _ui
import rmtool as _rmtool  # late-bound access to avoid circular import
from _toolbox_common import ToolboxStatusLabel, load_font_file, select_font_file


class RmkitCnSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_rmkit_cn.LocalizationStatus] = None
        self._busy = False
        self._other_packages_count = 0

        title = QtWidgets.QLabel("原生界面中文")
        title.setObjectName("toolboxFeatureTitle")

        detail = QtWidgets.QLabel(
            "默认会按固件版本精确匹配、下载并安装云端汉化包；"
            "网络不畅时也可下载到电脑或加载本地汉化包。"
            "中文翻译借用法语槽位，不安装后台服务。"
        )
        detail.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel("云端汉化包：检测后显示")
        self.catalog_label.setObjectName("rmkitCnCatalog")
        self.catalog_label.setWordWrap(True)
        self.catalog_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.other_packages_button = QtWidgets.QPushButton("其他固件版本")
        self.other_packages_button.setCheckable(True)
        self.other_packages_button.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.other_packages_button.hide()

        self.other_packages_label = QtWidgets.QLabel()
        self.other_packages_label.setObjectName("rmkitCnOtherCatalog")
        self.other_packages_label.setWordWrap(True)
        self.other_packages_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        self.other_packages_label.hide()

        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("rmkitCnDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("启用中文")
        self.enable_button.setProperty("btnRole", "primary")
        self.restore_button = QtWidgets.QPushButton("还原")
        self.enable_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.project_button = QtWidgets.QPushButton("查看源码")

        self.package_button = QtWidgets.QPushButton("获取汉化包")
        self.package_menu = QtWidgets.QMenu(self.package_button)
        self.download_package_action = self.package_menu.addAction("下载到电脑…")
        self.copy_package_link_action = self.package_menu.addAction("复制下载链接")
        self.package_button.setMenu(self.package_menu)
        self.load_package_button = QtWidgets.QPushButton("加载本地汉化包…")

        primary_buttons = QtWidgets.QHBoxLayout()
        primary_buttons.setContentsMargins(0, 0, 0, 0)
        primary_buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        primary_buttons.addWidget(self.detect_button)
        primary_buttons.addWidget(self.enable_button)
        primary_buttons.addWidget(self.restore_button)
        primary_buttons.addStretch()

        package_buttons = QtWidgets.QHBoxLayout()
        package_buttons.setContentsMargins(0, 0, 0, 0)
        package_buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        package_buttons.addWidget(self.package_button)
        package_buttons.addWidget(self.load_package_button)
        package_buttons.addWidget(self.project_button)
        package_buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.catalog_label)
        layout.addWidget(
            self.other_packages_button,
            alignment=QtCore.Qt.AlignLeft,
        )
        layout.addWidget(self.other_packages_label)
        layout.addWidget(self.status_label)
        layout.addLayout(primary_buttons)
        layout.addLayout(package_buttons)

        self.other_packages_button.toggled.connect(
            self._toggle_other_packages
        )
        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable_localization)
        self.restore_button.clicked.connect(self._restore_localization)
        self.download_package_action.triggered.connect(
            self._download_package_to_computer
        )
        self.copy_package_link_action.triggered.connect(
            self._copy_package_download_link
        )
        self.load_package_button.clicked.connect(self._load_local_package)
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
        package_available = bool(
            connected and self._status and self._status.package is not None
        )
        self.package_button.setEnabled(package_available)
        self.download_package_action.setEnabled(package_available)
        self.copy_package_link_action.setEnabled(package_available)
        self.load_package_button.setEnabled(package_available)

    @staticmethod
    def _package_display_text(package: _rmkit_cn.TranslationPackage) -> str:
        channel_names = {"stable": "正式版", "beta": "测试版"}
        return (
            f"{package.release_version} | "
            f"{channel_names[package.channel]} | "
            + (f"硬件 {package.platform.title()} | " if package.platform else "")
            + f"内部版本 {package.firmware}"
        )

    def _toggle_other_packages(self, expanded: bool):
        self.other_packages_button.setText(
            f"其他固件版本（{self._other_packages_count}） "
            + ("⌄" if expanded else "›")
        )
        self.other_packages_label.setVisible(
            expanded and not self.other_packages_button.isHidden()
        )

    def _apply_status(self, status: _rmkit_cn.LocalizationStatus):
        self._status = status
        if status.available_packages is not None:
            if status.package is not None:
                self.catalog_label.setText(
                    "当前固件汉化包：\n"
                    + self._package_display_text(status.package)
                )
            else:
                self.catalog_label.setText(
                    "当前固件汉化包：没有精确匹配版本"
                )

            other_packages = tuple(
                package
                for package in status.available_packages
                if package != status.package
            )
            self.other_packages_button.setChecked(False)
            if other_packages:
                self._other_packages_count = len(other_packages)
                self.other_packages_button.setText(
                    f"其他固件版本（{self._other_packages_count}） ›"
                )
                self.other_packages_label.setText(
                    "\n".join(
                        self._package_display_text(package)
                        for package in other_packages
                    )
                )
                self.other_packages_button.show()
            else:
                self._other_packages_count = 0
                self.other_packages_button.hide()
            self.other_packages_label.hide()
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
        return _ui.start_plugin_worker(
            self, fn, *args, pending=pending, success=success, error_hint=error_hint,
        )

    def _update_buttons(self):
        self._set_busy(self._busy)

    def _start_package_worker(
        self,
        fn,
        *args,
        pending: str,
        success_status: str,
        success_message,
    ):
        if self._busy:
            return
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(path):
            if sip.isdeleted(self):
                return
            self._set_busy(False)
            if self._status is not None:
                self._apply_status(self._status)
                self.status_label.setText(
                    f"{self.status_label.text()}；{success_status}"
                )
            else:
                self._on_connection_changed(self.ssh_client.is_connected())
            _ui.show_info(
                self,
                _rmtool.APP_NAME,
                success_message(path),
            )

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error(
                    "Localization package operation failed after tab close: %s",
                    exc,
                )
                return
            self._set_busy(False)
            if self._status is not None:
                self._apply_status(self._status)
            else:
                self._on_connection_changed(self.ssh_client.is_connected())
            logging.error("Localization package operation failed: %s", exc)
            _ui.show_error(self, _rmtool.APP_NAME, f"汉化包操作失败：{exc}")

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

    @require_connection
    def _download_package_to_computer(self):
        if not self._status or not self._status.package:
            return
        package = self._status.package
        download_dir = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.DownloadLocation
        )
        suggested_path = os.path.join(download_dir, package.asset)
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "下载汉化包",
            suggested_path,
            "Qt 翻译文件 (*.qm)",
        )
        if not destination:
            return
        if not destination.lower().endswith(".qm"):
            destination += ".qm"

        self._start_package_worker(
            _rmkit_cn.export_translation_package,
            package,
            str(_rmtool.app_state_dir()),
            destination,
            pending="正在下载并校验汉化包…",
            success_status="匹配的汉化包已保存到电脑",
            success_message=lambda path: f"汉化包已校验并保存到：\n{path}",
        )

    @require_connection
    def _copy_package_download_link(self):
        if not self._status or not self._status.package:
            return
        QtWidgets.QApplication.clipboard().setText(
            self._status.package.download_url
        )
        _ui.show_info(self, _rmtool.APP_NAME, "汉化包下载链接已复制到剪贴板。")

    @require_connection
    def _load_local_package(self):
        if not self._status or not self._status.package:
            return
        source_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "加载本地汉化包",
            "",
            "Qt 翻译文件 (*.qm)",
        )
        if not source_path:
            return

        self._start_package_worker(
            _rmkit_cn.import_translation_package,
            self._status.package,
            str(_rmtool.app_state_dir()),
            source_path,
            pending="正在校验本地汉化包…",
            success_status="本地汉化包已校验并缓存，可点击“启用中文”安装",
            success_message=lambda path: (
                "本地汉化包已通过当前固件对应的大小与 SHA-256 校验，"
                f"并写入缓存：\n{path}\n\n"
                "点击“启用中文”时会优先使用这份缓存。"
            ),
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
            _ui.show_warning(self, _rmtool.APP_NAME, "无法识别所选字体的字体族。")
            return None
        return path, family

    @require_connection
    def _enable_localization(self):
        if not self._status or not self._status.package:
            return
        repair_font = self._status.state is _rmkit_cn.LocalizationState.ENABLED
        if not repair_font and not _ui.ask_confirmation(
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
                "请手动重启设备，然后在“设置 → 语言”中选择“法语”，"
                "中文界面才会正式启用。"
            ),
        )

    @require_connection
    def _restore_localization(self):
        if not self._status or not self._status.package:
            return
        if not _ui.ask_confirmation(
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


class NativeChineseSection(QtWidgets.QWidget):
    def __init__(
        self,
        ssh_client: SSHClientWrapper,
        parent=None,
        config: Optional[Dict] = None,
    ):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.config = config if config is not None else {}
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_native_chinese.NativeChineseStatus] = None
        self._busy = False

        title = QtWidgets.QLabel("原生简体中文")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "为精确支持的固件增加独立的“简体中文”选项，保留法语。"
            "旧版法语槽位汉化需先还原并手动重启，再连接设备启用此插件。"
        )
        detail.setWordWrap(True)
        self.catalog_label = QtWidgets.QLabel("精确包：检测后显示")
        self.catalog_label.setWordWrap(True)
        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("启用原生中文")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.uninstall_button = QtWidgets.QPushButton("卸载")
        self.uninstall_button.setProperty("btnRole", "danger")
        self.set_emergency_button = QtWidgets.QPushButton("紧急停用共享 Xovi")
        self.clear_emergency_button = QtWidgets.QPushButton("清除紧急停用")
        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.enable_button,
            self.disable_button,
            self.uninstall_button,
            self.set_emergency_button,
            self.clear_emergency_button,
        ):
            buttons.addWidget(button)
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
        self.enable_button.clicked.connect(self._enable)
        self.disable_button.clicked.connect(self._disable)
        self.uninstall_button.clicked.connect(self._uninstall)
        self.set_emergency_button.clicked.connect(self._set_emergency)
        self.clear_emergency_button.clicked.connect(self._clear_emergency)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText("精确包：检测后显示")
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self._update_buttons()

    def _lock_screen_font_support(self) -> bool:
        return bool(self.config.get("font_lock_screen_support", True))

    def _update_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        self.disable_button.setText(
            "清理残留"
            if state is _native_chinese.NativeChineseState.FIRMWARE_RESIDUE
            else "停用"
        )
        self.detect_button.setEnabled(connected)
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state
            in (
                _native_chinese.NativeChineseState.NOT_INSTALLED,
                _native_chinese.NativeChineseState.INSTALLED_DISABLED,
                _native_chinese.NativeChineseState.OUTDATED,
            )
        )
        self.enable_button.setText(
            "修复并更新"
            if state is _native_chinese.NativeChineseState.OUTDATED
            else "启用原生中文"
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.installed
            and state
            not in (
                _native_chinese.NativeChineseState.INSTALLED_DISABLED,
                _native_chinese.NativeChineseState.DISABLE_PENDING_REBOOT,
            )
        )
        self.uninstall_button.setEnabled(
            connected
            and self._status is not None
            and self._status.installed
            and state in (
                _native_chinese.NativeChineseState.OUTDATED,
                _native_chinese.NativeChineseState.INSTALLED_DISABLED,
                _native_chinese.NativeChineseState.ENABLE_PENDING_REBOOT,
                _native_chinese.NativeChineseState.ENABLED,
                _native_chinese.NativeChineseState.DISABLE_PENDING_REBOOT,
                _native_chinese.NativeChineseState.EMERGENCY_DISABLED,
            )
        )
        self.clear_emergency_button.setEnabled(
            connected
            and self._status is not None
            and self._status.emergency_disabled
        )
        self.set_emergency_button.setEnabled(
            connected
            and self._status is not None
            and self._status.installed
            and not self._status.emergency_disabled
            and state is not _native_chinese.NativeChineseState.FIRMWARE_RESIDUE
        )

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self._update_buttons()

    def _apply_status(self, status: _native_chinese.NativeChineseStatus):
        self._status = status
        package = status.package
        if package is None:
            self.catalog_label.setText("精确包：当前设备不匹配")
        else:
            channel_names = {"stable": "正式版", "beta": "测试版"}
            verification = (
                "已实机验证"
                if package.device_verified
                else "离线验证，尚待实机"
            )
            self.catalog_label.setText(
                f"精确包：{package.release_version} | {channel_names[package.channel]} | "
                f"硬件 {package.platform.title()} | 内部版本 {package.firmware} | "
                f"{verification}"
            )
        messages = {
            _native_chinese.NativeChineseState.INCOMPATIBLE: "当前设备没有精确匹配的原生中文包",
            _native_chinese.NativeChineseState.NOT_INSTALLED: "尚未安装原生简体中文",
            _native_chinese.NativeChineseState.INSTALLED_DISABLED: "原生中文当前未启用",
            _native_chinese.NativeChineseState.ENABLE_PENDING_REBOOT: "已部署，等待手动重启后生效",
            _native_chinese.NativeChineseState.ENABLED: "原生简体中文已加载",
            _native_chinese.NativeChineseState.DISABLE_PENDING_REBOOT: "已停用，等待手动重启后完全移除",
            _native_chinese.NativeChineseState.EMERGENCY_DISABLED: "共享 Xovi 已被紧急标记停用",
            _native_chinese.NativeChineseState.FIRMWARE_RESIDUE: (
                "检测到固件升级后遗留的原生中文共享 Xovi 状态，可安全清理"
            ),
            _native_chinese.NativeChineseState.OUTDATED: "已安装的原生中文包需要修复更新",
            _native_chinese.NativeChineseState.BROKEN: "检测到不完整或不可信的原生中文安装",
        }
        message = messages[status.state]
        if status.detail:
            message += f"：{status.detail}"
        if (
            status.has_cjk_font is False
            and status.identity.platform in {"rm1", "rm2"}
        ):
            message += (
                "\n未检测到可用的简体中文字体匹配；启用时可安装 rmtool 内置的常用字兜底字体。"
            )
        if status.emergency_disabled and status.state != _native_chinese.NativeChineseState.EMERGENCY_DISABLED:
            message += "\n紧急停用标记存在，共享 Xovi 不会在下次启动时载入。"
        self.status_label.setText(message)
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
        on_done=None,
        show_errors: bool = True,
    ):
        return _ui.start_plugin_worker(
            self, fn, *args, pending=pending, success=success,
            close_connection=close_connection, on_done=on_done,
            show_errors=show_errors, retry=self._enable,
        )

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_worker(
            _native_chinese.get_cloud_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在核对设备身份、精确包与共享 Xovi 状态…",
            on_done=on_done,
            show_errors=show_errors,
        )

    def _bundled_fallback_font(self) -> Optional[tuple[str, str]]:
        path = _rmtool.resource_path(
            "assets", "fonts", _rmkit_cn.BUNDLED_FALLBACK_FONT_NAME
        )
        if not path.is_file() or path.stat().st_size != _rmkit_cn.BUNDLED_FALLBACK_FONT_SIZE:
            _ui.show_error(
                self,
                _rmtool.APP_NAME,
                "rmtool 内置中文兜底字体缺失或大小不正确，请重新安装当前版本。",
            )
            return None
        font_id, family = load_font_file(str(path))
        if font_id != -1:
            QtGui.QFontDatabase.removeApplicationFont(font_id)
        if not family:
            _ui.show_error(self, _rmtool.APP_NAME, "无法识别 rmtool 内置中文兜底字体。")
            return None
        return str(path), family

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        package = self._status.package
        updating = self._status.state is _native_chinese.NativeChineseState.OUTDATED
        verification_notice = (
            "该精确包已完成对应真机验证。"
            if package.device_verified
            else "该精确包已完成官方固件离线验证，尚待对应真机验证。"
        )
        fallback_font = None
        fallback_notice = ""
        if (
            self._status.has_cjk_font is False
            and package.platform in {"rm1", "rm2"}
        ):
            fallback_font = self._bundled_fallback_font()
            if fallback_font is None:
                return
            if self._lock_screen_font_support():
                fallback_notice = (
                    "未检测到可用的简体中文字体匹配。继续后，rmtool 会在同一套"
                    f"系统字体镜像与空间检查中安装约 { _rmkit_cn.BUNDLED_FALLBACK_FONT_SIZE / 1024 / 1024:.1f} MiB "
                    "的常用简体中文兜底字体；它会同时供解锁前后的系统界面使用。"
                )
            else:
                fallback_notice = (
                    "未检测到可用的简体中文字体匹配。继续后，rmtool 会把约 "
                    f"{_rmkit_cn.BUNDLED_FALLBACK_FONT_SIZE / 1024 / 1024:.1f} MiB 的常用简体中文兜底字体"
                    "保存在 /home，不占用 /data；如果设备启用锁屏密码，解锁前界面可能仍使用原生字体。"
                )
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{verification_notice}"
            + fallback_notice
            + (
                "将把键盘中文名称补丁迁移到原生中文功能，并保留其他插件。"
                if updating
                else ""
            )
            +
            "它会增加独立的“简体中文”选项，不替换法语。"
            "如果旧版法语槽位汉化仍在启用，操作会被拒绝；请先还原并手动重启，"
            "再重新连接设备执行本步骤。"
            "本次只部署文件，不重启 xochitl 或设备；完成后请手动重启。"
            "是否继续？",
            confirm_text="修复并更新" if updating else "部署并启用",
            cancel_text="取消",
        ):
            return
        fallback_args = fallback_font or (None, None)
        if not self._lock_screen_font_support():
            fallback_args = (*fallback_args, False)
        self._start_worker(
            _native_chinese.enable_cloud,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            *fallback_args,
            pending=(
                "正在验证并更新原生中文资源…"
                if updating
                else "正在下载、验证并部署原生中文资源…"
            ),
            success=(
                "原生简体中文已部署并通过校验，SSH 会话已关闭。\n"
                "请手动重启设备，然后在语言设置中选择“简体中文”。"
            ),
            close_connection=True,
        )

    @require_connection
    def _disable(self):
        if not self._status or not self._status.installed:
            return
        residue = (
            self._status.state
            is _native_chinese.NativeChineseState.FIRMWARE_RESIDUE
        )
        if residue:
            title = "清理旧固件原生中文残留"
            confirmation = (
                "固件升级后，旧共享 Xovi 已不再载入。将先把 zh_CN 安全改为 en，"
                "再删除经内置清单验证的整套旧共享状态；旧固件的其他 rmtool Xovi "
                "功能也会一并移除。本次不会重启设备，是否继续？"
            )
            confirm_text = "清理残留"
            pending = "正在验证并清理旧固件原生中文共享 Xovi 残留…"
            success = (
                "旧固件共享 Xovi 残留已清理，SSH 会话已关闭。\n"
                "重新连接并检测后，可安装当前固件支持的功能。"
            )
        else:
            title = _rmtool.APP_NAME
            confirmation = (
                "将停用原生简体中文。如果当前选中 zh_CN，rmtool 会先把"
                "[General] language 安全改为 en，再移除该功能的 QMD、扩展和翻译目录。"
                "其他共享 Xovi 功能会保留。操作不会重启设备，是否继续？"
            )
            confirm_text = "停用原生中文"
            pending = "正在安全停用原生简体中文…"
            success = "原生简体中文已停用，SSH 会话已关闭。\n请手动重启设备。"
        if not _ui.ask_confirmation(
            self,
            title,
            confirmation,
            confirm_text=confirm_text,
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _native_chinese.disable,
            self.ssh_client,
            _native_chinese._trusted_catalog(),
            pending=pending,
            success=success,
            close_connection=True,
        )

    @require_connection
    def _uninstall(self):
        if not self._status or not self._status.installed:
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将卸载原生简体中文及其 rmtool 管理的共享文件；其他插件和共享运行时会保留。"
            "本次不会自动重启设备，完成后请手动重启。是否继续？",
            confirm_text="卸载原生中文",
            cancel_text="取消",
            danger=True,
        ):
            return
        self._start_worker(
            _native_chinese.uninstall,
            self.ssh_client,
            _native_chinese._trusted_catalog(),
            pending="正在验证并卸载原生简体中文…",
            success="原生简体中文已卸载，SSH 会话已关闭。\n请手动重启设备。",
            close_connection=True,
        )

    @require_connection
    def _set_emergency(self):
        if (
            not self._status
            or not self._status.installed
            or self._status.emergency_disabled
        ):
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将创建共享 Xovi 紧急停用标记。当前界面不会重启；下次手动重启时，"
            "点击翻页、快速黑白和原生中文等 rmtool 共享 Xovi 功能都不会载入。"
            "是否继续？",
            confirm_text="创建停用标记",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _native_chinese.set_emergency_disable,
            self.ssh_client,
            _native_chinese._trusted_catalog(),
            pending="正在创建紧急停用标记…",
            success="紧急停用标记已创建；当前界面未重启，下次手动重启时生效。",
        )

    @require_connection
    def _clear_emergency(self):
        if not self._status or not self._status.emergency_disabled:
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "清除紧急停用标记后，已启用的共享 Xovi 功能会在下次手动重启时恢复加载。是否继续？",
            confirm_text="清除标记",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _native_chinese.clear_emergency_disable,
            self.ssh_client,
            _native_chinese._trusted_catalog(),
            pending="正在清除紧急停用标记…",
            success="紧急停用标记已清除；如需恢复加载，请稍后手动重启设备。",
        )


class PinyinInputSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_pinyin_input.PinyinInputStatus] = None
        self._busy = False

        title = QtWidgets.QLabel("拼音输入法")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "为系统软键盘和实体键盘增加离线拼音候选栏。词库完全保存在设备上，"
            "不使用云端预测；功能接入 rmtool 共享 Xovi，启停不会影响其他插件。"
        )
        detail.setWordWrap(True)
        self.catalog_label = QtWidgets.QLabel("精确包：检测后显示")
        self.catalog_label.setWordWrap(True)
        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("安装并启用")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.uninstall_button = QtWidgets.QPushButton("卸载")
        self.uninstall_button.setProperty("btnRole", "danger")
        self.project_button = QtWidgets.QPushButton("查看来源")
        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.enable_button,
            self.disable_button,
            self.uninstall_button,
            self.project_button,
        ):
            buttons.addWidget(button)
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
        self.enable_button.clicked.connect(self._enable)
        self.disable_button.clicked.connect(self._disable)
        self.uninstall_button.clicked.connect(self._uninstall)
        self.project_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl("https://github.com/boangs/rmkit")
            )
        )
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText("精确包：检测后显示")
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self._update_buttons()

    def _update_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        self.detect_button.setEnabled(connected)
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state in (
                _pinyin_input.PinyinInputState.NOT_INSTALLED,
                _pinyin_input.PinyinInputState.INSTALLED_DISABLED,
                _pinyin_input.PinyinInputState.OUTDATED,
                _pinyin_input.PinyinInputState.BROKEN,
            )
        )
        self.enable_button.setText(
            "修复并更新"
            if state in (
                _pinyin_input.PinyinInputState.OUTDATED,
                _pinyin_input.PinyinInputState.BROKEN,
            )
            else "安装并启用"
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.installed
            and state
            not in (
                _pinyin_input.PinyinInputState.INSTALLED_DISABLED,
                _pinyin_input.PinyinInputState.DISABLE_PENDING_REBOOT,
                _pinyin_input.PinyinInputState.BROKEN,
            )
        )
        self.uninstall_button.setEnabled(
            connected
            and self._status is not None
            and self._status.installed
            and state in (
                _pinyin_input.PinyinInputState.OUTDATED,
                _pinyin_input.PinyinInputState.INSTALLED_DISABLED,
                _pinyin_input.PinyinInputState.ENABLE_PENDING_REBOOT,
                _pinyin_input.PinyinInputState.ENABLED,
                _pinyin_input.PinyinInputState.DISABLE_PENDING_REBOOT,
                _pinyin_input.PinyinInputState.EMERGENCY_DISABLED,
            )
        )

    def _apply_status(self, status: _pinyin_input.PinyinInputStatus):
        self._status = status
        package = status.package
        if package is None:
            self.catalog_label.setText("精确包：当前设备不匹配")
        else:
            channel_names = {"stable": "正式版", "beta": "测试版"}
            verification = "已实机验证" if package.device_verified else "离线验证，尚待实机"
            self.catalog_label.setText(
                f"精确包：{package.release_version} | {channel_names[package.channel]} | "
                f"硬件 {package.platform.title()} | "
                f"内部版本 {package.firmware} | {verification}"
            )
        messages = {
            _pinyin_input.PinyinInputState.INCOMPATIBLE: "当前固件没有精确匹配的拼音输入法包",
            _pinyin_input.PinyinInputState.NOT_INSTALLED: "尚未安装拼音输入法",
            _pinyin_input.PinyinInputState.INSTALLED_DISABLED: "拼音输入法当前未启用",
            _pinyin_input.PinyinInputState.ENABLE_PENDING_REBOOT: "已部署，等待手动重启后生效",
            _pinyin_input.PinyinInputState.ENABLED: "拼音输入法已加载",
            _pinyin_input.PinyinInputState.DISABLE_PENDING_REBOOT: "已停用，等待手动重启后完全移除",
            _pinyin_input.PinyinInputState.EMERGENCY_DISABLED: "共享 Xovi 已被紧急标记停用",
            _pinyin_input.PinyinInputState.OUTDATED: "已安装的拼音包需要修复更新",
            _pinyin_input.PinyinInputState.BROKEN: "检测到不完整或不可验证的拼音输入法状态",
        }
        message = messages[status.state]
        if status.detail:
            message += f"：{status.detail}"
        self.status_label.setText(message)
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
        on_done=None,
        show_errors: bool = True,
        recovery_intent=None,
    ):
        return _ui.start_plugin_worker(
            self, fn, *args, pending=pending, success=success,
            close_connection=close_connection, on_done=on_done,
            show_errors=show_errors, retry=self._enable,
            recovery_intent=recovery_intent,
        )

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_worker(
            _pinyin_input.get_status,
            self.ssh_client,
            _pinyin_input._trusted_catalog(),
            pending="正在核对设备身份、服务载荷与共享 Xovi 状态…",
            on_done=on_done,
            show_errors=show_errors,
        )

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        repairing = self._status.state in (
            _pinyin_input.PinyinInputState.OUTDATED,
            _pinyin_input.PinyinInputState.BROKEN,
        )
        action = (
            "将严格验证并修复设备上已存在的 rmtool 拼音服务，重新接入共享 Xovi，"
            "并把中文键盘布局资源迁移到 QRR 可读取的位置。"
            if repairing
            else "将下载并验证离线拼音输入法包，把候选栏和输入拦截器接入 rmtool 共享 Xovi。"
        )
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{action}"
            "若设备已有 rmkit 输入法，操作会在写入前停止。部署过程不会重启 xochitl 或设备；"
            "完成后请手动重启。是否继续？",
            confirm_text="修复并更新" if repairing else "安装并启用",
            cancel_text="取消",
        ):
            return
        state_dir = str(_rmtool.app_state_dir())
        intent = _capacity_recovery.make_original_intent(
            self.ssh_client,
            _pinyin_input.FEATURE_ID,
            self._status.package,
            state_dir,
            self._status.identity,
        )
        self._start_worker(
            _pinyin_input.enable_cloud,
            self.ssh_client,
            self._status.package,
            state_dir,
            pending=("正在验证并修复旧版拼音输入法…" if repairing else
                     "正在下载、验证并部署拼音输入法…"),
            success=(
                "拼音输入法已补齐中文键盘布局并通过校验，SSH 会话已关闭。\n"
                if repairing
                else "拼音输入法已部署并通过校验，SSH 会话已关闭。\n"
            ) + "请手动重启设备后，在键盘语言中选择中文。",
            close_connection=True,
            recovery_intent=intent,
        )

    @require_connection
    def _disable(self):
        if not self._status or not self._status.installed:
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将从共享 Xovi 中停用拼音候选栏和输入拦截器，并移除 rmtool 管理的离线词库服务。"
            "其他 rmtool 插件会完整保留；本次不会重启设备。是否继续？",
            confirm_text="停用拼音输入法",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _pinyin_input.disable,
            self.ssh_client,
            _pinyin_input._trusted_catalog(),
            pending="正在安全停用拼音输入法并保留其他共享插件…",
            success="拼音输入法已停用，SSH 会话已关闭。\n请手动重启设备使修改生效。",
            close_connection=True,
        )

    @require_connection
    def _uninstall(self):
        if not self._status or not self._status.installed:
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将卸载拼音输入法及其 rmtool 管理的服务文件；其他插件和共享运行时会保留。"
            "本次不会自动重启设备，完成后请手动重启。是否继续？",
            confirm_text="卸载拼音输入法",
            cancel_text="取消",
            danger=True,
        ):
            return
        self._start_worker(
            _pinyin_input.uninstall,
            self.ssh_client,
            _pinyin_input._trusted_catalog(),
            pending="正在验证并卸载拼音输入法…",
            success="拼音输入法已卸载，SSH 会话已关闭。\n请手动重启设备。",
            close_connection=True,
        )
