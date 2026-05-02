"""ConnectionWidget extracted from rmtool.py."""

import logging
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from _ssh import SSHClientWrapper, UnknownHostKeyError
import rmtool as _rmtool  # late-bound access to avoid circular import


def _keyring():
    return _rmtool.keyring


class ConnectionWidget(QtWidgets.QWidget):
    connected = QtCore.pyqtSignal()
    disconnected = QtCore.pyqtSignal()
    device_changed = QtCore.pyqtSignal(dict)
    status_message = QtCore.pyqtSignal(str, str, int)

    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebarConnection")
        self.ssh_client = ssh_client
        self.config = config

        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._connection_progress: Optional[QtWidgets.QProgressDialog] = None
        self._active_connection_worker: Optional[_rmtool.Worker] = None

        # -- Status indicator (top of sidebar) --
        self.status_dot = QtWidgets.QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(12, 12)
        self.status_text = QtWidgets.QLabel("未连接")
        self.status_text.setObjectName("statusText")
        status_row = QtWidgets.QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_text)
        status_row.addStretch()

        self.device_title_label = QtWidgets.QLabel()
        self.device_title_label.setObjectName("deviceCardTitle")
        self.device_title_label.setWordWrap(True)
        self.device_meta_label = QtWidgets.QLabel()
        self.device_meta_label.setObjectName("deviceCardMeta")
        self.device_meta_label.setWordWrap(True)
        self.device_host_label = QtWidgets.QLabel()
        self.device_host_label.setObjectName("deviceCardHost")
        self.device_host_label.setWordWrap(True)
        for label in (self.device_title_label, self.device_meta_label, self.device_host_label):
            label.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)

        summary_card = QtWidgets.QFrame()
        summary_card.setObjectName("deviceCard")
        summary_layout = QtWidgets.QVBoxLayout(summary_card)
        summary_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        summary_layout.setSpacing(6)
        summary_layout.addWidget(self.device_title_label)
        summary_layout.addWidget(self.device_meta_label)
        summary_layout.addWidget(self.device_host_label)

        # -- Device selector --
        self.device_combo = QtWidgets.QComboBox()
        self.add_device_button = QtWidgets.QToolButton()
        self.add_device_button.setText("+")
        self.add_device_button.setText("新增")
        self.add_device_button.setToolTip("新增一个设备配置")
        self.remove_device_button = QtWidgets.QToolButton()
        self.remove_device_button.setText("-")
        self.remove_device_button.setText("删除")
        self.remove_device_button.setToolTip("删除当前设备配置")
        self.remove_device_button.setProperty("cssClass", "danger")
        self.save_device_button = QtWidgets.QToolButton()
        self.save_device_button.setText("保存")
        self.save_device_button.setToolTip("保存当前设备配置")
        self.save_device_button.setText("💾")

        self.save_device_button.setText("保存")
        device_btn_row = QtWidgets.QHBoxLayout()
        device_btn_row.setContentsMargins(0, 0, 0, 0)
        device_btn_row.setSpacing(4)
        device_btn_row.addWidget(self.add_device_button)
        device_btn_row.addWidget(self.remove_device_button)
        device_btn_row.addWidget(self.save_device_button)
        device_btn_row.addStretch()

        # -- Connection mode --
        self.usb_radio = QtWidgets.QRadioButton("USB")
        self.wifi_radio = QtWidgets.QRadioButton("WiFi")
        mode_layout = QtWidgets.QHBoxLayout()
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.addWidget(self.usb_radio)
        mode_layout.addWidget(self.wifi_radio)
        mode_layout.addStretch()

        # -- Fields --
        self.host_edit = QtWidgets.QLineEdit()
        self.host_edit.setPlaceholderText("10.11.99.1")
        self.device_type_combo = _rmtool.CompactComboBox(maximum_hint_width=220)
        for profile_name in _rmtool.DEVICE_PROFILES.keys():
            self.device_type_combo.addItem(_rmtool.DEVICE_PROFILE_LABELS.get(profile_name, profile_name), profile_name)
        self.device_type_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_type_combo.setMinimumContentsLength(12)
        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password_edit.setPlaceholderText("root 密码")
        self.remember_checkbox = QtWidgets.QCheckBox("记住密码")
        if _keyring() is None:
            self.remember_checkbox.setEnabled(False)
            self.remember_checkbox.setToolTip("未找到 keyring 库，无法安全保存密码。")

        # -- Buttons --
        self.connect_button = QtWidgets.QPushButton("连接")
        self.disconnect_button = QtWidgets.QPushButton("断开")
        self.disconnect_button.setProperty("cssClass", "danger")
        self.connect_button.setText("连接到当前设备")
        self.disconnect_button.setText("断开连接")
        button_layout = QtWidgets.QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(self.disconnect_button)

        # -- Sidebar vertical layout --
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(6)

        layout.addLayout(status_row)
        layout.addWidget(summary_card)
        layout.addSpacing(12)

        section_label = QtWidgets.QLabel("设备")
        section_label.setObjectName("sidebarSectionLabel")
        layout.addWidget(section_label)
        layout.addWidget(self.device_combo)
        layout.addLayout(device_btn_row)
        layout.addSpacing(8)

        layout.addLayout(mode_layout)
        layout.addSpacing(4)

        address_label = QtWidgets.QLabel("地址")
        address_label.setObjectName("sidebarSectionLabel")
        layout.addWidget(address_label)
        layout.addWidget(self.host_edit)
        layout.addSpacing(4)

        type_label = QtWidgets.QLabel("设备类型")
        type_label.setObjectName("sidebarSectionLabel")
        layout.addWidget(type_label)
        layout.addWidget(self.device_type_combo)
        layout.addSpacing(4)

        pw_label = QtWidgets.QLabel("密码")
        pw_label.setObjectName("sidebarSectionLabel")
        layout.addWidget(pw_label)
        layout.addWidget(self.password_edit)
        layout.addWidget(self.remember_checkbox)
        layout.addSpacing(8)

        layout.addLayout(button_layout)

        # -- Bottom area: theme toggle + branding --
        layout.addStretch()
        footer_actions = QtWidgets.QHBoxLayout()
        footer_actions.setContentsMargins(0, 0, 0, 0)
        footer_actions.setSpacing(10)
        footer_actions.addStretch()

        self.theme_button = QtWidgets.QToolButton()
        self.theme_button.setObjectName("themeToggle")
        self.theme_button.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self.theme_button.setIconSize(QtCore.QSize(18, 18))
        self.theme_button.setFixedSize(38, 38)
        self.theme_button.setCursor(QtCore.Qt.PointingHandCursor)
        footer_actions.addWidget(self.theme_button)

        self.log_button = QtWidgets.QToolButton()
        self.log_button.setObjectName("logViewerButton")
        self.log_button.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self.log_button.setIconSize(QtCore.QSize(18, 18))
        self.log_button.setFixedSize(38, 38)
        self.log_button.setCursor(QtCore.Qt.PointingHandCursor)
        self.log_button.setToolTip("查看运行日志")
        self.log_button.setAccessibleName("查看运行日志")
        footer_actions.addWidget(self.log_button)

        self.github_button = QtWidgets.QToolButton()
        self.github_button.setObjectName("githubLinkButton")
        self.github_button.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self.github_button.setIconSize(QtCore.QSize(18, 18))
        self.github_button.setFixedSize(38, 38)
        self.github_button.setCursor(QtCore.Qt.PointingHandCursor)
        self.github_button.setToolTip("打开 GitHub 仓库")
        self.github_button.setAccessibleName("打开 GitHub 仓库")
        footer_actions.addWidget(self.github_button)
        footer_actions.addStretch()

        layout.addLayout(footer_actions)
        layout.addSpacing(4)
        brand_label = QtWidgets.QLabel(_rmtool.APP_NAME)
        brand_label.setObjectName("sidebarBrand")
        brand_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(brand_label)

        self.setLayout(layout)

        self.disconnect_button.setEnabled(False)
        self.set_footer_theme("dark")

        self.device_combo.currentIndexChanged.connect(self._on_device_selected)
        self.add_device_button.clicked.connect(self._add_device)
        self.remove_device_button.clicked.connect(self._remove_device)
        self.save_device_button.clicked.connect(self._save_device)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.usb_radio.toggled.connect(self._emit_device_preview)
        self.wifi_radio.toggled.connect(self._emit_device_preview)
        self.github_button.clicked.connect(self._open_github_repo)
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

    def _device_by_name(self, name: str) -> Dict:
        for device in self.config.get("devices", []):
            if device["name"] == name:
                return device
        return {}

    def _current_device(self) -> Dict:
        return self._device_by_name(self.device_combo.currentText())

    def current_device(self) -> Dict:
        """Expose the currently selected device for other widgets."""

        return self._current_device().copy()

    def _current_device_type(self) -> str:
        return str(self.device_type_combo.currentData() or self.device_type_combo.currentText())

    def _device_type_display(self, device_type: str) -> str:
        return _rmtool.DEVICE_PROFILE_LABELS.get(device_type, device_type)

    def _select_device_type(self, device_type: str) -> None:
        idx = self.device_type_combo.findData(device_type)
        if idx == -1:
            idx = self.device_type_combo.findText(self._device_type_display(device_type))
        if idx != -1:
            self.device_type_combo.setCurrentIndex(idx)

    def _disconnect_if_device_target_changed(self, device: Dict) -> None:
        if not self.ssh_client.is_connected():
            return
        connected_info = getattr(self.ssh_client, "connection_info", {})
        connected_device = connected_info.get("device_name", "")
        connected_host = connected_info.get("host", "")
        device_name = device.get("name", "")
        device_host = device.get("host", "")
        if connected_device and connected_device != device_name:
            self.ssh_client.close()
        elif connected_host and connected_host != device_host:
            self.ssh_client.close()
        else:
            return
        QtWidgets.QMessageBox.information(
            self,
            _rmtool.APP_NAME,
            "已切换到其他设备，当前 SSH 连接已自动断开。\n请重新连接后再继续操作。",
        )

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
        self._select_device_type(device_type)
        password = self._load_password(device["name"])
        self.password_edit.setText(password)
        if _keyring():
            self.remember_checkbox.blockSignals(True)
            self.remember_checkbox.setChecked(bool(password))
            self.remember_checkbox.blockSignals(False)
        self._disconnect_if_device_target_changed(device)
        self.config["active_device"] = device["name"]
        _rmtool.save_config(self.config)
        self.status_message.emit(
            "info",
            f'已保存“{device["name"]}”的连接配置。',
            3000,
        )
        self._emit_device_preview()

    def _add_device(self):
        name, ok = QtWidgets.QInputDialog.getText(self, _rmtool.APP_NAME, "输入新设备名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        if any(device["name"] == name for device in self.config.get("devices", [])):
            QtWidgets.QMessageBox.warning(self, _rmtool.APP_NAME, "已存在同名设备。")
            return
        new_device = {
            "name": name,
            "mode": "usb",
            "host": "10.11.99.1",
            "type": "reMarkable Paper Pro",
        }
        self.config.setdefault("devices", []).append(new_device)
        _rmtool.save_config(self.config)
        self._populate_devices()
        idx = self.device_combo.findText(name)
        if idx != -1:
            self.device_combo.setCurrentIndex(idx)

    def _remove_device(self):
        if self.device_combo.count() <= 1:
            QtWidgets.QMessageBox.warning(self, _rmtool.APP_NAME, "至少保留一个设备配置。")
            return
        name = self.device_combo.currentText()
        confirm = QtWidgets.QMessageBox.question(
            self,
            _rmtool.APP_NAME,
            f'确定删除设备「{name}」的配置？',
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        self.config["devices"] = [d for d in self.config["devices"] if d["name"] != name]
        if _keyring():
            try:
                _keyring().delete_password(_rmtool.KEYRING_SERVICE, name)
            except Exception:  # pragma: no cover - backend dependent
                pass
        self.config["active_device"] = self.config["devices"][0]["name"]
        _rmtool.save_config(self.config)
        self._populate_devices()

    def _save_device(self):
        device = self._current_device()
        if not device:
            return
        device["mode"] = "usb" if self.usb_radio.isChecked() else "wifi"
        device["host"] = self.host_edit.text().strip()
        device["type"] = self._current_device_type()
        _rmtool.save_config(self.config)
        self.status_message.emit("info", f"已保存“{device['name']}”的连接配置。", 3000)
        self._emit_device_preview()

    # Credential helpers --------------------------------------------------------
    def _load_password(self, device_name: str) -> str:
        if not _keyring():
            return ""
        try:
            stored = _keyring().get_password(_rmtool.KEYRING_SERVICE, device_name)
            return stored or ""
        except Exception:  # pragma: no cover - backend specific
            logging.exception("Failed to load password from keyring")
            return ""

    def _store_password(self, device_name: str, password: str):
        if not _keyring():
            return
        try:
            _keyring().set_password(_rmtool.KEYRING_SERVICE, device_name, password)
        except Exception:  # pragma: no cover - backend specific
            logging.exception("Failed to store password in keyring")
            QtWidgets.QMessageBox.warning(
                self,
                _rmtool.APP_NAME,
                "无法保存密码到系统凭证管理器，请检查 keyring 配置。",
            )

    def _delete_password(self, device_name: str):
        if not _keyring():
            return
        try:
            stored = _keyring().get_password(_rmtool.KEYRING_SERVICE, device_name)
            if not stored:
                return
            _keyring().delete_password(_rmtool.KEYRING_SERVICE, device_name)
        except Exception:  # pragma: no cover - backend specific
            logging.exception("Failed to delete password from keyring")

    def _sync_password_preference(
        self, device_name: str, password: str, remember_password: bool
    ) -> None:
        if remember_password:
            self._store_password(device_name, password)
        else:
            self._delete_password(device_name)

    def _teardown_connection_progress(self):
        if self._connection_progress:
            self._connection_progress.close()
            self._connection_progress.deleteLater()
            self._connection_progress = None
        self._active_connection_worker = None
        if not self.ssh_client.is_connected():
            self.connect_button.setEnabled(True)

    def _begin_connection(
        self,
        host: str,
        password: str,
        remember_password: bool,
        trust_unknown_host: bool = False,
    ) -> None:
        device_name = self.device_combo.currentText()
        device_mode = "usb" if self.usb_radio.isChecked() else "wifi"
        self.connect_button.setEnabled(False)
        progress = QtWidgets.QProgressDialog("正在连接到设备…", "", 0, 0, self)
        progress.setWindowTitle(_rmtool.APP_NAME)
        progress.setWindowModality(QtCore.Qt.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setRange(0, 0)
        progress.show()
        self._connection_progress = progress

        worker = _rmtool.Worker(
            self.ssh_client.connect,
            host,
            password,
            trust_unknown_host=trust_unknown_host,
            device_name=device_name,
            connection_mode=device_mode,
        )

        def on_finished(_: object):
            self._teardown_connection_progress()
            device = self._device_by_name(device_name)
            if not device:
                return
            device["mode"] = device_mode
            device["host"] = host
            _rmtool.save_config(self.config)
            self._sync_password_preference(
                device["name"], password, remember_password
            )

        def on_error(exc: Exception):
            self._teardown_connection_progress()
            if isinstance(exc, UnknownHostKeyError):
                confirm = QtWidgets.QMessageBox.question(
                    self,
                    _rmtool.APP_NAME,
                    (
                        f"首次连接到 {exc.host}。\n"
                        f"SSH 指纹：{exc.fingerprint}\n\n"
                        "如果你确认这是自己的设备，可以信任并继续连接。"
                    ),
                )
                if confirm == QtWidgets.QMessageBox.Yes:
                    self._begin_connection(
                        host,
                        password,
                        remember_password,
                        trust_unknown_host=True,
                    )
                return
            logging.error("Unable to connect: %s", exc)
            QtWidgets.QMessageBox.critical(self, _rmtool.APP_NAME, f"连接失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._active_connection_worker = worker
        self.thread_pool.start(worker)

    def _connect(self):
        if self._active_connection_worker is not None:
            return
        host = self.host_edit.text().strip()
        password = self.password_edit.text().strip()
        if not host or not password:
            QtWidgets.QMessageBox.warning(
                self,
                _rmtool.APP_NAME,
                "请填写完整的连接信息（包括 root 密码）。",
            )
            return

        remember_password = self.remember_checkbox.isChecked()
        self._begin_connection(host, password, remember_password)

    def _disconnect(self):
        self.ssh_client.close()

    def _on_connection_changed(self, connected: bool):
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.status_text.setText("已连接" if connected else "未连接")
        self.status_dot.setProperty("connected", connected)
        # Force style refresh for the dynamic property
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
        self._refresh_device_summary()
        if connected:
            device_name = self._current_device().get("name", "当前设备")
            self.status_message.emit("success", f"已连接到“{device_name}”。", 3000)
            self.connected.emit()
        else:
            self.status_message.emit("info", "连接已断开。", 2500)
            self.disconnected.emit()

    def _emit_device_preview(self):
        device = self._current_device().copy()
        if not device:
            return
        device["mode"] = "usb" if self.usb_radio.isChecked() else "wifi"
        device["host"] = self.host_edit.text().strip()
        device["type"] = self._current_device_type()
        self._refresh_device_summary(device)
        self.device_changed.emit(device)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # pragma: no cover - layout reaction
        super().resizeEvent(event)
        self._update_connect_button_text()

    def _preferred_connect_button_text(self, device: Optional[Dict]) -> str:
        if not device:
            return "连接设备"
        device_name = device.get("name", "").strip()
        if not device_name:
            return "连接设备"
        if self.width() and self.width() < 360:
            return "连接设备"
        return f"连接 {device_name}"

    def _update_connect_button_text(self, device: Optional[Dict] = None) -> None:
        self.connect_button.setText(self._preferred_connect_button_text(device or self._current_device()))

    def _refresh_device_summary(self, device: Optional[Dict] = None) -> None:
        device = device or self._current_device()
        if not device:
            self.device_title_label.setText("未选择设备")
            self.device_meta_label.setText("选择一个设备后开始连接。")
            self.device_host_label.setText("地址：-")
            self._update_connect_button_text(None)
            return

        device_name = device.get("name", "未命名设备")
        mode_label = _rmtool.friendly_mode_label(device.get("mode", "usb"))
        device_type = device.get("type", "未知型号")
        host = device.get("host", "10.11.99.1")

        self.device_title_label.setText(device_name)
        self.device_meta_label.setText(f"{self._device_type_display(device_type)} · {mode_label}")
        self.device_host_label.setText(f"地址：{host}")
        self._update_connect_button_text(device)

    def set_footer_theme(self, theme: str) -> None:
        is_dark = theme == "dark"
        icon_color = "#C0C8E0" if is_dark else "#5A6070"
        target_theme_label = "亮色主题" if is_dark else "暗色主题"
        theme_icon = _rmtool._make_sidebar_icon("sun" if is_dark else "moon", icon_color)

        self.theme_button.setIcon(theme_icon)
        self.theme_button.setToolTip(f"切换到{target_theme_label}")
        self.theme_button.setAccessibleName(f"切换到{target_theme_label}")

        self.log_button.setIcon(_rmtool._make_sidebar_icon("log", icon_color))
        self.github_button.setIcon(_rmtool._make_sidebar_icon("github", icon_color))

    def _open_github_repo(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(_rmtool.GITHUB_REPO_URL))

