"""Document-center setup is explicit, asynchronous and bound to one connection."""
from dataclasses import replace
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets
import rmtool
import _native_import_control as control
import _tab_documents as documents
import _tab_toolbox as toolbox
import _tap_page_turn as tap
import _ui_tasks
import _xovi_standalone as shared
from tests.test_device_actions import RecordingClient

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class NativeTransferToolboxTests(unittest.TestCase):
    def setUp(self):
        self.ssh = RecordingClient()
        connected = patch.object(self.ssh, "is_connected", side_effect=lambda: self.ssh._client is not None)
        connected.start()
        self.addCleanup(connected.stop)

    def test_status_probe_is_read_only_and_ignores_old_session_results(self):
        ssh = self.ssh
        package = next(p for p in control._trusted_catalog() if p.platform == "ferrari")
        identity = tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
        feature = control._context(identity, package)[3]
        record = shared.SharedFeatureState(feature, True, "old")
        inspection = shared.SharedInspection({control.FEATURE_ID: record}, True, True)
        digest_command = "sha256sum /usr/bin/xochitl"
        ssh.outputs = {
            "tr -cd '0-9' < /etc/version": package.firmware,
            "uname -m": package.architecture,
            "cat /sys/devices/soc0/machine 2>/dev/null || "
            "tr -d '\\0' < /proc/device-tree/model 2>/dev/null || true": package.platform,
            digest_command: package.xochitl_sha256,
        }
        workers = []
        with (patch.object(toolbox.ToolboxTab, "_probe_device_identity"),
              patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
              patch.object(control, "get_status", wraps=control.get_status) as probe,
              patch.object(control, "initialize") as initialize,
              patch.object(control, "install") as install,
              patch.object(control, "uninstall") as uninstall,
              patch.object(tap, "_xochitl_process_token", return_value="now"),
              patch.object(shared, "has_shared_artifacts", return_value=True),
              patch.object(shared, "firmware_residue_detail", return_value=""),
              patch.object(control, "_inspect") as inspect):
            tab = toolbox.ToolboxTab(ssh, rmtool._default_config())
            self.addCleanup(tab.deleteLater)
            section = tab.native_transfer_section
            row = next(i for i, entry in enumerate(tab._tool_entries) if entry["section"] is section)
            for digest, result, summary in (
                (package.xochitl_sha256, replace(inspection, states={}), "未安装\n可安装"),
                (package.xochitl_sha256, replace(inspection, launcher_update_available=True), "已安装\n可更新"),
                (package.xochitl_sha256, inspection, "已启用"),
                ("0" * 64, inspection, "不兼容"),
            ):
                with self.subTest(summary=summary):
                    ssh.outputs[digest_command] = digest
                    inspect.return_value = (result, {}, {})
                    section.detect_button.click()
                    worker = workers.pop()
                    status = worker.fn()
                    worker.signals.finished.emit(status)
                    probe.assert_called_once_with(ssh, control._trusted_catalog())
                    probe.reset_mock()
                    self.assertEqual(tab.tool_table.item(row, 1).text(), summary)
                    self.assertEqual(ssh.commands, list(ssh.outputs))
                    ssh.commands.clear()

            section.detect_button.click()
            worker = workers.pop()
            ssh.reconnect()
            with self.assertRaisesRegex(RuntimeError, "设备连接已变化"):
                worker.fn()
            worker.signals.finished.emit(status)
            self.assertEqual(tab.tool_table.item(row, 1).text(), "待检测")
            probe.assert_not_called()
            initialize.assert_not_called()
            install.assert_not_called()
            uninstall.assert_not_called()
        self.assertEqual(ssh.commands, [])

    def test_details_button_navigates_without_opening_menu_or_installing(self):
        ssh = self.ssh
        with (patch.object(rmtool, "FirmwareSSHClientWrapper", return_value=ssh),
              patch.object(rmtool, "load_config", return_value=rmtool._default_config()),
              patch.object(toolbox.ToolboxTab, "_probe_device_identity"),
              patch.object(QtCore.QThreadPool, "start"),
              patch.object(documents, "ask_confirmation", return_value=False) as ask,
              patch.object(documents, "run_in_background") as run,
              patch.object(control, "initialize") as initialize,
              patch.object(control, "install") as install,
              patch.object(control, "uninstall") as uninstall):
            window = rmtool.MainWindow()
            self.addCleanup(window.deleteLater)
            window._update_tabs_enabled(True)
            window.documents_tab.set_connection_state(True)
            window.nav_buttons[window.pages.indexOf(window.toolbox_tab)].click()
            section = window.toolbox_tab.native_transfer_section
            row = next(i for i, entry in enumerate(window.toolbox_tab._tool_entries) if entry["section"] is section)
            window.toolbox_tab.tool_table.setCurrentCell(row, 0)
            detail = window.toolbox_tab.detail_stack.currentWidget()
            button = next(b for b in detail.findChildren(QtWidgets.QPushButton) if b.text() == "前往文档中心")
            menu = window.documents_tab.native_transfer_button.menu()
            opened = Mock()
            menu.aboutToShow.connect(opened)
            menu.aboutToShow.connect(lambda: QtCore.QTimer.singleShot(0, menu.close))
            button.click()
            self.assertIs(window.pages.currentWidget(), window.documents_tab)
            self.assertTrue(window.nav_buttons[window.pages.indexOf(window.documents_tab)].isChecked())
            opened.assert_not_called()
            ask.assert_not_called()
            run.assert_not_called()
            initialize.assert_not_called()
            install.assert_not_called()
            uninstall.assert_not_called()
        self.assertEqual(ssh.commands, [])


class NativeImportUiTests(unittest.TestCase):
    def setUp(self):
        self.ssh = RecordingClient()
        self.widget = rmtool.DocumentsTab(self.ssh)
        self.widget.set_connection_state(True)
        self.addCleanup(self.widget.deleteLater)
        self.addCleanup(QtWidgets.QApplication.processEvents)
        package = control._trusted_catalog()[0]
        identity = tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
        self.status = control.NativeImportStatus(control.NativeImportState.ENABLE_PENDING_REBOOT, identity, package)

    def test_menu_has_accessible_fixed_size_control_and_cancel_never_starts_work(self):
        button = self.widget.native_transfer_button
        self.assertEqual(button.toolTip(), "原生传输服务")
        self.assertEqual(button.accessibleName(), button.toolTip())
        self.assertEqual(button.size(), QtCore.QSize(36, 36))
        self.assertFalse(button.icon().isNull())
        with patch.object(documents, "ask_confirmation", return_value=False), patch.object(documents, "run_in_background") as run:
            for action in button.menu().actions():
                action.trigger()
            run.assert_not_called()
        self.assertFalse(self.ssh.commands)

    def test_setup_worker_runs_only_on_captured_connection(self):
        workers = []
        with (patch.object(documents, "ask_confirmation", return_value=True),
              patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
              patch.object(control, "initialize", return_value=self.status) as initialize,
              patch.object(documents, "show_info") as info):
            self.widget._initialize_native_transfer()
            self.assertFalse(self.widget.upload_button.isEnabled())
            self.assertFalse(self.widget.native_transfer_button.isEnabled())
            self.assertEqual(len(workers), 1)
            self.assertEqual(workers[0].fn(), self.status)
            initialize.assert_called_once()
            workers[0].signals.finished.emit(self.status)
            self.assertFalse(self.ssh.is_connected())
            self.assertIn("手动重启", info.call_args.args[2])
            self.assertFalse(self.widget.upload_button.isEnabled())
        self.assertFalse(self.ssh.commands)

    def test_changed_profile_during_confirmation_or_queue_never_mutates_new_device(self):
        for queued in (False, True):
            workers = []
            def confirm(*_args, **_kwargs):
                if not queued:
                    self.ssh.reconnect()
                return True
            with (self.subTest(queued=queued), patch.object(documents, "ask_confirmation", side_effect=confirm),
                  patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
                  patch.object(control, "initialize") as initialize):
                self.widget._initialize_native_transfer()
                if queued:
                    self.ssh.reconnect()
                with self.assertRaisesRegex(RuntimeError, "设备连接已变化"):
                    workers[0].fn()
                initialize.assert_not_called()
                self.widget.set_connection_state(False)
                self.widget.set_connection_state(True)

    def test_busy_setup_and_upload_block_duplicate_actions(self):
        for upload in (False, True):
            with self.subTest(upload=upload), patch.object(documents, "ask_confirmation") as ask:
                self.widget._upload_active = upload
                if not upload:
                    self.widget.native_transfer_button._rmtool_background_task = object()
                self.widget._initialize_native_transfer()
                self.widget._remove_native_transfer()
                self.widget.upload_document()
                ask.assert_not_called()
                self.widget.set_connection_state(False)
                self.widget.set_connection_state(True)

    def test_ready_and_noop_removal_do_not_disconnect_or_request_restart(self):
        with patch.object(documents, "show_info") as info:
            self.widget._native_transfer_initialized(replace(self.status, state=control.NativeImportState.ENABLED))
            self.assertTrue(self.ssh.is_connected())
            self.assertNotIn("重启", info.call_args.args[2])
            self.widget._native_transfer_removed(replace(self.status, state=control.NativeImportState.NOT_INSTALLED, detail="无需移除"))
            self.assertTrue(self.ssh.is_connected())
        with patch.object(documents, "show_error") as error:
            self.widget._native_transfer_initialized(replace(self.status, state=control.NativeImportState.BROKEN, detail="unsafe"))
            self.assertTrue(self.ssh.is_connected())
            self.assertEqual(error.call_args.args[2], "unsafe")

    def test_removal_is_confirmed_and_only_calls_native_control_lifecycle(self):
        workers = []
        removed = replace(self.status, state=control.NativeImportState.NOT_INSTALLED)
        with (patch.object(documents, "ask_confirmation", return_value=True) as ask,
              patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
              patch.object(control, "uninstall", return_value=removed) as uninstall,
              patch.object(documents, "show_info") as info):
            self.widget._remove_native_transfer()
            self.assertTrue(ask.call_args.kwargs["danger"])
            self.assertEqual(workers[0].fn(), removed)
            uninstall.assert_called_once_with(self.ssh, control._trusted_catalog())
            workers[0].signals.finished.emit(removed)
            self.assertFalse(self.ssh.is_connected())
            self.assertIn("手动重启", info.call_args.args[2])
            self.assertIn("书籍及其他插件已保留", info.call_args.args[2])
        self.assertFalse(self.ssh.commands)

    def test_old_completion_does_not_unlock_new_task_or_show_success(self):
        workers = []
        with (patch.object(documents, "ask_confirmation", return_value=True),
              patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
              patch.object(documents, "show_info") as info,
              patch.object(_ui_tasks, "show_error") as error):
            self.widget._initialize_native_transfer()
            self.ssh.reconnect()
            self.widget.set_connection_state(False)
            self.widget.set_connection_state(True)
            self.widget._initialize_native_transfer()
            workers[0].signals.finished.emit(self.status)
            self.assertFalse(self.widget.upload_button.isEnabled())
            self.assertFalse(self.widget.native_transfer_button.isEnabled())
            workers[1].signals.error.emit(RuntimeError("test failure"))
            self.assertTrue(self.widget.upload_button.isEnabled())
            self.assertTrue(self.widget.native_transfer_button.isEnabled())
            info.assert_not_called()
            error.assert_called_once()
