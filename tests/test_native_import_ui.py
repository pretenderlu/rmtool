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
import _tab_toolbox as toolbox
import _tap_page_turn as tap
import _ui_tasks
from tests.test_device_actions import RecordingClient

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class NativeTransferToolboxTests(unittest.TestCase):
    def test_automatic_activation_is_not_a_plugin_entry(self):
        ssh = RecordingClient()
        with (patch.object(ssh, "is_connected", return_value=True),
              patch.object(toolbox.ToolboxTab, "_probe_device_identity"),
              patch.object(control, "get_status") as probe):
            tab = toolbox.ToolboxTab(ssh, rmtool._default_config())
            self.addCleanup(tab.deleteLater)
            titles = {entry["title"] for entry in tab._tool_entries}
            self.assertIn("扫码传输", titles)
            self.assertNotIn("自动开启导入服务", titles)
            self.assertNotIn("原生文档传输", titles)
            probe.assert_not_called()


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

    def test_documents_exposes_upload_without_a_component_menu(self):
        self.assertTrue(self.widget.upload_button.isEnabled())
        self.assertFalse(hasattr(self.widget, "native_transfer_button"))

    def _offer_activation(self):
        self.widget._offer_native_activation(["book.pdf"], self.widget._connection_generation, self.ssh._client, "manual hint")

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
                self._offer_activation()
                if queued:
                    self.ssh.reconnect()
                    with self.assertRaisesRegex(RuntimeError, "设备连接已变化"):
                        workers[0].fn()
                else:
                    self.assertEqual(workers, [])
                initialize.assert_not_called()
                self.widget.set_connection_state(False)
                self.widget.set_connection_state(True)

    def test_upload_offers_automatic_activation_only_after_preflight_and_waits_for_reboot(self):
        workers = []
        with (patch.object(QtWidgets.QFileDialog, "getOpenFileNames", return_value=(["book.pdf"], "")),
              patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
              patch.object(documents, "ask_confirmation", return_value=True) as ask,
              patch.object(documents, "show_info") as info,
              patch.object(control, "initialize", return_value=self.status) as initialize):
            self.widget.upload_document()
            ask.assert_not_called()
            workers[0].signals.error.emit(documents.NativeImportActivationNeeded("manual hint"))
            self.assertEqual(ask.call_args.kwargs["cancel_text"], "我手动开启")
            self.assertIsNone(self.widget._active_progress)
            self.assertEqual(len(workers), 2)
            workers[1].signals.finished.emit(workers[1].fn())
            initialize.assert_called_once_with(self.ssh, str(rmtool.app_state_dir()))
            self.assertIn("手动重启", info.call_args.args[2])
            self.assertFalse(self.ssh.is_connected())
            self.assertEqual(len(workers), 2)

    def test_manual_choice_does_not_offer_install_again_on_same_connection(self):
        workers = []
        with (patch.object(QtWidgets.QFileDialog, "getOpenFileNames", return_value=(["book.pdf"], "")),
              patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
              patch.object(documents, "ask_confirmation", return_value=False) as ask,
              patch.object(documents, "show_info") as info, patch.object(self.widget, "refresh"),
              patch.object(self.widget, "_on_error") as error):
            unavailable = documents.NativeImportActivationNeeded("manual hint")
            for _ in range(2):
                self.widget.upload_document()
                workers[-1].signals.error.emit(unavailable)
            ask.assert_called_once()
            self.assertIn("manual hint", ask.call_args.args[2])
            error.assert_called_once_with(unavailable)
            info.assert_not_called()
            self.assertEqual(len(workers), 2)

    def test_ready_component_resumes_only_the_same_batch_without_another_offer(self):
        workers = []
        ready = replace(self.status, state=control.NativeImportState.ENABLED)
        with (patch.object(QtWidgets.QFileDialog, "getOpenFileNames", return_value=(["book.pdf"], "")) as select,
              patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
              patch.object(documents, "ask_confirmation", return_value=True) as ask,
              patch.object(control, "initialize", return_value=ready), patch.object(self.widget, "refresh"),
              patch.object(self.widget, "_on_error") as error):
            self.widget.upload_document()
            unavailable = documents.NativeImportActivationNeeded("manual hint")
            workers[0].signals.error.emit(unavailable)
            workers[1].signals.finished.emit(workers[1].fn())
            self.assertEqual(workers[2].args, (["book.pdf"],))
            self.assertIs(workers[2].fn.__self__.client, self.ssh._client)
            workers[2].signals.error.emit(unavailable)
            select.assert_called_once()
            ask.assert_called_once()
            error.assert_called_once_with(unavailable)

    def test_busy_setup_and_upload_block_duplicate_actions(self):
        for upload in (False, True):
            with (self.subTest(upload=upload), patch.object(documents, "ask_confirmation") as ask,
                  patch.object(QtWidgets.QFileDialog, "getOpenFileNames") as select):
                self.widget._upload_active = upload
                if not upload:
                    self.widget.upload_button._rmtool_background_task = object()
                self.widget.upload_document()
                ask.assert_not_called()
                select.assert_not_called()
                self.widget.set_connection_state(False)
                self.widget.set_connection_state(True)

    def test_old_completion_does_not_unlock_new_task_or_show_success(self):
        workers = []
        with (patch.object(documents, "ask_confirmation", return_value=True),
              patch.object(QtCore.QThreadPool, "start", side_effect=workers.append),
              patch.object(documents, "show_info") as info,
              patch.object(_ui_tasks, "show_error") as error):
            self._offer_activation()
            self.ssh.reconnect()
            self.widget.set_connection_state(False)
            self.widget.set_connection_state(True)
            self._offer_activation()
            workers[0].signals.finished.emit(self.status)
            self.assertFalse(self.widget.upload_button.isEnabled())
            workers[1].signals.error.emit(RuntimeError("test failure"))
            self.assertTrue(self.widget.upload_button.isEnabled())
            info.assert_not_called()
            error.assert_called_once()
