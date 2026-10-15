"""Read-only diagnostics export section."""

import logging
from pathlib import Path
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets, sip

import _diagnostics
from _ssh import SSHClientWrapper, require_connection
# Dialog helpers are looked up on _toolbox_common at call time so a single
# patch point covers every toolbox section.
import _toolbox_common as _ui
import rmtool as _rmtool  # late-bound access to avoid circular import
from _toolbox_common import ToolboxStatusLabel


def _collect_diagnostics(ssh_client, log_path, include_device=True):
    return _diagnostics.collect(ssh_client, log_path, include_device=include_device)


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


FEEDBACK_STEPS = ("采集诊断信息", "确认内容", "保存诊断包", "发送给维护者")


class FeedbackGuide(QtWidgets.QFrame):
    """Step banner shown while the user reports a problem."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("feedbackGuide")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self._fit_pending = False
        self._fitted = {}
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self.title_label = QtWidgets.QLabel("反馈问题")
        self.title_label.setObjectName("feedbackGuideTitle")
        self.error_label = QtWidgets.QLabel()
        self.error_label.setObjectName("feedbackGuideError")
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        steps = QtWidgets.QHBoxLayout()
        steps.setContentsMargins(0, 0, 0, 0)
        steps.setSpacing(6)
        self.step_labels = []
        for index, text in enumerate(FEEDBACK_STEPS, start=1):
            label = QtWidgets.QLabel(f"{index}. {text}")
            label.setObjectName("feedbackGuideStep")
            label.setProperty("state", "pending")
            self.step_labels.append(label)
            steps.addWidget(label)
        steps.addStretch()

        self.hint_label = QtWidgets.QLabel()
        self.hint_label.setObjectName("feedbackGuideHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        # Wrapped labels report height-for-width; Minimum lets the banner grow
        # to the full text instead of clipping the last line.
        self.hint_label.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum
        )

        actions = QtWidgets.QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(_rmtool.SUBSECTION_GAP)
        self.retry_button = QtWidgets.QPushButton("重新生成")
        self.open_folder_button = QtWidgets.QPushButton("打开所在文件夹")
        self.issue_button = QtWidgets.QPushButton("前往 GitHub 提交问题")
        self.issue_button.setProperty("btnRole", "primary")
        self.close_button = QtWidgets.QPushButton("结束反馈")
        for button in (self.retry_button, self.open_folder_button, self.issue_button):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.close_button)

        layout.addWidget(self.title_label)
        layout.addWidget(self.error_label)
        layout.addLayout(steps)
        layout.addWidget(self.hint_label)
        layout.addLayout(actions)

    def _wrapped_labels(self):
        return (self.error_label, self.hint_label)

    def _fit_wrapped_labels(self) -> None:
        # QScrollArea sizes its content from minimumSizeHint, which measures
        # word-wrapped labels at a guessed width; pin each label's height to
        # its real width so the last line and the buttons are never clipped.
        # Deferred and keyed on (width, text) so a scrollbar appearing and
        # narrowing the label cannot make this oscillate within one event.
        if not self._fit_pending:
            self._fit_pending = True
            QtCore.QTimer.singleShot(0, self._apply_label_fit)

    def _apply_label_fit(self) -> None:
        if sip.isdeleted(self):
            return
        self._fit_pending = False
        for label in self._wrapped_labels():
            if not label.isVisibleTo(self) or label.width() <= 0:
                continue
            key = (label.width(), label.text())
            if self._fitted.get(label.objectName()) == key:
                continue
            self._fitted[label.objectName()] = key
            needed = label.heightForWidth(label.width())
            if needed > 0:
                label.setMinimumHeight(needed)

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_wrapped_labels()

    def set_error(self, message: str) -> None:
        text = message.strip()
        if len(text) > 300:
            text = text[:300] + "…"
        self.error_label.setText(f"报错：{text}" if text else "")
        self.error_label.setVisible(bool(text))

    def set_step(self, current: int, hint: str, *, failed: bool = False) -> None:
        """Mark steps before ``current`` done; ``current`` is 0-based."""
        for index, label in enumerate(self.step_labels):
            if index < current:
                state = "done"
            elif index == current:
                state = "failed" if failed else "active"
            else:
                state = "pending"
            label.setProperty("state", state)
            label.style().unpolish(label)
            label.style().polish(label)
        self.hint_label.setText(hint)
        finished = current >= len(FEEDBACK_STEPS) - 1 and not failed
        self.issue_button.setVisible(finished)
        self.open_folder_button.setVisible(finished)
        # Also offered after saving: a user who was disconnected can connect
        # and regenerate a bundle that includes the device items.
        self.retry_button.setVisible(failed or finished)
        self._fit_wrapped_labels()
        self.updateGeometry()


class DiagnosticsSection(QtWidgets.QWidget):
    """One-click read-only diagnostic bundle export for user support.

    Also hosts the guided "反馈问题" flow started from error dialogs: it
    collects automatically, previews, saves the zip, then points the user to
    where to send it.  Without a connection only PC-side items are collected.
    """

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._busy = False
        # True from collection start until the preview/save step finishes;
        # covers the modal preview and file dialogs, where _busy is False.
        self._flow_running = False
        self._feedback_active = False
        self._feedback_message = ""
        self._saved_path: Optional[Path] = None

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

        self.guide = FeedbackGuide(self)
        self.guide.hide()

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
        layout.addWidget(self.guide)
        layout.addWidget(detail)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.export_button.clicked.connect(self._export)
        self.guide.retry_button.clicked.connect(self._run_feedback)
        self.guide.open_folder_button.clicked.connect(self._open_saved_folder)
        self.guide.issue_button.clicked.connect(self._open_issue_page)
        self.guide.close_button.clicked.connect(self.end_feedback)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    # -- state -----------------------------------------------------------------
    def _on_connection_changed(self, connected: bool):
        if not connected:
            self.status_label.setText("设备未连接")
        elif not self._busy:
            self.status_label.setText("设备已连接，尚未导出")
        self._update_export_button()

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self._update_export_button()

    def _update_export_button(self):
        self.export_button.setEnabled(self.ssh_client.is_connected() and not self._busy)

    # -- guided feedback ---------------------------------------------------------
    def start_feedback(self, error_message: str = "") -> None:
        """Begin the guided flow; collection starts immediately."""
        if self._flow_running:
            # A collection or its preview/save dialog is already open (an
            # error from another section can surface meanwhile).  Fold the
            # request into the running flow instead of nesting a second one.
            if not self._feedback_active:
                self._feedback_active = True
                self._feedback_message = error_message or ""
                self.guide.set_error(self._feedback_message)
                self.guide.set_step(
                    0, "已有诊断采集正在进行，完成后会继续以下步骤。"
                )
            self.guide.show()
            return
        self._feedback_active = True
        self._feedback_message = error_message or ""
        self._saved_path = None
        self.guide.set_error(self._feedback_message)
        self.guide.show()
        self._run_feedback()

    def end_feedback(self) -> None:
        if self._flow_running:
            # Keep the guide until the open collection/preview finishes.
            return
        self._feedback_active = False
        self._feedback_message = ""
        self.guide.hide()

    def _run_feedback(self) -> None:
        connected = self.ssh_client.is_connected()
        hint = (
            "正在通过 SSH 读取只读诊断信息，通常需要几秒到几十秒，请稍候…"
            if connected
            else "设备未连接，本次只采集电脑端日志和运行环境；"
            "如能连接设备，连接后点击“重新生成”可得到包含设备信息的完整诊断包。"
        )
        self.guide.set_step(0, hint)
        self._collect(include_device=connected)

    def _feedback_step(self, step: int, hint: str, *, failed: bool = False) -> None:
        if self._feedback_active:
            self.guide.set_step(step, hint, failed=failed)

    def _open_saved_folder(self) -> None:
        if self._saved_path is not None:
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(self._saved_path.parent))
            )

    def _open_issue_page(self) -> None:
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl(f"{_rmtool.GITHUB_REPO_URL}/issues/new")
        )

    # -- collection --------------------------------------------------------------
    @require_connection
    def _export(self):
        if not self._flow_running:
            # A plain export is not a report; drop any finished flow.
            self.end_feedback()
        self._collect(include_device=True)

    def _collect(self, *, include_device: bool) -> None:
        if self._flow_running:
            return
        self._flow_running = True
        self._set_busy(
            True,
            "正在通过 SSH 采集只读诊断信息…" if include_device else "正在采集电脑端诊断信息…",
        )
        worker = _rmtool.Worker(
            _collect_diagnostics,
            self.ssh_client,
            _rmtool.app_state_dir() / "remarkable_tool.log",
            include_device,
        )

        def on_finished(collected):
            if sip.isdeleted(self):
                return
            self._set_busy(False, "采集完成，请预览并保存诊断包")
            if self._feedback_active and self._feedback_message:
                collected = [_diagnostics.reported_error(self._feedback_message), *collected]
            try:
                self._preview_and_save(collected)
            finally:
                self._flow_running = False

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Diagnostics collection failed after tab close: %s", exc)
                return
            self._flow_running = False
            self._set_busy(False, "诊断信息采集失败，未生成文件")
            logging.error("Diagnostics collection failed: %s", exc)
            self._feedback_step(0, f"采集失败：{exc}", failed=True)
            # feedback=False: never offer to report a failure of the reporter.
            _ui.show_error(
                self, _rmtool.APP_NAME, f"诊断信息采集失败：{exc}", feedback=False
            )

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _preview_and_save(self, collected):
        self._feedback_step(
            1, "请检查将写入诊断包的内容，可取消勾选不想提供的可选条目，然后点击“保存诊断包…”。"
        )
        dialog = DiagnosticPreviewDialog(collected, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            self.status_label.setText("已取消，未生成诊断包")
            self._feedback_step(1, "已取消保存。需要时可点击“重新生成”。", failed=True)
            return
        self._feedback_step(2, "请选择诊断包的保存位置。")
        default_name = _diagnostics.bundle_name(
            _diagnostics.platform_label(collected)
        )
        target, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存诊断包", default_name, "诊断包 (*.zip)"
        )
        if not target:
            self.status_label.setText("已取消，未生成诊断包")
            self._feedback_step(2, "已取消保存。需要时可点击“重新生成”。", failed=True)
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
            self._feedback_step(2, f"写入失败：{exc}", failed=True)
            _ui.show_error(
                self, _rmtool.APP_NAME, f"诊断包写入失败：{exc}", feedback=False
            )
            return
        self._saved_path = Path(saved)
        self.status_label.setText(f"诊断包已保存：{saved}")
        if self._feedback_active:
            # The guide replaces the modal summary: it stays visible with the
            # next actions (open folder / open GitHub issue).
            self._feedback_step(
                3,
                f"诊断包已保存：{saved}\n"
                "请在 GitHub 新建问题，简单描述操作步骤，并把这个 zip 拖入问题描述中上传。"
                "包内不包含任何设备密码。",
            )
            self._open_saved_folder()
            return
        _ui.show_info(
            self,
            _rmtool.APP_NAME,
            "诊断包已保存：\n"
            f"{saved}\n\n"
            "请把这个文件发给维护者以协助定位问题。"
            "包内不包含任何设备密码。",
        )
        self._open_saved_folder()
