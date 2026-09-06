"""Offline recovery UI contracts, including device switches and queued work."""

import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets, sip

import _package_download
import _plugin_recovery as recovery
import rmtool
import _tab_toolbox as toolbox
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
        self.signals = SimpleNamespace(finished=Signal(), error=Signal())

    def execute(self):
        return self.fn(*self.args, **self.kwargs)


def report(state=recovery.RecoveryState.REPAIR_AVAILABLE, backup_path=""):
    return SimpleNamespace(
        state=state, detail="verified ownership", features=("reading-enhancements", "pinyin-input"),
        issues=("missing payload: reading.qmd",), backup_path=backup_path,
        can_repair=state == recovery.RecoveryState.REPAIR_AVAILABLE,
    )


class RecoveryUiTests(unittest.TestCase):
    def setUp(self):
        self.ssh = SSHClientWrapper()
        self.ssh._client = mock.Mock()
        self.ssh._client.get_transport.return_value.is_active.return_value = True
        self.pool = mock.Mock()
        patch = mock.patch.object(rmtool, "Worker", PendingWorker)
        patch.start()
        self.addCleanup(patch.stop)
        self.section = toolbox.LegacyPluginMigrationSection(self.ssh)
        self.section.thread_pool = self.pool
        self.addCleanup(lambda: None if sip.isdeleted(self.section) else self.section.deleteLater())

    def reconnect(self):
        self.ssh.close()
        with self.ssh._transport_lock, self.ssh._state_lock:
            self.ssh._client = mock.Mock()
            self.ssh._client.get_transport.return_value.is_active.return_value = True
            self.ssh.connection_changed.emit(True)

    def queued_repair(self):
        self.section._apply_status(report())
        with mock.patch.object(toolbox, "ask_confirmation", return_value=True):
            self.section._repair()
        return self.pool.start.call_args.args[0]

    def test_read_only_detection_uses_inspector_not_repair_or_migration(self):
        result = report()
        done = mock.Mock()
        with mock.patch.object(recovery, "inspect_recovery", return_value=result) as inspect, \
                mock.patch.object(recovery, "repair") as repair, \
                mock.patch.object(toolbox._residue_migration, "inspect_residue") as migrate, \
                mock.patch.object(self.ssh, "exec_checked") as write:
            self.section._start_status_detection(on_done=done)
            worker = self.pool.start.call_args.args[0]
            worker.signals.finished.emit(worker.execute())
        inspect.assert_called_once_with(self.ssh)
        repair.assert_not_called()
        migrate.assert_not_called()
        write.assert_not_called()
        done.assert_called_once_with()
        self.assertTrue(self.section.repair_button.isEnabled())

    def test_report_states_gate_repair_and_expose_details(self):
        summaries = {
            recovery.RecoveryState.NOT_NEEDED: "无需修复",
            recovery.RecoveryState.REPAIR_AVAILABLE: "可修复",
            recovery.RecoveryState.UNSUPPORTED: "不支持",
            recovery.RecoveryState.BLOCKED: "已阻止",
        }
        for state, summary in summaries.items():
            with self.subTest(state=state):
                self.section._apply_status(report(state))
                text = self.section.status_label.text()
                self.assertIn("verified ownership", text)
                self.assertIn("missing payload", text)
                self.assertIn("pinyin-input", text)
                self.assertEqual(toolbox.ToolboxTab._status_summary(text), summary)
                self.assertEqual(self.section.repair_button.isEnabled(), state == recovery.RecoveryState.REPAIR_AVAILABLE)
        with mock.patch.object(toolbox, "ask_confirmation") as confirm:
            self.section._repair()
        confirm.assert_not_called()

    def test_busy_and_disconnected_detection_finish_once_without_changing_status(self):
        self.section._busy = True
        self.section.status_label.setText("installing")
        done = mock.Mock()
        self.section._start_status_detection(on_done=done)
        done.assert_called_once_with()
        self.pool.start.assert_not_called()
        self.assertTrue(self.section._busy)
        self.assertEqual(self.section.status_label.text(), "installing")
        self.ssh.close()
        done.reset_mock()
        self.section._start_status_detection(on_done=done)
        done.assert_called_once_with()
        self.pool.start.assert_not_called()

    def test_terminal_signals_call_done_exactly_once(self):
        for terminal in ("finished", "error"):
            with self.subTest(terminal=terminal):
                done = mock.Mock()
                self.section._start_status_detection(on_done=done, show_errors=False)
                worker = self.pool.start.call_args.args[0]
                if terminal == "finished":
                    worker.signals.finished.emit(report())
                else:
                    worker.signals.error.emit(RuntimeError("failed"))
                worker.signals.finished.emit(report())
                worker.signals.error.emit(RuntimeError("duplicate"))
                done.assert_called_once_with()
                self.assertFalse(self.section._busy)

    def test_pool_submission_failure_finishes_once(self):
        done = mock.Mock()
        self.pool.start.side_effect = RuntimeError("queue unavailable")
        self.section._start_status_detection(on_done=done, show_errors=False)
        done.assert_called_once_with()
        self.assertFalse(self.section._busy)

    def test_confirmation_explains_scope_and_can_cancel(self):
        self.section._apply_status(report())
        with mock.patch.object(toolbox, "ask_confirmation", return_value=False) as confirm:
            self.section._repair()
        message = confirm.call_args.args[2]
        for phrase in ("全部已启用", "受信资源包重建", "启用/停用", "备份", "设置、字体、书籍", "手动重启"):
            self.assertIn(phrase, message)
        self.assertIn("仅解除本次新增", message)
        self.assertIn("失败时保留保护", message)
        self.assertTrue(confirm.call_args.kwargs["danger"])
        self.pool.start.assert_not_called()

    def test_device_switch_during_confirmation_does_not_queue(self):
        self.section._apply_status(report())
        with mock.patch.object(toolbox, "ask_confirmation", side_effect=lambda *a, **k: (self.reconnect() or True)):
            self.section._repair()
        self.pool.start.assert_not_called()
        self.assertFalse(self.section.repair_button.isEnabled())

    def test_report_replaced_during_confirmation_does_not_queue(self):
        self.section._apply_status(report())
        with mock.patch.object(toolbox, "ask_confirmation", side_effect=lambda *a, **k: (self.section._apply_status(report()) or True)):
            self.section._repair()
        self.pool.start.assert_not_called()

    def test_changed_transport_before_signal_delivery_rejects_cached_report(self):
        self.section._apply_status(report())
        self.ssh._client = mock.Mock()
        with mock.patch.object(toolbox, "ask_confirmation") as confirm:
            self.section._repair()
        confirm.assert_not_called()
        self.pool.start.assert_not_called()

    def test_queued_repair_rejects_new_transport_without_backend_call(self):
        with mock.patch.object(recovery, "repair") as repair:
            worker = self.queued_repair()
            self.ssh._client = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, "连接已变化"):
                worker.execute()
        repair.assert_not_called()

    def test_identity_check_happens_after_transport_lock(self):
        with mock.patch.object(recovery, "repair") as repair:
            worker = self.queued_repair()
            started = threading.Event()
            errors = []

            def execute():
                started.set()
                try:
                    worker.execute()
                except Exception as exc:
                    errors.append(exc)

            with self.ssh._transport_lock:
                thread = threading.Thread(target=execute)
                thread.start()
                self.assertTrue(started.wait(2))
                self.ssh._client = mock.Mock()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        repair.assert_not_called()

    def test_stale_callbacks_do_not_change_new_status_or_close_connection(self):
        done = mock.Mock()
        self.section._start_status_detection(on_done=done)
        worker = self.pool.start.call_args.args[0]
        self.reconnect()
        self.section._start_status_detection()
        text = self.section.status_label.text()
        with mock.patch.object(self.ssh, "close") as close, \
                mock.patch.object(toolbox, "show_error") as error:
            worker.signals.finished.emit(report())
            worker.signals.error.emit(RuntimeError("old error"))
        done.assert_called_once_with()
        close.assert_not_called()
        error.assert_not_called()
        self.assertTrue(self.section._busy)
        self.assertEqual(self.section.status_label.text(), text)

    def test_deleted_section_completes_coordinator_without_widgets(self):
        done = mock.Mock()
        self.section._start_status_detection(on_done=done)
        worker = self.pool.start.call_args.args[0]
        sip.delete(self.section)
        with mock.patch.object(self.ssh, "close") as close:
            worker.signals.finished.emit(report())
            worker.signals.error.emit(RuntimeError("late"))
        close.assert_not_called()
        done.assert_called_once_with()

    def test_non_repaired_results_do_not_claim_success_or_close(self):
        for state in recovery.RecoveryState:
            with self.subTest(state=state):
                worker = self.queued_repair()
                with mock.patch.object(toolbox, "show_info") as info, \
                        mock.patch.object(self.ssh, "close") as close:
                    worker.signals.finished.emit(report(state))
                info.assert_not_called()
                close.assert_not_called()
                self.assertNotIn("重装完成", self.section.status_label.text())
                self.assertNotIn("备份保留在", self.section.status_label.text())

    def test_backup_path_alone_is_not_a_success_signal(self):
        worker = self.queued_repair()
        with mock.patch.object(toolbox, "show_info") as info, \
                mock.patch.object(self.ssh, "close") as close:
            worker.signals.finished.emit(report(recovery.RecoveryState.BLOCKED, "/retained-backup"))
        info.assert_not_called()
        close.assert_not_called()
        self.assertIn("/retained-backup", self.section.status_label.text())
        self.assertFalse(self.section.repair_button.isEnabled())

    def test_success_calls_backend_and_shows_nonempty_backup_without_closing_new_device(self):
        result = report(recovery.RecoveryState.NOT_NEEDED, "/home/root/recovery-backup")
        with mock.patch.object(recovery, "repair", return_value=result) as repair:
            worker = self.queued_repair()
            value = worker.execute()
        repair.assert_called_once_with(self.ssh, rmtool.app_state_dir())
        old_client = self.ssh._client
        with mock.patch.object(toolbox, "show_info", side_effect=lambda *a: self.reconnect()) as info:
            worker.signals.finished.emit(value)
        old_client.close.assert_called_once_with()
        self.assertTrue(self.ssh.is_connected())
        self.ssh._client.close.assert_not_called()
        self.assertIn(result.backup_path, info.call_args.args[2])
        self.assertIn(result.detail, info.call_args.args[2])
        self.assertIn(result.issues[0], info.call_args.args[2])
        self.assertIn("原生简体中文", info.call_args.args[2])
        self.assertIn("清除紧急停用", info.call_args.args[2])
        self.assertIn("手动重启", info.call_args.args[2])

    def test_stale_repair_completion_never_closes_new_connection(self):
        worker = self.queued_repair()
        self.reconnect()
        with mock.patch.object(self.ssh, "close") as close, \
                mock.patch.object(toolbox, "show_info") as info:
            worker.signals.finished.emit(report(recovery.RecoveryState.NOT_NEEDED, "/backup"))
        close.assert_not_called()
        info.assert_not_called()

    def test_failed_repair_warns_against_restart_and_preserves_backup_detail(self):
        worker = self.queued_repair()
        with mock.patch.object(toolbox, "show_error") as error, \
                mock.patch.object(toolbox, "show_info") as info, \
                mock.patch.object(self.ssh, "close") as close:
            worker.signals.error.emit(RuntimeError("rollback incomplete; /retained-backup"))
        self.assertIn("/retained-backup", error.call_args.args[2])
        self.assertIn("请勿重启或解除紧急停用", error.call_args.args[2])
        self.assertIn("不代表已通过校验", error.call_args.args[2])
        self.assertFalse(self.section.repair_button.isEnabled())
        close.assert_not_called()
        info.assert_not_called()

    def test_download_error_uses_manual_load_and_retry_is_session_bound(self):
        worker = self.queued_repair()
        error = _package_download.PackageDownloadError("plugin", "test.tar.gz", ("https://example.test/pkg",), 10, "a" * 64)
        with mock.patch.object(toolbox, "_show_package_download_error") as manual, \
                mock.patch.object(self.ssh, "close") as close:
            worker.signals.error.emit(error)
        close.assert_not_called()
        self.assertIs(manual.call_args.args[1], error)
        retry = manual.call_args.kwargs["retry"]
        with mock.patch.object(self.section, "_repair") as repair:
            retry()
            repair.assert_called_once_with()
            self.reconnect()
            retry()
            repair.assert_called_once_with()

    def test_other_mutations_invalidate_recovery_authorization(self):
        for method in ("_migrate", "_cleanup_residue", "_cleanup"):
            with self.subTest(method=method):
                self.section._busy = False
                self.section._apply_status(report())
                self.section._report = SimpleNamespace(migratable=True, features=(SimpleNamespace(label="pinyin", enabled=True),))
                self.section._report_session = self.section._session_token()
                with mock.patch.object(toolbox, "ask_confirmation", return_value=True):
                    getattr(self.section, method)()
                self.assertIsNone(self.section._recovery_report)
                self.assertFalse(self.section.repair_button.isEnabled())

    def test_existing_mutations_also_reject_confirmation_and_queue_switches(self):
        for method in ("_migrate", "_cleanup_residue", "_cleanup"):
            for queued in (False, True):
                with self.subTest(method=method, queued=queued):
                    self.pool.reset_mock()
                    self.section._report = SimpleNamespace(migratable=True, features=(SimpleNamespace(label="pinyin", enabled=True),))
                    self.section._report_session = self.section._session_token()
                    confirm = (lambda *a, **k: True) if queued else (lambda *a, **k: (self.reconnect() or True))
                    with mock.patch.object(toolbox, "ask_confirmation", side_effect=confirm):
                        getattr(self.section, method)()
                    if queued:
                        worker = self.pool.start.call_args.args[0]
                        self.reconnect()
                        with self.assertRaisesRegex(RuntimeError, "连接已变化"):
                            worker.execute()
                    else:
                        self.pool.start.assert_not_called()

    def test_old_cleanup_callback_cannot_close_new_device(self):
        self.section._report = SimpleNamespace(features=(SimpleNamespace(label="pinyin"),))
        self.section._report_session = self.section._session_token()
        with mock.patch.object(toolbox, "ask_confirmation", return_value=True):
            self.section._cleanup_residue()
        worker = self.pool.start.call_args.args[0]
        self.reconnect()
        current_text = self.section.status_label.text()
        with mock.patch.object(self.ssh, "close") as close, \
                mock.patch.object(toolbox, "show_info") as info, \
                mock.patch.object(toolbox, "show_error") as error:
            worker.signals.finished.emit(None)
            worker.signals.error.emit(RuntimeError("old cleanup"))
        close.assert_not_called()
        info.assert_not_called()
        error.assert_not_called()
        self.assertEqual(self.section.status_label.text(), current_text)

    def test_migration_report_is_bound_to_detection_session(self):
        result = SimpleNamespace(migratable=True, features=(), blockers=(), detail="verified")
        with mock.patch.object(toolbox._residue_migration, "inspect_residue", return_value=result) as inspect, \
                mock.patch.object(self.section, "_report_text", return_value="verified"):
            self.section._detect()
            worker = self.pool.start.call_args.args[0]
            worker.signals.finished.emit(worker.execute())
        inspect.assert_called_once_with(self.ssh)
        self.assertIs(self.section._report, result)
        self.assertEqual(self.section._report_session, self.section._session_token())

    def test_failed_migration_redetection_does_not_reenable_cached_actions(self):
        self.section._report = SimpleNamespace(migratable=True, features=(SimpleNamespace(label="pinyin", enabled=True),))
        self.section._report_session = self.section._session_token()
        self.section._detect()
        worker = self.pool.start.call_args.args[0]
        with mock.patch.object(toolbox, "show_error"):
            worker.signals.error.emit(RuntimeError("ownership changed"))
        self.assertIsNone(self.section._report)
        self.assertIsNone(self.section._report_session)
        self.assertFalse(self.section.migrate_button.isEnabled())
        self.assertFalse(self.section.cleanup_residue_button.isEnabled())

    def test_migration_actions_reject_report_from_replaced_transport_before_signal(self):
        for method in ("_migrate", "_cleanup_residue"):
            with self.subTest(method=method):
                self.section._report = SimpleNamespace(migratable=True, features=(SimpleNamespace(label="pinyin", enabled=True),))
                self.section._report_session = self.section._session_token()
                self.ssh._client = mock.Mock()
                with mock.patch.object(toolbox, "ask_confirmation") as confirm:
                    getattr(self.section, method)()
                confirm.assert_not_called()
                self.pool.start.assert_not_called()

    def test_migration_actions_reject_report_changed_during_confirmation(self):
        for method in ("_migrate", "_cleanup_residue"):
            with self.subTest(method=method):
                self.section._report = SimpleNamespace(migratable=True, features=(SimpleNamespace(label="pinyin", enabled=True),))
                self.section._report_session = self.section._session_token()

                def replace_report(*args, **kwargs):
                    self.section._report = None
                    return True

                with mock.patch.object(toolbox, "ask_confirmation", side_effect=replace_report):
                    getattr(self.section, method)()
                self.pool.start.assert_not_called()

    def test_existing_mutations_execute_correct_backend_inside_session(self):
        for method, module, backend in (
            ("_migrate", toolbox._residue_migration, "migrate"),
            ("_cleanup_residue", toolbox._residue_migration, "cleanup"),
            ("_cleanup", toolbox._legacy_vellum, "remove_legacy_plugins"),
        ):
            with self.subTest(method=method):
                self.section._busy = False
                self.section._report = SimpleNamespace(migratable=True, features=(SimpleNamespace(label="pinyin", enabled=True),))
                self.section._report_session = self.section._session_token()
                with mock.patch.object(toolbox, "ask_confirmation", return_value=True), \
                        mock.patch.object(module, backend, return_value="result") as operation, \
                        mock.patch.object(self.ssh, "operation_session", wraps=self.ssh.operation_session) as session:
                    getattr(self.section, method)()
                    worker = self.pool.start.call_args.args[0]
                    self.assertEqual(worker.execute(), "result")
                session.assert_called_once_with()
                expected = (self.ssh, rmtool.app_state_dir()) if method == "_migrate" else (self.ssh,)
                operation.assert_called_once_with(*expected)

    def test_detect_all_and_blocked_feature_route_include_recovery(self):
        with mock.patch.object(toolbox.ToolboxTab, "_probe_device_identity"):
            tab = toolbox.ToolboxTab(self.ssh, {})
        self.addCleanup(tab.deleteLater)
        self.assertIn(tab.legacy_plugin_section, tab._detectable_sections)
        with mock.patch.object(tab, "_tap_entry_hidden", return_value=False):
            for section in tab._detectable_sections:
                patch = mock.patch.object(section, "_start_status_detection", side_effect=lambda **kw: kw["on_done"]())
                patch.start()
                self.addCleanup(patch.stop)
            tab._detect_all_statuses()
        self.assertFalse(tab._detect_all_busy)
        tab.legacy_plugin_section._start_status_detection.assert_called_once()
        label = tab.reading_enhancements_section.status_label
        label.setText("检测到不完整或不可验证的安装")
        page = tab.detail_stack.widget(2)
        link = page.findChild(QtWidgets.QPushButton, "pluginRecoveryLink")
        self.assertFalse(link.isHidden())
        tab.search_input.setText("阅读")
        with mock.patch.object(tab.legacy_plugin_section, "_start_status_detection") as detect:
            link.click()
        detect.assert_called_once_with(show_errors=False)
        self.assertEqual(tab.search_input.text(), "")
        self.assertEqual(tab.tool_table.currentRow(), 8)


if __name__ == "__main__":
    unittest.main()
