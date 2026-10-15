import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtGui, QtWidgets

import rmtool
import _diagnostics
import _dialogs
import _toolbox_common
import _toolbox_diagnostics

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeClient(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, connected=True):
        super().__init__()
        self._connected = connected
        self.connection_info = {"device_name": "", "host": ""}

    def is_connected(self):
        return self._connected


def _drain():
    QtCore.QThreadPool.globalInstance().waitForDone(5000)
    QtWidgets.QApplication.processEvents()


class ErrorDialogFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_dialogs.set_feedback_handler, None)

    def _run_error(self, result, *, handler=True, feedback=True):
        calls = []
        if handler:
            _dialogs.set_feedback_handler(calls.append)
        built = []
        real_build = _dialogs._build_dialog

        def build(*args, **kwargs):
            dialog = real_build(*args, **kwargs)
            built.append(dialog)
            dialog.exec_ = lambda: result
            return dialog

        with mock.patch.object(_dialogs, "_build_dialog", side_effect=build):
            _dialogs.show_error(None, "rmtool", "连接失败：timeout", feedback=feedback)
        button = built[0].findChild(QtWidgets.QPushButton, "appDialogFeedback")
        return calls, button

    def test_feedback_button_starts_handler_with_error_text(self):
        calls, button = self._run_error(_dialogs.FEEDBACK_RESULT)
        self.assertIsNotNone(button)
        self.assertEqual(button.text(), "反馈问题")
        self.assertEqual(calls, ["连接失败：timeout"])

    def test_dismissing_error_does_not_start_feedback(self):
        for result in (QtWidgets.QDialog.Accepted, QtWidgets.QDialog.Rejected):
            with self.subTest(result=result):
                calls, _button = self._run_error(result)
                self.assertEqual(calls, [])

    def test_no_button_without_handler_or_when_opted_out(self):
        _calls, button = self._run_error(_dialogs.FEEDBACK_RESULT, handler=False)
        self.assertIsNone(button)
        calls, button = self._run_error(_dialogs.FEEDBACK_RESULT, feedback=False)
        self.assertIsNone(button)
        self.assertEqual(calls, [])

    def test_feedback_button_triggers_custom_result(self):
        _dialogs.set_feedback_handler(lambda _message: None)
        dialog = _dialogs._build_dialog(
            None, "rmtool", "h", "m", kind="error", feedback_text="反馈问题"
        )
        self.addCleanup(dialog.deleteLater)
        dialog.findChild(QtWidgets.QPushButton, "appDialogFeedback").click()
        self.assertEqual(dialog.result(), _dialogs.FEEDBACK_RESULT)

    def test_handler_failure_is_logged_not_raised(self):
        def broken(_message):
            raise RuntimeError("boom")

        _dialogs.set_feedback_handler(broken)
        with mock.patch.object(_dialogs, "_show_message", return_value=_dialogs.FEEDBACK_RESULT), \
                mock.patch.object(_dialogs.logging, "exception") as logged:
            _dialogs.show_error(None, "rmtool", "x")
        logged.assert_called_once()


class CollectWithoutDeviceTests(unittest.TestCase):
    def test_disconnected_collection_skips_ssh_and_records_items(self):
        client = mock.Mock()
        collected = _diagnostics.collect(client, None, include_device=False)
        client.exec_command.assert_not_called()
        device = [r for r in collected if r.item.name.startswith("device/")]
        self.assertEqual(len(device), len(_diagnostics.DEVICE_ITEMS))
        self.assertTrue(all(r.error == "设备未连接，未采集" for r in device))

    def test_reported_error_is_optional_and_capped(self):
        item = _diagnostics.reported_error("x" * (_diagnostics.ITEM_CAP_BYTES + 10))
        self.assertTrue(item.item.optional)
        self.assertTrue(item.truncated)
        self.assertLessEqual(len(item.text.encode("utf-8")), _diagnostics.ITEM_CAP_BYTES)


class FeedbackFlowTests(unittest.TestCase):
    def make_section(self, connected=True):
        section = _toolbox_diagnostics.DiagnosticsSection(FakeClient(connected))
        self.addCleanup(section.deleteLater)
        return section

    def step_states(self, section):
        return [label.property("state") for label in section.guide.step_labels]

    def test_start_feedback_collects_and_saves_with_reported_error(self):
        section = self.make_section(connected=True)
        collected = [
            _diagnostics.CollectedItem(_diagnostics.DiagItem("pc/environment.txt", "env"), "env\n")
        ]
        dialog = mock.Mock()
        dialog.exec_.return_value = QtWidgets.QDialog.Accepted
        dialog.include_optional.return_value = True
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "diag.zip"
            with mock.patch.object(
                _toolbox_diagnostics, "_collect_diagnostics", return_value=collected
            ) as collect, mock.patch.object(
                _toolbox_diagnostics, "DiagnosticPreviewDialog", return_value=dialog
            ) as preview, mock.patch.object(
                QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(target), "")
            ), mock.patch.object(QtGui.QDesktopServices, "openUrl") as open_url, \
                    mock.patch.object(_toolbox_common, "show_info") as info:
                section.start_feedback("安装失败：SHA-256 不匹配")
                self.assertTrue(section.guide.isVisibleTo(section))
                self.assertEqual(self.step_states(section)[0], "active")
                _drain()
                self.assertTrue(target.is_file())
                with zipfile.ZipFile(target) as archive:
                    reported = archive.read("pc/reported-error.txt").decode("utf-8")
            self.assertIn("SHA-256 不匹配", reported)
            self.assertTrue(collect.call_args.args[2])  # device items included
            preview_items = preview.call_args.args[0]
            self.assertEqual(preview_items[0].item.name, "pc/reported-error.txt")
            info.assert_not_called()  # the guide replaces the modal summary
            open_url.assert_called_once()  # opens the folder with the zip
        self.assertEqual(self.step_states(section), ["done", "done", "done", "active"])
        self.assertTrue(section.guide.issue_button.isVisibleTo(section))
        with mock.patch.object(QtGui.QDesktopServices, "openUrl") as open_url:
            section.guide.issue_button.click()
        self.assertTrue(open_url.call_args.args[0].toString().endswith("/issues/new"))

    def test_disconnected_feedback_collects_pc_side_only(self):
        section = self.make_section(connected=False)
        self.assertFalse(section.export_button.isEnabled())
        with mock.patch.object(
            _toolbox_diagnostics, "_collect_diagnostics", return_value=[]
        ) as collect, mock.patch.object(section, "_preview_and_save"):
            section.start_feedback("连接失败")
            self.assertIn("设备未连接", section.guide.hint_label.text())
            _drain()
        self.assertFalse(collect.call_args.args[2])

    def test_cancelled_preview_marks_step_failed_and_offers_retry(self):
        section = self.make_section()
        section._feedback_active = True
        section.guide.show()
        dialog = mock.Mock()
        dialog.exec_.return_value = QtWidgets.QDialog.Rejected
        with mock.patch.object(_toolbox_diagnostics, "DiagnosticPreviewDialog", return_value=dialog):
            section._preview_and_save([])
        self.assertEqual(self.step_states(section)[1], "failed")
        self.assertTrue(section.guide.retry_button.isVisibleTo(section))

    def test_collection_failure_does_not_offer_feedback_again(self):
        section = self.make_section()
        with mock.patch.object(
            _toolbox_diagnostics, "_collect_diagnostics", side_effect=RuntimeError("ssh down")
        ), mock.patch.object(_toolbox_common, "show_error") as show_error:
            section.start_feedback("原始错误")
            _drain()
        self.assertEqual(show_error.call_args.kwargs.get("feedback"), False)
        self.assertEqual(self.step_states(section)[0], "failed")

    def test_feedback_during_open_preview_does_not_nest_a_second_flow(self):
        section = self.make_section()
        nested = []
        dialog = mock.Mock()
        dialog.include_optional.return_value = True

        def preview_exec():
            # Another section's error offers 反馈问题 while the preview is open.
            with mock.patch.object(section, "_collect") as collect:
                section.start_feedback("第二个错误")
                nested.append(collect.call_count)
            return QtWidgets.QDialog.Rejected

        dialog.exec_.side_effect = preview_exec
        with mock.patch.object(
            _toolbox_diagnostics, "_collect_diagnostics", return_value=[]
        ), mock.patch.object(
            _toolbox_diagnostics, "DiagnosticPreviewDialog", return_value=dialog
        ):
            section.start_feedback("第一个错误")
            _drain()
        self.assertEqual(nested, [0])
        self.assertEqual(section._feedback_message, "第一个错误")
        self.assertFalse(section._flow_running)

    def test_feedback_adopts_running_plain_export(self):
        section = self.make_section()
        gate = __import__("threading").Event()

        def slow_collect(*_args):
            gate.wait(5)
            return []

        with mock.patch.object(
            _toolbox_diagnostics, "_collect_diagnostics", side_effect=slow_collect
        ), mock.patch.object(section, "_preview_and_save") as preview:
            section._export()
            section.start_feedback("运行中报错")
            self.assertTrue(section._feedback_active)
            self.assertIn("已有诊断采集正在进行", section.guide.hint_label.text())
            gate.set()
            _drain()
        reported = preview.call_args.args[0][0]
        self.assertEqual(reported.item.name, "pc/reported-error.txt")

    def test_manual_export_after_finished_flow_is_not_a_report(self):
        section = self.make_section()
        section._feedback_active = True
        section._feedback_message = "旧报错"
        section.guide.show()
        with mock.patch.object(section, "_collect"):
            section._export()
        self.assertFalse(section._feedback_active)
        self.assertFalse(section.guide.isVisibleTo(section))

    def test_end_feedback_hides_guide(self):
        section = self.make_section()
        with mock.patch.object(section, "_collect"):
            section.start_feedback("x")
        section.guide.close_button.click()
        self.assertFalse(section.guide.isVisibleTo(section))
        self.assertFalse(section._feedback_active)


class MainWindowFeedbackTests(unittest.TestCase):
    def test_feedback_opens_diagnostics_even_when_disconnected(self):
        window = rmtool.MainWindow()
        self.addCleanup(window.deleteLater)
        toolbox_index = window.pages.indexOf(window.toolbox_tab)
        self.assertFalse(window.toolbox_tab.isEnabled())
        section = window.toolbox_tab.diagnostics_section
        with mock.patch.object(section, "start_feedback") as start:
            window.start_feedback("连接失败")
        start.assert_called_once_with("连接失败")
        self.assertEqual(window.pages.currentIndex(), toolbox_index)
        self.assertTrue(window.toolbox_tab.isEnabled())
        self.assertTrue(window.nav_buttons[toolbox_index].isChecked())
        self.assertIs(
            window.toolbox_tab.detail_stack.currentWidget(),
            window.toolbox_tab.detail_stack.widget(
                next(
                    row for row, entry in enumerate(window.toolbox_tab._tool_entries)
                    if entry["section"] is section
                )
            ),
        )

    def test_open_guide_survives_disconnect_and_close_restores_gate(self):
        window = rmtool.MainWindow()
        self.addCleanup(window.deleteLater)
        section = window.toolbox_tab.diagnostics_section
        with mock.patch.object(section, "_collect"):
            window.start_feedback("x")
        window._update_tabs_enabled(False)
        self.assertTrue(window.toolbox_tab.isEnabled())
        section.guide.close_button.click()
        self.assertFalse(window.toolbox_tab.isEnabled())


if __name__ == "__main__":
    unittest.main()
