"""Device clock and control widgets."""


from PyQt5 import QtWidgets

from _ssh import SSHClientWrapper, require_connection
from _ui_tasks import run_in_background
import _device_actions
# Dialog helpers are looked up on _toolbox_common at call time so a single
# patch point covers every toolbox section.
import _toolbox_common as _ui
import rmtool as _rmtool  # late-bound access to avoid circular import


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

    def _time_buttons(self):
        return (self.sync_button, self.info_button, self.tz_button)

    @require_connection
    def _sync_time(self):
        run_in_background(
            self,
            _device_actions.sync_time,
            self.ssh_client,
            on_success=lambda stamp: self._append_output(f"已同步设备时间到 {stamp}"),
            error_prefix="同步失败",
            log_message="Sync time failed",
            disable=self._time_buttons(),
        )

    @require_connection
    def _show_time_info(self):
        def show(rows):
            for title, text in rows:
                self._append_output(f"[{title}]\n{text}\n")

        run_in_background(
            self,
            _device_actions.time_info,
            self.ssh_client,
            on_success=show,
            error_prefix="查询失败",
            log_message="Get time info failed",
            disable=self._time_buttons(),
        )

    @require_connection
    def _set_timezone(self):
        run_in_background(
            self,
            _device_actions.set_timezone,
            self.ssh_client,
            on_success=lambda tz: self._append_output(f"已将时区设置为 {tz}"),
            error_prefix="设置失败",
            log_message="Set timezone failed",
            disable=self._time_buttons(),
        )


class ControlTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client

        self.restart_button = QtWidgets.QPushButton("重启设备")
        self.restart_button.setProperty("btnRole", "danger")
        self.enable_wifi_ssh_button = QtWidgets.QPushButton("开启 Wi-Fi SSH 通道")
        self.brightness_button = QtWidgets.QPushButton("提升前光亮度")

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

    def _control_buttons(self):
        return (self.restart_button, self.enable_wifi_ssh_button, self.brightness_button)

    @require_connection
    def _restart_device(self):
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "确定要重启设备吗？这将断开连接。",
            confirm_text="重启",
            cancel_text="取消",
            danger=True,
        ):
            return
        run_in_background(
            self,
            _device_actions.reboot,
            self.ssh_client,
            on_success=lambda _: _ui.show_info(self, _rmtool.APP_NAME, "已发送重启命令。"),
            error_prefix="重启失败",
            log_message="Restart failed",
            disable=self._control_buttons(),
        )

    @require_connection
    def _enable_wifi_ssh(self):
        run_in_background(
            self,
            _device_actions.enable_wifi_ssh,
            self.ssh_client,
            on_success=lambda _: _ui.show_info(
                self,
                _rmtool.APP_NAME,
                "已开启 Wi-Fi SSH，请在断开 USB 后使用 WLAN 地址连接。",
            ),
            error_prefix="操作失败",
            log_message="Enable Wi-Fi SSH failed",
            disable=self._control_buttons(),
        )

    @require_connection
    def _increase_brightness(self):
        run_in_background(
            self,
            _device_actions.increase_brightness,
            self.ssh_client,
            on_success=lambda _: _ui.show_info(self, _rmtool.APP_NAME, "前光亮度已调整。"),
            error_prefix="设置失败",
            log_message="Brightness tweak failed",
            disable=self._control_buttons(),
        )
