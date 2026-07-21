"""ConnectionWidget extracted from rmtool.py."""

import logging
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from _dialogs import ask_confirmation, show_error, show_info, show_warning
from _ssh import SSHClientWrapper, UnknownHostKeyError
import rmtool as _rmtool  # late-bound access to avoid circular import


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
        _rmtool.normalise_config(self.config)

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
        self.remove_device_button.setProperty("btnRole", "danger")
        self.edit_device_button = QtWidgets.QToolButton()
        self.edit_device_button.setText("编辑")
        self.edit_device_button.setToolTip("编辑当前设备配置")
        device_btn_row = QtWidgets.QHBoxLayout()
        device_btn_row.setContentsMargins(0, 0, 0, 0)
        device_btn_row.setSpacing(4)
        device_btn_row.addWidget(self.add_device_button)
        device_btn_row.addWidget(self.edit_device_button)
        device_btn_row.addWidget(self.remove_device_button)
        device_btn_row.addStretch()

        self.credential_status_label = QtWidgets.QLabel("未保存")
        self.credential_status_label.setObjectName("credentialStatusLabel")
        self.credential_status_label.setWordWrap(True)
        self.forget_password_button = QtWidgets.QToolButton()
        self.forget_password_button.setObjectName("forgetPasswordButton")
        self.forget_password_button.setText("忘记密码")
        self.forget_password_button.setToolTip("删除当前设备已保存的 root 密码")
        self.forget_password_button.setEnabled(False)
        credential_status_row = QtWidgets.QHBoxLayout()
        credential_status_row.setContentsMargins(0, 0, 0, 0)
        credential_status_row.setSpacing(8)
        credential_status_row.addWidget(self.credential_status_label, 1)
        credential_status_row.addWidget(self.forget_password_button)

        # -- Buttons (stacked so both keep their full text at sidebar width) --
        self.connect_button = QtWidgets.QPushButton("连接设备")
        self.disconnect_button = QtWidgets.QPushButton("断开连接")
        self.connect_button.setProperty("btnRole", "primary")
        self.disconnect_button.setProperty("btnRole", "danger")
        button_layout = QtWidgets.QVBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(8)
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

        credential_label = QtWidgets.QLabel("凭证")
        credential_label.setObjectName("sidebarSectionLabel")
        layout.addWidget(credential_label)
        layout.addLayout(credential_status_row)
        layout.addSpacing(8)

        layout.addLayout(button_layout)

        # -- Bottom area: theme toggle + branding --
        # Extra sidebar sections (e.g. page navigation) are inserted above
        # this stretch so the footer stays pinned to the bottom.
        self._footer_stretch_index = layout.count()
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
        self.edit_device_button.clicked.connect(self._edit_device)
        self.remove_device_button.clicked.connect(self._remove_device)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.github_button.clicked.connect(self._open_github_repo)
        self.forget_password_button.clicked.connect(self._forget_saved_password)
        ssh_client.connection_changed.connect(self._on_connection_changed)

        self._populate_devices()

    # Device management helpers -------------------------------------------------
    def _populate_devices(self):
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in self.config.get("devices", []):
            self.device_combo.addItem(device["name"], device.get("id", ""))

        has_device = bool(self.device_combo.count())
        self.edit_device_button.setEnabled(has_device)
        self.remove_device_button.setEnabled(has_device)
        self.connect_button.setEnabled(
            has_device
            and not self.ssh_client.is_connected()
            and self._active_connection_worker is None
        )
        if not has_device:
            self.device_combo.blockSignals(False)
            self.config["active_device_id"] = ""
            self.config["active_device"] = ""
            self._refresh_device_summary()
            self._set_credential_status("未保存", False)
            return

        idx = -1
        active_id = self.config.get("active_device_id", "")
        if active_id:
            idx = self.device_combo.findData(active_id)
        if idx == -1 and self.config.get("active_device"):
            idx = self.device_combo.findText(self.config["active_device"])
        if idx == -1:
            idx = 0
        self.device_combo.setCurrentIndex(idx)
        self.device_combo.blockSignals(False)
        self._on_device_selected(idx)

    def _device_by_id(self, device_id: str) -> Dict:
        return _rmtool.find_device_by_id(self.config, device_id)

    def _device_by_name(self, name: str) -> Dict:
        for device in self.config.get("devices", []):
            if device["name"] == name:
                return device
        return {}

    def _current_device(self) -> Dict:
        device_id = self.device_combo.currentData()
        if device_id:
            device = self._device_by_id(str(device_id))
            if device:
                return device
        return self._device_by_name(self.device_combo.currentText())

    def current_device(self) -> Dict:
        """Expose the currently selected device for other widgets."""

        return self._current_device().copy()

    def _device_type_display(self, device_type: str) -> str:
        return _rmtool.DEVICE_PROFILE_LABELS.get(device_type, device_type)

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
        show_info(
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
        password = self._load_password(device)
        self._set_credential_status(
            "已保存到项目本地文件" if password else "未保存", bool(password)
        )
        self._disconnect_if_device_target_changed(device)
        active_changed = (
            self.config.get("active_device_id") != device["id"]
            or self.config.get("active_device") != device["name"]
        )
        self.config["active_device_id"] = device["id"]
        self.config["active_device"] = device["name"]
        if active_changed:
            _rmtool.save_config(self.config)
        self.status_message.emit(
            "info",
            f'已保存“{device["name"]}”的连接配置。',
            3000,
        )
        self._emit_device_preview()

    def _add_device(self):
        details = self._request_new_device()
        if not details:
            return
        name = details.get("name", "").strip()
        if not name:
            show_warning(self, _rmtool.APP_NAME, "请输入设备名称。")
            return
        if any(device["name"] == name for device in self.config.get("devices", [])):
            show_warning(self, _rmtool.APP_NAME, "已存在同名设备。")
            return
        new_device = {
            "id": _rmtool.new_device_id(),
            "name": name,
            "mode": details.get("mode", "usb"),
            "host": details.get("host", "10.11.99.1").strip() or "10.11.99.1",
            "type": details.get("type", "reMarkable Paper Pro"),
        }
        password = details.get("password", "")
        remember_password = bool(details.get("remember_password"))
        self._sync_password_preference(
            new_device, password, remember_password, persist=False
        )
        self.config.setdefault("devices", []).append(new_device)
        self.config["active_device_id"] = new_device["id"]
        self.config["active_device"] = new_device["name"]
        _rmtool.save_config(self.config)
        self._populate_devices()
        self._emit_device_preview()

    def _make_device_details_dialog(
        self, title: str, initial: Optional[Dict] = None
    ):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setModal(True)
        initial = initial or {}

        name_edit = QtWidgets.QLineEdit()
        name_edit.setPlaceholderText("例如：我的 Paper Pro")
        name_edit.setText(initial.get("name", ""))
        usb_radio = QtWidgets.QRadioButton("USB")
        wifi_radio = QtWidgets.QRadioButton("WiFi")
        mode = initial.get("mode", "usb")
        usb_radio.setChecked(mode != "wifi")
        wifi_radio.setChecked(mode == "wifi")
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(usb_radio)
        mode_row.addWidget(wifi_radio)
        mode_row.addStretch()

        host_edit = QtWidgets.QLineEdit(initial.get("host", "10.11.99.1"))
        type_combo = _rmtool.CompactComboBox(maximum_hint_width=260)
        for profile_name in _rmtool.DEVICE_PROFILES.keys():
            type_combo.addItem(
                _rmtool.DEVICE_PROFILE_LABELS.get(profile_name, profile_name),
                profile_name,
            )
        device_type = initial.get("type", "reMarkable Paper Pro")
        type_index = type_combo.findData(device_type)
        if type_index == -1:
            type_index = type_combo.findText(self._device_type_display(device_type))
        if type_index != -1:
            type_combo.setCurrentIndex(type_index)
        password_edit = QtWidgets.QLineEdit()
        password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        password_edit.setPlaceholderText("root 密码")
        saved_password = self._load_password(initial) if initial else ""
        password_edit.setText(saved_password)
        remember_checkbox = QtWidgets.QCheckBox("记住密码")
        remember_checkbox.setChecked(bool(saved_password) or not initial)
        remember_checkbox.setToolTip("将 root 密码保存到项目本地文件。")

        form = QtWidgets.QFormLayout()
        form.setContentsMargins(18, 18, 18, 12)
        form.setSpacing(10)
        form.addRow("设备名称", name_edit)
        form.addRow("连接方式", mode_row)
        form.addRow("地址", host_edit)
        form.addRow("设备类型", type_combo)
        form.addRow("root 密码", password_edit)
        form.addRow("", remember_checkbox)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Save).setText("保存设备")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        for role in (QtWidgets.QDialogButtonBox.Save, QtWidgets.QDialogButtonBox.Cancel):
            button = buttons.button(role)
            if button:
                button.setMinimumHeight(36)
                button.setMinimumWidth(96)

        button_frame = QtWidgets.QWidget()
        button_frame.setObjectName("deviceDialogButtonFrame")
        button_layout = QtWidgets.QHBoxLayout(button_frame)
        button_layout.setContentsMargins(18, 8, 18, 18)
        button_layout.addWidget(buttons)

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(form)
        layout.addWidget(button_frame)

        controls = {
            "name": name_edit,
            "usb": usb_radio,
            "wifi": wifi_radio,
            "host": host_edit,
            "type": type_combo,
            "password": password_edit,
            "remember": remember_checkbox,
        }
        return dialog, controls

    def _request_device_details(
        self, title: str, initial: Optional[Dict] = None
    ) -> Optional[Dict]:
        dialog, controls = self._make_device_details_dialog(title, initial)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None

        return {
            "name": controls["name"].text(),
            "mode": "usb" if controls["usb"].isChecked() else "wifi",
            "host": controls["host"].text(),
            "type": str(controls["type"].currentData() or controls["type"].currentText()),
            "password": controls["password"].text(),
            "remember_password": controls["remember"].isChecked(),
        }

    def _request_new_device(self) -> Optional[Dict]:
        return self._request_device_details("新增设备")

    def _request_edit_device(self, device: Dict) -> Optional[Dict]:
        return self._request_device_details("编辑设备", device)

    def _remove_device(self):
        device = self._current_device()
        if not device:
            return
        name = device.get("name", self.device_combo.currentText())
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f'确定删除设备「{name}」的配置？',
            confirm_text="删除",
            cancel_text="取消",
            danger=True,
        ):
            return
        if self.ssh_client.is_connected():
            self.ssh_client.close()
        self.config["devices"] = [
            d for d in self.config["devices"] if d.get("id") != device.get("id")
        ]
        if self.config["devices"]:
            self.config["active_device_id"] = self.config["devices"][0]["id"]
            self.config["active_device"] = self.config["devices"][0]["name"]
        else:
            self.config["active_device_id"] = ""
            self.config["active_device"] = ""
        _rmtool.save_config(self.config)
        self._populate_devices()

    def _edit_device(self):
        device = self._current_device()
        if not device:
            return
        details = self._request_edit_device(device)
        if not details:
            return
        name = details.get("name", "").strip()
        if not name:
            show_warning(self, _rmtool.APP_NAME, "请输入设备名称。")
            return
        device_id = device.get("id", "")
        for existing in self.config.get("devices", []):
            if existing.get("id") != device_id and existing.get("name") == name:
                show_warning(self, _rmtool.APP_NAME, "已存在同名设备。")
                return
        password = details.get("password", "")
        remember_password = bool(details.get("remember_password"))
        if remember_password and not password:
            show_warning(
                self,
                _rmtool.APP_NAME,
                "要记住密码，请先填写 root 密码。",
            )
            saved_password = self._load_password(device)
            self._set_credential_status(
                "已保存到项目本地文件" if saved_password else "未保存",
                bool(saved_password),
            )
            return
        device["name"] = name
        device["mode"] = details.get("mode", "usb")
        device["host"] = details.get("host", "10.11.99.1").strip() or "10.11.99.1"
        device["type"] = details.get("type", "reMarkable Paper Pro")
        self.config["active_device_id"] = device["id"]
        self.config["active_device"] = device["name"]
        self._sync_password_preference(
            device, password, remember_password, persist=False
        )
        _rmtool.save_config(self.config)
        current_index = self.device_combo.currentIndex()
        if current_index >= 0:
            self.device_combo.blockSignals(True)
            self.device_combo.setItemText(current_index, device["name"])
            self.device_combo.setItemData(current_index, device["id"])
            self.device_combo.blockSignals(False)
        self.status_message.emit("info", f"已保存“{device['name']}”的连接配置。", 3000)
        self._emit_device_preview()

    # Credential helpers --------------------------------------------------------
    def _set_credential_status(self, text: str, can_forget: bool) -> None:
        self.credential_status_label.setText(text)
        self.forget_password_button.setEnabled(can_forget)

    def _load_password(self, device: Dict) -> str:
        return str(device.get("password", ""))

    def _store_password(
        self, device: Dict, password: str, *, persist: bool = True
    ) -> bool:
        if not password:
            show_warning(
                self,
                _rmtool.APP_NAME,
                "要记住密码，请先填写 root 密码。",
            )
            self._set_credential_status("未保存", False)
            return False
        device["password"] = password
        if persist:
            _rmtool.save_config(self.config)
        self._set_credential_status("已保存到项目本地文件", True)
        return True

    def _delete_password(self, device: Dict, *, persist: bool = True):
        if "password" in device:
            device.pop("password")
            if persist:
                _rmtool.save_config(self.config)
        self._set_credential_status("未保存", False)

    def _sync_password_preference(
        self,
        device: Dict,
        password: str,
        remember_password: bool,
        *,
        persist: bool = True,
    ) -> bool:
        if remember_password:
            return self._store_password(device, password, persist=persist)
        self._delete_password(device, persist=persist)
        return True

    def _forget_saved_password(self) -> None:
        device = self._current_device()
        if not device:
            return
        self._delete_password(device)
        self.status_message.emit("info", f"已忘记“{device['name']}”的已保存密码。", 3000)

    def _teardown_connection_progress(self):
        if self._connection_progress:
            self._connection_progress.close()
            self._connection_progress.deleteLater()
            self._connection_progress = None
        self._active_connection_worker = None
        self.connect_button.setEnabled(
            not self.ssh_client.is_connected() and bool(self._current_device())
        )

    def _begin_connection(
        self,
        host: str,
        password: str,
        remember_password: bool,
        trust_unknown_host: bool = False,
    ) -> None:
        device = self._current_device()
        device_id = device.get("id", "")
        device_name = device.get("name", self.device_combo.currentText())
        device_mode = device.get("mode", "usb")
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
            device_id=device_id,
            device_name=device_name,
        )

        def on_finished(_: object):
            self._teardown_connection_progress()
            device = self._device_by_id(device_id) or self._device_by_name(device_name)
            if not device:
                return
            device["mode"] = device_mode
            device["host"] = host
            self._sync_password_preference(
                device, password, remember_password, persist=False
            )
            _rmtool.save_config(self.config)

        def on_error(exc: Exception):
            self._teardown_connection_progress()
            if isinstance(exc, UnknownHostKeyError):
                if exc.key_changed:
                    message = (
                        f"{exc.host} 的 SSH 指纹已变化。\n"
                        f"新 SSH 指纹：{exc.fingerprint}\n\n"
                        "请确认设备身份；继续操作将重新信任并连接。"
                    )
                    confirm_text = "重新信任并连接"
                else:
                    message = (
                        f"首次连接到 {exc.host}。\n"
                        f"SSH 指纹：{exc.fingerprint}\n\n"
                        "如果你确认这是自己的设备，可以信任并继续连接。"
                    )
                    confirm_text = "信任并连接"
                confirmed = ask_confirmation(
                    self,
                    _rmtool.APP_NAME,
                    message,
                    confirm_text=confirm_text,
                    cancel_text="取消",
                )
                if confirmed:
                    self._begin_connection(
                        host,
                        password,
                        remember_password,
                        trust_unknown_host=True,
                    )
                return
            logging.error("Unable to connect: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"连接失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._active_connection_worker = worker
        self.thread_pool.start(worker)

    def _make_password_dialog(self, device: Dict):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("连接设备")
        dialog.setModal(True)

        password_edit = QtWidgets.QLineEdit()
        password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        password_edit.setPlaceholderText("root 密码")
        remember_checkbox = QtWidgets.QCheckBox("记住密码")
        remember_checkbox.setChecked(True)
        remember_checkbox.setToolTip("将 root 密码保存到项目本地文件。")

        form = QtWidgets.QFormLayout()
        form.setContentsMargins(18, 18, 18, 12)
        form.setSpacing(10)
        form.addRow(f"{device.get('name', '当前设备')} root 密码", password_edit)
        form.addRow("", remember_checkbox)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("连接设备")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        for role in (QtWidgets.QDialogButtonBox.Ok, QtWidgets.QDialogButtonBox.Cancel):
            button = buttons.button(role)
            if button:
                button.setMinimumHeight(36)
                button.setMinimumWidth(96)

        button_frame = QtWidgets.QWidget()
        button_frame.setObjectName("passwordDialogButtonFrame")
        button_layout = QtWidgets.QHBoxLayout(button_frame)
        button_layout.setContentsMargins(18, 8, 18, 18)
        button_layout.addWidget(buttons)

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(form)
        layout.addWidget(button_frame)

        return dialog, {"password": password_edit, "remember": remember_checkbox}

    def _request_connection_password(self, device: Dict) -> Optional[Dict]:
        dialog, controls = self._make_password_dialog(device)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return {
            "password": controls["password"].text(),
            "remember_password": controls["remember"].isChecked(),
        }

    def _connect(self):
        if self._active_connection_worker is not None:
            return
        device = self._current_device()
        if not device:
            show_warning(self, _rmtool.APP_NAME, "请先选择设备。")
            return
        host = device.get("host", "").strip()
        password = self._load_password(device)
        remember_password = bool(password)
        if not password:
            details = self._request_connection_password(device)
            if not details:
                return
            password = details.get("password", "")
            remember_password = bool(details.get("remember_password"))
        if not host or not password:
            show_warning(
                self,
                _rmtool.APP_NAME,
                "请填写完整的连接信息（包括 root 密码）。",
            )
            return

        self._begin_connection(host, password, remember_password)

    def _disconnect(self):
        self.ssh_client.close()

    def _on_connection_changed(self, connected: bool):
        self.connect_button.setEnabled(
            not connected
            and self._active_connection_worker is None
            and bool(self._current_device())
        )
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
        self._refresh_device_summary(device)
        self.device_changed.emit(device)

    def _refresh_device_summary(self, device: Optional[Dict] = None) -> None:
        device = device or self._current_device()
        if not device:
            self.device_title_label.setText("未选择设备")
            self.device_meta_label.setText("选择一个设备后开始连接。")
            self.device_host_label.setText("地址：-")
            return

        device_name = device.get("name", "未命名设备")
        mode_label = _rmtool.friendly_mode_label(device.get("mode", "usb"))
        device_type = device.get("type", "未知型号")
        host = device.get("host", "10.11.99.1")

        self.device_title_label.setText(device_name)
        self.device_meta_label.setText(f"{self._device_type_display(device_type)} · {mode_label}")
        self.device_host_label.setText(f"地址：{host}")

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

    def add_sidebar_section(self, widget: QtWidgets.QWidget) -> None:
        """Insert a sidebar section above the pinned footer row."""
        self.layout().insertWidget(self._footer_stretch_index, widget)
        self._footer_stretch_index += 1

    def _open_github_repo(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(_rmtool.GITHUB_REPO_URL))
