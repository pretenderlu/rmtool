"""Offline regressions for connection-scoped selection and plugin worker ownership."""

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets, sip

import rmtool
import _native_chinese
import _pinyin_input
import _plugin_recovery
import _tab_documents
import _tab_toolbox
import _tap_page_turn
import _toolbox_common as common
import _weread_app
import _weread_launcher
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

    def document(self, identifier="11111111-1111-1111-1111-111111111111"):
        return rmtool.DocumentItem(
            identifier=identifier, name=identifier, doc_type="DocumentType",
            updated=None, available_assets=["rm"],
        )

    def select_document(self, tab, identifier="11111111-1111-1111-1111-111111111111"):
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
        first, second = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
        items = [self.document(first), self.document(second)]
        with mock.patch.object(self.ssh, "exec_checked") as execute, \
                mock.patch.object(self.ssh, "sftp_session") as session:
            session.return_value.__enter__.return_value.listdir.return_value = [first, second]
            tab._perform_delete_documents(items, tab._connection_generation, self.ssh._client)
        self.assertEqual(execute.call_count, 3)
        self.assertIn(first, execute.call_args_list[0].args[0])
        self.assertIn(second, execute.call_args_list[1].args[0])
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
        probe = mock.patch.object(_tab_toolbox.ToolboxTab, "_probe_device_identity")
        probe.start()
        self.addCleanup(probe.stop)
        tab = _tab_toolbox.ToolboxTab(self.ssh, {})
        self.addCleanup(lambda: None if sip.isdeleted(tab) else tab.deleteLater())
        patch = mock.patch.object(tab, "_tap_entry_hidden", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)
        for section in tab._detectable_sections:
            section.thread_pool = self.pool
        return tab

    def test_toolbox_first_visit_later_then_accept_after_reconnect(self):
        tab = self.make_toolbox()
        with mock.patch.object(common, "ask_confirmation", side_effect=(False, True)) as ask:
            tab.show()
            _APP.processEvents()
            ask.assert_called_once()
            self.pool.start.assert_not_called()
            tab.hide()
            tab.show()
            _APP.processEvents()
            ask.assert_called_once()
            self.reconnect()
            _APP.processEvents()
            self.assertEqual(ask.call_count, 2)
            self.assertEqual(ask.call_args.kwargs["confirm_text"], "检测全部插件")
            self.assertEqual(ask.call_args.kwargs["cancel_text"], "稍后")
            self.pool.start.assert_called_once()

    def test_toolbox_direct_maintenance_visits_do_not_consume_prompt(self):
        tab = self.make_toolbox()
        with mock.patch.object(common, "ask_confirmation", return_value=False) as ask:
            for navigate in (tab.show_diagnostics, tab.show_capacity_recovery):
                tab.show()
                navigate()
                _APP.processEvents()
                ask.assert_not_called()
                tab.hide()
            tab.show()
            _APP.processEvents()
            ask.assert_called_once()

    def test_toolbox_manual_scan_and_disconnected_visits_do_not_prompt(self):
        self.ssh.close()
        tab = self.make_toolbox()
        with mock.patch.object(common, "ask_confirmation") as ask:
            tab.show()
            _APP.processEvents()
            ask.assert_not_called()
            self.reconnect()
            tab._detect_all_statuses()
            _APP.processEvents()
            ask.assert_not_called()
            self.pool.start.assert_called_once()

    def test_toolbox_prompt_acceptance_cannot_scan_replacement_device(self):
        tab = self.make_toolbox()
        responses = iter((True, False))

        def reconnect_on_accept(*args, **kwargs):
            accepted = next(responses)
            if accepted:
                self.reconnect()
            return accepted

        with mock.patch.object(common, "ask_confirmation", side_effect=reconnect_on_accept) as ask:
            tab.show()
            _APP.processEvents()
            _APP.processEvents()
            self.assertEqual(ask.call_count, 2)
            self.pool.start.assert_not_called()

    def test_toolbox_destroyed_while_confirmation_is_open(self):
        tab = self.make_toolbox()
        # Run the real modal dialog. Deleting the parent must also end its event loop.
        modal_was_open = []

        def delete_parent():
            modal_was_open.append(_APP.activeModalWidget() is not None)
            sip.delete(tab)

        tab.show()
        QtCore.QTimer.singleShot(0, delete_parent)
        _APP.processEvents()
        self.assertEqual(modal_was_open, [True])
        self.assertTrue(sip.isdeleted(tab))
        self.pool.start.assert_not_called()

    def test_toolbox_old_scan_completion_does_not_advance_new_scan(self):
        tab = self.make_toolbox()
        tab._detect_all_statuses()
        old_worker = self.pool.start.call_args.args[0]
        self.reconnect()
        tab._detect_all_statuses()
        progress = tab.detect_all_button.text()
        self.assertEqual(self.pool.start.call_count, 2)
        old_worker.signals.error.emit(RuntimeError("old failure"))
        self.assertEqual(self.pool.start.call_count, 2)
        self.assertEqual(tab.detect_all_button.text(), progress)
        self.assertTrue(tab._detect_all_busy)

    def test_toolbox_probe_failure_is_typed_and_scan_continues(self):
        tab = self.make_toolbox()
        tab._detect_all_statuses()
        self.assertIn("0/", tab.detect_all_button.text())
        worker = self.pool.start.call_args.args[0]
        worker.signals.error.emit(RuntimeError("已启用 可更新"))
        self.assertEqual(tab.tool_table.item(0, 1).text(), "检测失败")
        self.assertIn("1/", tab.detect_all_button.text())
        self.assertEqual(self.pool.start.call_count, 2)

    def test_toolbox_late_busy_skip_does_not_increment_progress(self):
        tab = self.make_toolbox()
        with mock.patch.object(
            tab.native_chinese_section, "_start_status_detection",
            side_effect=lambda *, on_done, show_errors: on_done(),
        ):
            tab._detect_all_statuses()
        self.pool.start.assert_called_once()
        self.assertTrue(tab.pinyin_input_section._busy)
        self.assertEqual(tab.detect_all_button.text(), "正在检测 0/9")

    def test_toolbox_scan_waits_for_identity_to_determine_tap_applicability(self):
        probe = _tab_toolbox.ToolboxTab._probe_device_identity
        is_hidden = _tab_toolbox.ToolboxTab._tap_entry_hidden
        tab = self.make_toolbox()
        tab._tap_entry_hidden.side_effect = lambda: is_hidden(tab)
        tab.thread_pool = self.pool
        probe(tab)
        identity_worker = self.pool.start.call_args.args[0]
        tab._detect_all_statuses()
        self.pool.start.assert_called_once()
        self.assertEqual(tab.detect_all_button.text(), "正在识别设备…")

        package = next(p for p in _tap_page_turn._trusted_catalog() if p.platform == "rm1")
        identity = _tap_page_turn.DeviceIdentity(
            package.firmware, package.platform, package.architecture, package.xochitl_sha256
        )
        identity_worker.signals.finished.emit(identity)
        self.assertFalse(tab._tap_entry_hidden())
        self.assertEqual(tab.detect_all_button.text(), "正在检测 0/9")
        self.assertEqual(self.pool.start.call_count, 2)

    def test_toolbox_typed_plugin_statuses_ignore_misleading_detail(self):
        tab = self.make_toolbox()
        section = tab.native_chinese_section
        section._status = _native_chinese.NativeChineseStatus(
            _native_chinese.NativeChineseState.ENABLED, None, detail="未安装 可更新"
        )
        section.status_label.setText(section._status.detail)
        self.assertEqual(tab.tool_table.item(0, 1).text(), "已启用")
        section = tab.pinyin_input_section
        section._apply_status(_pinyin_input.PinyinInputStatus(
            _pinyin_input.PinyinInputState.OUTDATED, None,
            package=_pinyin_input._trusted_catalog()[0], detail="已启用 可更新",
            installed=True, firmware_residue=True,
        ))
        self.assertEqual(tab.tool_table.item(1, 1).text(), "固件残留")
        self.assertTrue(section.status_label.text().startswith("检测到拼音输入法固件残留"))
        self.assertFalse(section.enable_button.isEnabled())
        self.assertFalse(section.disable_button.isEnabled())
        self.assertFalse(section.uninstall_button.isEnabled())

    def test_toolbox_tap_predecessor_is_not_a_direct_update(self):
        tab = self.make_toolbox()
        section = tab.tap_page_turn_section
        section._status = _tap_page_turn.TapPageTurnStatus(
            _tap_page_turn.TapPageTurnState.OUTDATED, None, package=object(), dropin_present=True
        )
        section.status_label.setText("旧版可更新")
        self.assertEqual(tab.tool_table.item(5, 1).text(), "旧版\n可卸载")

    def test_toolbox_weread_app_and_entry_have_separate_statuses(self):
        tab = self.make_toolbox()
        section = tab.weread_launcher_section
        section._status = _weread_launcher.WeReadLauncherStatus(
            _weread_launcher.WeReadLauncherState.REPAIR_AVAILABLE, None, package=object()
        )
        section._apply_app_status(_weread_app.WeReadAppStatus(
            _weread_app.WeReadAppState.NOT_INSTALLED, None, package=object()
        ))
        text = tab.tool_table.item(4, 1).text()
        self.assertIn("App 未安装", text)
        self.assertIn("入口", text)
        self.assertNotIn("可更新", text)
        section._apply_app_status(_weread_app.WeReadAppStatus(
            _weread_app.WeReadAppState.INSTALLED, None, package=object()
        ))
        self.assertEqual(tab.tool_table.item(4, 1).text(), "App 已安装\n入口 已安装\n可更新")
        tab.tool_table.setCurrentCell(4, 0)
        self.assertTrue(tab.detail_stack.currentWidget().isAncestorOf(section))

    def test_toolbox_shared_recovery_does_not_override_plugin_update(self):
        tab = self.make_toolbox()
        section = tab.native_chinese_section
        section._status = _native_chinese.NativeChineseStatus(
            _native_chinese.NativeChineseState.OUTDATED, None, package=object(), installed=True
        )
        section.status_label.setText("可直接修复更新")
        tab.legacy_plugin_section._apply_status(_plugin_recovery.RecoveryReport(
            _plugin_recovery.RecoveryState.BLOCKED, "可更新 已启用", operation_locked=True
        ))
        self.assertEqual(tab.tool_table.item(0, 1).text(), "旧版\n可更新")
        row = next(i for i, entry in enumerate(tab._tool_entries) if entry["title"] == "共享插件恢复")
        self.assertEqual(tab.tool_table.item(row, 1).text(), "已阻止")

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
                worker = self.pool.start.call_args.args[0]
                toolbox._detect_all_statuses()
                self.assertFalse(toolbox._detect_all_busy)
                self.assertEqual(self.pool.start.call_count, 1)
                self.assertTrue(busy_section._busy)
                self.assertEqual(busy_section.status_label.text(), "installing")
                self.assertTrue(toolbox.detect_all_button.isEnabled())
                self.assertIn("0/9", toolbox.detect_all_button.toolTip())
                with mock.patch.object(busy_section, "_apply_status"):
                    worker.signals.finished.emit(None)
                self.assertFalse(common.plugin_operation_busy(self.ssh))

    def test_different_plugins_share_connection_reservation_until_completion(self):
        toolbox = self.make_toolbox()
        first, second = toolbox._detectable_sections[:2]
        done = mock.Mock()
        first._start_worker(lambda: None, pending="installing", show_errors=False)
        worker = self.pool.start.call_args.args[0]
        second._start_worker(lambda: None, pending="peer", on_done=done, show_errors=False)
        self.assertEqual(self.pool.start.call_count, 1)
        done.assert_called_once_with()
        worker.signals.error.emit(RuntimeError("preflight rejected"))
        self.assertTrue(self.ssh.is_connected())
        self.assertFalse(common.plugin_operation_busy(self.ssh))
        second._start_worker(lambda: None, pending="retry", show_errors=False)
        self.assertEqual(self.pool.start.call_count, 2)
        self.pool.start.call_args.args[0].signals.error.emit(RuntimeError("finish"))

    def test_failed_plugin_does_not_close_connection_and_done_is_once(self):
        section = self.make_toolbox()._detectable_sections[0]
        done = mock.Mock()
        section._start_worker(lambda: None, pending="install", close_connection=True,
                              on_done=done, show_errors=False)
        worker = self.pool.start.call_args.args[0]
        with mock.patch.object(self.ssh, "close") as close:
            worker.signals.error.emit(RuntimeError("preflight"))
            worker.signals.error.emit(RuntimeError("duplicate"))
            worker.signals.finished.emit(None)
        close.assert_not_called()
        done.assert_called_once_with()
        self.assertFalse(common.plugin_operation_busy(self.ssh))

    def test_queued_plugin_rejects_new_client_and_stale_callback_preserves_it(self):
        section = self.make_toolbox()._detectable_sections[0]
        operation = mock.Mock()
        done = mock.Mock()
        section._start_worker(operation, pending="install", close_connection=True,
                              on_done=done, show_errors=False)
        worker = self.pool.start.call_args.args[0]
        self.reconnect()
        with self.assertRaisesRegex(RuntimeError, "连接已变化"):
            worker.fn()
        operation.assert_not_called()
        replacement_task = object()
        self.ssh._toolbox_task = replacement_task
        with mock.patch.object(self.ssh, "close") as close, mock.patch.object(section, "_apply_status") as apply:
            worker.signals.finished.emit(None)
        close.assert_not_called()
        apply.assert_not_called()
        self.assertIs(self.ssh._toolbox_task, replacement_task)
        done.assert_called_once_with()
        del self.ssh._toolbox_task

    def test_plugin_reservation_released_after_tab_deletion_or_queue_failure(self):
        from PyQt5 import sip

        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                section = _tab_toolbox.ReadingEnhancementsSection(self.ssh)
                section.thread_pool = self.pool
                self.addCleanup(lambda section=section: None if sip.isdeleted(section) else section.deleteLater())
                done = mock.Mock()
                if not deleted:
                    self.pool.start.side_effect = RuntimeError("queue unavailable")
                section._start_worker(lambda: None, pending="install", on_done=done, show_errors=False)
                if deleted:
                    worker = self.pool.start.call_args.args[0]
                    sip.delete(section)
                    worker.signals.error.emit(RuntimeError("interrupted"))
                done.assert_called_once_with()
                self.assertFalse(common.plugin_operation_busy(self.ssh))
                self.pool.start.side_effect = None

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

    def test_font_action_during_plugin_work_refuses_without_exception_or_modal(self):
        font = _tab_toolbox.FontTab(self.ssh, rmtool._default_config())
        font.thread_pool = self.pool
        self.addCleanup(font.deleteLater)
        self.ssh._toolbox_task = object()
        with mock.patch.object(QtWidgets, "QProgressDialog") as progress:
            font._start_font_worker(lambda: None, pending="font", on_success=mock.Mock(), error_prefix="font")
        self.pool.start.assert_not_called()
        progress.assert_not_called()
        self.assertFalse(font._busy)
        self.assertIn("另一个插件操作", font.manager_status_label.text())
        del self.ssh._toolbox_task


if __name__ == "__main__":
    unittest.main()
