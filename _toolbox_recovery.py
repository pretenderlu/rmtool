"""Persistent capacity-only recovery section for the toolbox."""

from PyQt5 import QtCore, QtWidgets

import _capacity_recovery as recovery
import _toolbox_common as _ui
import rmtool as _rmtool


class CapacityRecoverySection(QtWidgets.QWidget):
    def __init__(self, ssh_client, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.diagnostics_handler = None
        self._status = None
        self._busy = False
        self._intent = None
        self._plan = None

        self.title_label = QtWidgets.QLabel("设备修复与清理")
        self.title_label.setObjectName("toolboxFeatureTitle")
        self.status_label = _ui.ToolboxStatusLabel(
            "选择要检查的功能，或从对应的容量错误进入。"
        )
        self.status_label.setWordWrap(True)
        self.capacity_label = QtWidgets.QLabel()
        self.capacity_label.setWordWrap(True)
        self.feature_combo = QtWidgets.QComboBox()
        self.feature_combo.addItem("拼音输入法", "pinyin-input")
        self.feature_combo.addItem("扫码传输", "qr-upload")
        self.home_only_checkbox = QtWidgets.QCheckBox(
            "解锁后保留当前字体，锁屏用默认字体"
        )
        self.home_only_checkbox.setChecked(False)
        self.home_only_checkbox.setVisible(False)
        self.detect_button = QtWidgets.QPushButton("开始检测")
        self.execute_button = QtWidgets.QPushButton("确认字体取舍并继续原安装")
        self.diagnostics_button = QtWidgets.QPushButton("保存诊断包并反馈")

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.capacity_label)
        layout.addWidget(self.feature_combo)
        layout.addWidget(self.home_only_checkbox)
        layout.addWidget(self.detect_button)
        layout.addWidget(self.execute_button)
        layout.addWidget(self.diagnostics_button)
        layout.addStretch()

        self.detect_button.clicked.connect(self._start_scan)
        self.execute_button.clicked.connect(self._execute)
        self.diagnostics_button.clicked.connect(self._show_diagnostics)
        self.home_only_checkbox.toggled.connect(self._update_buttons)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._update_buttons()

    @staticmethod
    def _capacity_text(context) -> str:
        if context is None:
            return ""
        return (
            f"系统存储安全可用 {context.available / 1048576:.2f} MiB；"
            f"继续安装需要 {context.required / 1048576:.2f} MiB（含安全余量）；"
            f"还差 {context.shortfall / 1048576:.2f} MiB。"
        )

    def _result_capacity_text(self, result) -> str:
        before, after = result.before_capacity, result.after_capacity
        if (
            before is not None
            and after is not None
            and before.kind == after.kind
            and before.partition == after.partition
            and before.required == after.required
        ):
            released = after.available - before.available
            return (
                "清理前：" + self._capacity_text(before)
                + "\n清理后：" + self._capacity_text(after)
                + f"\n实际释放 {released / 1048576:.2f} MiB（按实时容量探针计算）。"
            )
        return self._capacity_text(result.capacity)

    def _current_intent(self) -> bool:
        return (
            self._intent is not None
            and self.ssh_client.is_connected()
            and getattr(self.ssh_client, "_client", None) is self._intent.client
        )

    def _update_buttons(self, *_args):
        connected = self.ssh_client.is_connected()
        self.detect_button.setEnabled(connected and not self._busy)
        self.execute_button.setEnabled(
            self._current_intent()
            and not self._busy
            and self._plan is not None
            and self.home_only_checkbox.isChecked()
        )

    def open_intent(self, intent, error) -> None:
        if self._busy:
            return
        self._intent = intent
        self._plan = None
        self._status = None
        context = recovery.capacity_context(error)
        self.capacity_label.setText(self._capacity_text(context))
        self.home_only_checkbox.setChecked(False)
        self.home_only_checkbox.setVisible(False)
        self.status_label.setText("正在重新检查空间与可安全处理的字体选项…")
        self._update_buttons()
        if self._current_intent():
            self._start_scan()
        else:
            self.status_label.setText("原设备连接已变化，请重新检测当前设备。")

    def _start_scan(self):
        if self._busy or not self.ssh_client.is_connected():
            return
        if not self._current_intent():
            return self._start_fresh_intent()
        self._plan = None
        self.home_only_checkbox.setChecked(False)
        self.home_only_checkbox.setVisible(False)
        return _ui.start_plugin_worker(
            self,
            recovery.scan,
            self.ssh_client,
            self._intent,
            pending="正在只读检测空间与字体状态…",
            apply_status=self._apply_scan,
        )

    def _start_fresh_intent(self):
        self._plan = None
        self._fresh_ready = False
        self.home_only_checkbox.setChecked(False)
        self.home_only_checkbox.setVisible(False)
        return _ui.start_plugin_worker(
            self,
            recovery.fresh_intent,
            self.ssh_client,
            self.feature_combo.currentData(),
            str(_rmtool.app_state_dir()),
            pending="正在核对当前设备与精确资源包…",
            apply_status=self._apply_fresh_intent,
            on_done=self._finish_fresh_intent,
        )

    def _apply_fresh_intent(self, intent):
        self._intent = intent
        self._status = None
        self._fresh_ready = True

    def _finish_fresh_intent(self):
        if not self._fresh_ready:
            return
        self._fresh_ready = False
        self._start_scan()

    def _apply_scan(self, result):
        self._plan = None
        self.home_only_checkbox.setChecked(False)
        if isinstance(result, recovery.CapacityPlan):
            self._plan = result
            self.capacity_label.setText(self._capacity_text(result.capacity))
            self.status_label.setText(
                f"继续安装“{result.intent.feature_label}”前，可选择仅保留 home 字体。"
                "该操作会移除 rmtool 的锁屏字体镜像；书籍、文档和其他设置不会被删除。"
            )
            self.home_only_checkbox.setVisible(True)
        else:
            self._status = result
            self.status_label.setText(result.detail)
            self.capacity_label.setText(self._capacity_text(result.capacity))
            self.home_only_checkbox.setVisible(False)
        self._update_buttons()

    def _execute(self):
        plan = self._plan
        if plan is None or not self.home_only_checkbox.isChecked() or not self._current_intent():
            return
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将保留解锁后的当前字体，但密码锁屏界面改用默认字体。"
            "随后重新检查空间，只有满足原安装要求才继续；不会自动重启设备。是否继续？",
            confirm_text="确认锁屏取舍并继续",
            cancel_text="取消",
            danger=True,
        ):
            return
        if self._plan is not plan or not self._current_intent():
            self._plan = None
            self.status_label.setText("设备或计划已变化，请重新检测。")
            self._update_buttons()
            return
        return _ui.start_plugin_worker(
            self,
            recovery.execute,
            self.ssh_client,
            plan,
            pending="正在重验字体计划、调整布局并重新检查原安装…",
            apply_status=self._apply_result,
        )

    def _apply_result(self, result):
        self._status = result
        self._plan = None
        self.home_only_checkbox.setChecked(False)
        self.home_only_checkbox.setVisible(False)
        self.status_label.setText(result.detail)
        self.capacity_label.setText(self._result_capacity_text(result))
        self._update_buttons()
        if result.state == "deployed" and self._current_intent():
            self.ssh_client.close()

    def _show_diagnostics(self):
        if self.diagnostics_handler is not None:
            self.diagnostics_handler()

    def _on_connection_changed(self, _connected):
        self._plan = None
        self.home_only_checkbox.setChecked(False)
        self.home_only_checkbox.setVisible(False)
        if self._status is not None and self._status.state == "deployed":
            self._update_buttons()
            return
        if self._intent is not None and not self._current_intent():
            self.status_label.setText("设备连接已变化，旧计划已失效；请重新检测当前设备。")
        self._update_buttons()
