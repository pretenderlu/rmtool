"""Read-only diagnostics export section."""

import logging
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets, sip

import _diagnostics
from _ssh import SSHClientWrapper, require_connection
# Dialog helpers are looked up on _toolbox_common at call time so a single
# patch point covers every toolbox section.
import _toolbox_common as _ui
import rmtool as _rmtool  # late-bound access to avoid circular import
from _toolbox_common import ToolboxStatusLabel


def _collect_diagnostics(ssh_client, log_path):
    return _diagnostics.collect(ssh_client, log_path)


class DiagnosticPreviewDialog(QtWidgets.QDialog):
    """Shows exactly what a diagnostic bundle will contain before saving."""

    def __init__(self, collected, parent=None):
        super().__init__(parent)
        self.setWindowTitle("诊断包预览")
        self.resize(760, 560)
        layout = QtWidgets.QVBoxLayout(self)

        hint = QtWidgets.QLabel(
            "以下是将写入诊断包的全部内容。带勾选的可选条目可能包含本地路径、"
            "文档名或字体名，如不希望提供可取消勾选。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.optional_boxes = {}
        for result in collected:
            if not result.item.optional:
                continue
            box = QtWidgets.QCheckBox(f"包含：{result.item.description}")
            box.setChecked(True)
            self.optional_boxes[result.item.name] = box
            layout.addWidget(box)

        preview = QtWidgets.QPlainTextEdit(self._preview_text(collected))
        preview.setReadOnly(True)
        preview.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        font = QtGui.QFont("Consolas")
        font.setStyleHint(QtGui.QFont.Monospace)
        preview.setFont(font)
        layout.addWidget(preview)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save
            | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Save).setText("保存诊断包…")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _preview_text(collected) -> str:
        chunks = []
        for result in collected:
            header = f"===== {result.item.name}（{result.item.description}）"
            if result.error:
                header += f" [采集失败：{result.error}]"
            elif result.truncated:
                header += " [已截断]"
            chunks.append(header + " =====")
            chunks.append(result.text.rstrip())
            chunks.append("")
        return "\n".join(chunks)

    def include_optional(self, name: str) -> bool:
        box = self.optional_boxes.get(name)
        return box is None or box.isChecked()


class DiagnosticsSection(QtWidgets.QWidget):
    """One-click read-only diagnostic bundle export for user support."""

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._busy = False

        title = QtWidgets.QLabel("诊断日志导出")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "把 rmtool 本机日志和设备上的只读诊断信息（版本、服务状态、"
            "rmtool Xovi 启动日志、共享目录清单等）打包成一个 zip，"
            "供你发给维护者定位问题。导出前会展示全部内容供确认；"
            "可能包含本地路径、文档名或字体名的条目可以单独取消。"
            "设备凭据永远不会进入诊断包。"
        )
        detail.setWordWrap(True)

        self.status_label = ToolboxStatusLabel("设备已连接，尚未导出")
        self.status_label.setWordWrap(True)

        self.export_button = QtWidgets.QPushButton("生成诊断包…")
        self.export_button.setProperty("btnRole", "primary")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        buttons.addWidget(self.export_button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.export_button.clicked.connect(self._export)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self.status_label.setText("设备未连接")
        elif not self._busy:
            self.status_label.setText("设备已连接，尚未导出")
        self.export_button.setEnabled(connected and not self._busy)

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self.export_button.setEnabled(self.ssh_client.is_connected() and not busy)

    @require_connection
    def _export(self):
        self._set_busy(True, "正在通过 SSH 采集只读诊断信息…")
        worker = _rmtool.Worker(
            _collect_diagnostics,
            self.ssh_client,
            _rmtool.app_state_dir() / "remarkable_tool.log",
        )

        def on_finished(collected):
            if sip.isdeleted(self):
                return
            self._set_busy(False, "采集完成，请预览并保存诊断包")
            self._preview_and_save(collected)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Diagnostics collection failed after tab close: %s", exc)
                return
            self._set_busy(False, "诊断信息采集失败，未生成文件")
            logging.error("Diagnostics collection failed: %s", exc)
            _ui.show_error(self, _rmtool.APP_NAME, f"诊断信息采集失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _preview_and_save(self, collected):
        dialog = DiagnosticPreviewDialog(collected, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            self.status_label.setText("已取消，未生成诊断包")
            return
        default_name = _diagnostics.bundle_name(
            _diagnostics.platform_label(collected)
        )
        target, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存诊断包", default_name, "诊断包 (*.zip)"
        )
        if not target:
            self.status_label.setText("已取消，未生成诊断包")
            return
        included = [
            result
            for result in collected
            if dialog.include_optional(result.item.name)
        ]
        try:
            saved = _diagnostics.write_bundle(target, collected, included)
        except Exception as exc:
            logging.error("Diagnostic bundle write failed: %s", exc)
            _ui.show_error(self, _rmtool.APP_NAME, f"诊断包写入失败：{exc}")
            return
        self.status_label.setText(f"诊断包已保存：{saved}")
        _ui.show_info(
            self,
            _rmtool.APP_NAME,
            "诊断包已保存：\n"
            f"{saved}\n\n"
            "请把这个文件发给维护者以协助定位问题。"
            "包内不包含任何设备密码。",
        )
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(Path(saved).parent))
        )
