"""Document-center setup is explicit, asynchronous and bound to one connection."""
from dataclasses import replace
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets
import rmtool
import _native_import_control as control
import _tab_documents as documents
import _tap_page_turn as tap
import _ui_tasks
from tests.test_device_actions import RecordingClient

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


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
