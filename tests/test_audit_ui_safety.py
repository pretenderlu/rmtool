"""Offline regressions for connection-scoped selection and plugin worker ownership."""

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

import rmtool
import _tab_documents
import _tab_toolbox
from _ssh import SSHClientWrapper


_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, value):
        for callback in self.callbacks:
            callback(value)


class PendingWorker:
    def __init__(self, fn, *args, **kwargs):
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = mock.Mock(finished=Signal(), error=Signal())


class UiSafetyTests(unittest.TestCase):
    def setUp(self):
        self.ssh = SSHClientWrapper()
        self.ssh._client = mock.Mock()
        self.ssh._client.get_transport.return_value.is_active.return_value = True
        self.pool = mock.Mock()
        self.worker_patch = mock.patch.object(rmtool, "Worker", PendingWorker)
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)

    def make_documents(self):
        tab = _tab_documents.DocumentsTab(self.ssh)
        tab.thread_pool = self.pool
        tab.set_connection_state(True)
        self.ssh.connection_changed.connect(tab.set_connection_state)
        self.addCleanup(tab.deleteLater)
        return tab

    def document(self, identifier="old-device-id"):
        return rmtool.DocumentItem(
            identifier=identifier, name=identifier, doc_type="DocumentType",
            updated=None, available_assets=["rm"],
        )

    def select_document(self, tab, identifier="old-device-id"):
        tab._on_documents_loaded([self.document(identifier)])
        tab.table.selectRow(0)

    def reconnect(self):
        self.ssh.close()
        with self.ssh._transport_lock, self.ssh._state_lock:
            self.ssh._client = mock.Mock()
            self.ssh._client.get_transport.return_value.is_active.return_value = True
            self.ssh.connection_changed.emit(True)

    def queued_delete(self, tab):
        with mock.patch.object(_tab_documents, "ask_confirmation", return_value=True), \
                mock.patch.object(tab, "_show_progress_dialog"):
            tab._delete_document()
        return self.pool.start.call_args.args[0]

    def test_disconnect_clears_inventory_selection_and_preview(self):
        tab = self.make_documents()
        self.select_document(tab)
        self.assertTrue(tab.delete_button.isEnabled())
        self.assertTrue(tab.export_button.isEnabled())
        self.reconnect()
        self.assertEqual(tab.documents, [])
        self.assertEqual(tab._documents_by_id, {})
        self.assertEqual(tab.table.rowCount(), 0)
        self.assertEqual(tab._selected_documents(), [])
        self.assertIsNone(tab._current_preview_request)
        self.assertIsNone(tab._preview_cover)
        self.assertEqual(tab.preview.toPlainText(), "")
        self.assertFalse(tab.delete_button.isEnabled())
        self.assertFalse(tab.export_button.isEnabled())
        with mock.patch.object(_tab_documents, "show_warning"), \
                mock.patch.object(_tab_documents, "ask_confirmation") as confirm:
            tab._delete_document()
        confirm.assert_not_called()

    def test_manual_and_quiet_refresh_discard_old_results_and_errors(self):
        for quiet in (False, True):
            with self.subTest(quiet=quiet):
                tab = self.make_documents()
                done = mock.Mock()
                if quiet:
                    tab.refresh_quiet(done)
                else:
                    tab.refresh()
                worker = self.pool.start.call_args.args[0]
                self.reconnect()
                self.select_document(tab, "new-device-id")
                with mock.patch.object(tab, "_on_error") as error:
                    worker.signals.finished.emit([self.document()])
                    worker.signals.error.emit(RuntimeError("old refresh failed"))
                self.assertEqual(tab.documents[0].identifier, "new-device-id")
                error.assert_not_called()
                if quiet:
                    self.assertEqual(done.call_count, 2)  # one per terminal signal

    def test_old_preview_cannot_replace_new_preview_for_same_uuid(self):
        tab = self.make_documents()
        self.select_document(tab)
        worker = self.pool.start.call_args.args[0]
        self.reconnect()
        self.select_document(tab)
        with mock.patch.object(tab, "_on_preview_loaded") as loaded, \
                mock.patch.object(tab, "_on_preview_error") as error:
            worker.signals.finished.emit(b"old cover")
            worker.signals.error.emit(RuntimeError("old preview failed"))
        loaded.assert_not_called()
        error.assert_not_called()

    def test_connection_change_in_confirmation_never_submits_delete(self):
        tab = self.make_documents()
        self.select_document(tab)
        self.pool.reset_mock()

        def confirm(*args, **kwargs):
            self.reconnect()
            return True

        with mock.patch.object(_tab_documents, "ask_confirmation", side_effect=confirm):
            tab._delete_document()
        self.pool.start.assert_not_called()

    def test_queued_delete_rejects_reconnected_device(self):
        tab = self.make_documents()
        self.select_document(tab)
        worker = self.queued_delete(tab)
        self.reconnect()
        with mock.patch.object(self.ssh, "exec_checked") as execute:
            with self.assertRaises(RuntimeError):
                worker.fn(*worker.args, **worker.kwargs)
        execute.assert_not_called()

    def test_queued_delete_checks_session_even_before_ui_signal_delivery(self):
        tab = self.make_documents()
        self.select_document(tab)
        worker = self.queued_delete(tab)
        self.ssh._client = mock.Mock()
        with mock.patch.object(self.ssh, "exec_checked") as execute:
            with self.assertRaises(RuntimeError):
                worker.fn(*worker.args, **worker.kwargs)
        execute.assert_not_called()

    def test_delete_validation_happens_after_waiting_for_transport(self):
        tab = self.make_documents()
        self.select_document(tab)
        worker = self.queued_delete(tab)
        started = threading.Event()
        errors = []

        def run():
            started.set()
            try:
                worker.fn(*worker.args, **worker.kwargs)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(self.ssh, "exec_checked") as execute:
            with self.ssh._transport_lock:
                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(started.wait(2))
                self.ssh._client = mock.Mock()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        execute.assert_not_called()

    def test_same_session_batch_delete_retains_existing_behavior(self):
        tab = self.make_documents()
        items = [self.document("first"), self.document("second")]
        with mock.patch.object(self.ssh, "exec_checked") as execute:
            tab._perform_delete_documents(items, tab._connection_generation, self.ssh._client)
        self.assertEqual(execute.call_count, 3)
        self.assertIn("first", execute.call_args_list[0].args[0])
        self.assertIn("second", execute.call_args_list[1].args[0])
        self.assertEqual(execute.call_args_list[2].args[0], "systemctl restart xochitl")

    def test_stale_delete_completion_does_not_refresh_or_report_new_device(self):
        tab = self.make_documents()
        self.select_document(tab)
        worker = self.queued_delete(tab)
        self.reconnect()
        with mock.patch.object(tab, "refresh") as refresh, \
                mock.patch.object(tab, "_on_error") as error, \
                mock.patch.object(_tab_documents, "show_info") as info:
            worker.signals.finished.emit(None)
            worker.signals.error.emit(RuntimeError("old delete failed"))
        refresh.assert_not_called()
        error.assert_not_called()
        info.assert_not_called()

    def make_toolbox(self):
        with mock.patch.object(_tab_toolbox.ToolboxTab, "_probe_device_identity"):
            tab = _tab_toolbox.ToolboxTab(self.ssh, {})
        self.addCleanup(tab.deleteLater)
        patch = mock.patch.object(tab, "_tap_entry_hidden", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)
        for section in tab._detectable_sections:
            section.thread_pool = self.pool
        return tab

    def test_each_plugin_rejects_overlapping_detection_and_install(self):
        toolbox = self.make_toolbox()
        for section in toolbox._detectable_sections:
            for terminal in ("finished", "error"):
                with self.subTest(section=type(section).__name__, terminal=terminal):
                    self.pool.reset_mock()
                    section._start_worker(lambda: None, pending="installing", show_errors=False)
                    worker = self.pool.start.call_args.args[0]
                    status = section.status_label.text()
                    done = mock.Mock()
                    section._start_status_detection(on_done=done, show_errors=False)
                    section._start_worker(lambda: None, pending="second install")
                    self.assertEqual(self.pool.start.call_count, 1)
                    self.assertTrue(section._busy)
                    self.assertEqual(section.status_label.text(), status)
                    self.assertFalse(section.detect_button.isEnabled())
                    done.assert_called_once_with()
                    with mock.patch.object(section, "_apply_status"), \
                            mock.patch.object(_tab_toolbox.logging, "error"):
                        if terminal == "finished":
                            worker.signals.finished.emit(None)
                        else:
                            worker.signals.error.emit(RuntimeError("interrupted install"))
                    self.assertFalse(section._busy)

    def test_detect_all_skips_each_busy_plugin_and_finishes(self):
        toolbox = self.make_toolbox()
        for busy_section in toolbox._detectable_sections:
            with self.subTest(section=type(busy_section).__name__):
                self.pool.reset_mock()
                busy_section._start_worker(lambda: None, pending="installing")
                toolbox._detect_all_statuses()
                completed = 1
                while toolbox._detect_all_busy:
                    self.assertLess(completed, 6)
                    worker = self.pool.start.call_args.args[0]
                    section = toolbox._detectable_sections[toolbox._detect_all_index - 1]
                    with mock.patch.object(section, "_apply_status"):
                        worker.signals.finished.emit(None)
                    completed += 1
                self.assertEqual(self.pool.start.call_count, 5)
                self.assertTrue(busy_section._busy)
                self.assertEqual(busy_section.status_label.text(), "installing")
                self.assertTrue(toolbox.detect_all_button.isEnabled())
                busy_section._busy = False

    def test_legacy_localization_package_and_status_workers_share_busy_guard(self):
        section = _tab_toolbox.RmkitCnSection(self.ssh)
        section.thread_pool = self.pool
        self.addCleanup(section.deleteLater)
        section._busy = True
        section._start_worker(lambda: None, pending="status")
        section._start_package_worker(
            lambda: None, pending="package", success_status="ready", success_message=str,
        )
        self.pool.start.assert_not_called()
        self.assertTrue(section._busy)


if __name__ == "__main__":
    unittest.main()
