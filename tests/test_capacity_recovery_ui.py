import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets, sip

import _capacity_recovery as recovery
import _dialogs
import _rmkit_cn
import rmtool
import _tab_toolbox as toolbox
import _tap_page_turn as tap
import _toolbox_common as toolbox_common
from _ssh import SSHClientWrapper
from _toolbox_recovery import CapacityRecoverySection


APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
IDENTITY = tap.DeviceIdentity("firmware", "rm2", "armv7l", "a" * 64)
PACKAGE = SimpleNamespace(package_id="package")
CONTEXT = recovery.CapacityContext("shared-capacity", "/", 100, 40, 60)
ENABLE_PLAN = "shared-enable-plan"
SNAPSHOT = _rmkit_cn.HomeOnlyFontState(
    "/home/root/.local/share/fonts",
    "selected.ttf",
    "/home/root/.local/share/fonts/selected.ttf",
    "/data/rmtool/fonts/active-ui-font.ttf",
    "Selected",
    "b" * 64,
    "c" * 64,
    "d" * 64,
    1000,
)


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


class PendingWorker:
    def __init__(self, fn, *args, **kwargs):
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = SimpleNamespace(finished=Signal(), error=Signal())

    def execute(self):
        return self.fn(*self.args, **self.kwargs)


def capacity_error():
    error = RuntimeError("空间不足")
    error.recovery_context = {
        "kind": CONTEXT.kind,
        "partition": CONTEXT.partition,
        "required": CONTEXT.required,
        "available": CONTEXT.available,
        "shortfall": CONTEXT.shortfall,
    }
    return error


class CapacityRecoveryUiTests(unittest.TestCase):
    def setUp(self):
        self.ssh = SSHClientWrapper()
        self.ssh._client = mock.Mock()
        self.ssh._client.get_transport.return_value.is_active.return_value = True
        self.ssh.exec_checked = mock.Mock(return_value="")
        self.pool = mock.Mock()
        patcher = mock.patch.object(rmtool, "Worker", PendingWorker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.section = CapacityRecoverySection(self.ssh)
        self.section.thread_pool = self.pool
        self.addCleanup(
            lambda: None if sip.isdeleted(self.section) else self.section.deleteLater()
        )
        self.intent = recovery.make_original_intent(
            self.ssh, "pinyin-input", PACKAGE, ".rmtool", IDENTITY
        )
        self.plan = recovery.CapacityPlan(
            self.intent, CONTEXT, recovery.HomeOnlyFontPlan(SNAPSHOT), ENABLE_PLAN
        )

    def reconnect(self):
        old_client = self.ssh._client
        with self.ssh._transport_lock, self.ssh._state_lock:
            self.ssh._client = None
        old_client.close()
        self.ssh.connection_changed.emit(False)
        with self.ssh._transport_lock, self.ssh._state_lock:
            self.ssh._client = mock.Mock()
            self.ssh._client.get_transport.return_value.is_active.return_value = True
            self.ssh.connection_changed.emit(True)

    def scan(self):
        with mock.patch.object(recovery, "scan", return_value=self.plan) as scan:
            self.section.open_intent(self.intent, capacity_error())
            self.section.open_intent(self.intent, capacity_error())
            self.pool.start.assert_called_once()
            worker = self.pool.start.call_args.args[0]
            worker.signals.finished.emit(worker.execute())
        scan.assert_called_once_with(self.ssh, self.intent)
        return worker

    def queue_execute(self):
        self.scan()
        self.pool.reset_mock()
        self.section.home_only_checkbox.setChecked(True)
        with mock.patch.object(toolbox_common, "ask_confirmation", return_value=True):
            self.section._execute()
        return self.pool.start.call_args.args[0]

    def test_error_entry_is_single_scan_and_candidate_defaults_unselected(self):
        self.scan()
        self.assertTrue(self.section.home_only_checkbox.isVisibleTo(self.section))
        self.assertFalse(self.section.home_only_checkbox.isChecked())
        self.assertFalse(self.section.execute_button.isEnabled())
        self.assertIn("锁屏用默认字体", self.section.home_only_checkbox.text())

    def test_cancel_and_confirmation_drift_start_no_worker(self):
        self.scan()
        self.pool.reset_mock()
        self.section.home_only_checkbox.setChecked(True)
        with mock.patch.object(
            toolbox_common, "ask_confirmation", return_value=False
        ) as confirm:
            self.section._execute()
        self.assertTrue(confirm.call_args.kwargs["danger"])
        self.pool.start.assert_not_called()

        other = recovery.CapacityPlan(
            self.intent,
            recovery.CapacityContext("shared-capacity", "/", 101, 40, 61),
            self.plan.home_only,
            ENABLE_PLAN,
        )
        with mock.patch.object(
            toolbox_common, "ask_confirmation",
            side_effect=lambda *args, **_kwargs: setattr(self.section, "_plan", other) or True,
        ):
            self.section._execute()
        self.pool.start.assert_not_called()

    def test_only_deployed_closes_original_connection(self):
        worker = self.queue_execute()
        old_client = self.ssh._client
        deployed = recovery.CapacityResult("deployed", "已部署，请手动重启", cleaned=True)
        worker.signals.finished.emit(deployed)
        old_client.close.assert_called_once_with()
        self.assertEqual(self.section.status_label.text(), "已部署，请手动重启")

        for state in ("insufficient", "preflight_failed", "install_failed"):
            with self.subTest(state=state), mock.patch.object(self.ssh, "close") as close:
                self.section._apply_result(
                    recovery.CapacityResult(state, "停止", cleaned=True)
                )
                close.assert_not_called()

    def test_result_uses_measured_before_after_capacity(self):
        before = recovery.CapacityContext(
            "shared-capacity", "/", 100 * 1048576, 40 * 1048576, 60 * 1048576
        )
        after = recovery.CapacityContext(
            "shared-capacity", "/", 100 * 1048576, 90 * 1048576, 10 * 1048576
        )
        result = recovery.CapacityResult(
            "install_failed",
            "安装失败",
            after,
            True,
            before,
            after,
        )
        self.section._apply_result(result)
        text = self.section.capacity_label.text()
        self.assertIn("清理前", text)
        self.assertIn("清理后", text)
        self.assertIn("实际释放 50.00 MiB", text)
        self.assertNotIn(str(SNAPSHOT.size), text)

    def test_fixed_entry_fresh_detect_reconnect_and_reconfirmation(self):
        self.assertTrue(self.section.detect_button.isEnabled())
        with mock.patch.object(
            recovery, "fresh_intent", return_value=self.intent
        ) as fresh, mock.patch.object(recovery, "scan", return_value=self.plan):
            self.section._start_scan()
            first = self.pool.start.call_args.args[0]
            first.signals.finished.emit(first.execute())
            second = self.pool.start.call_args.args[0]
            second.signals.finished.emit(second.execute())
        fresh.assert_called_once()
        self.assertIs(self.section._plan, self.plan)

        self.reconnect()
        self.pool.reset_mock()
        new_intent = recovery.make_original_intent(
            self.ssh, "pinyin-input", PACKAGE, ".rmtool", IDENTITY
        )
        new_plan = recovery.CapacityPlan(
            new_intent, CONTEXT, self.plan.home_only, ENABLE_PLAN
        )
        with mock.patch.object(
            recovery, "fresh_intent", return_value=new_intent
        ) as fresh, mock.patch.object(recovery, "scan", return_value=new_plan):
            self.section._start_scan()
            first = self.pool.start.call_args.args[0]
            first.signals.finished.emit(first.execute())
            second = self.pool.start.call_args.args[0]
            second.signals.finished.emit(second.execute())
        fresh.assert_called_once()
        self.assertIs(self.section._intent, new_intent)
        self.assertIsNot(self.section._intent, self.intent)

        self.pool.reset_mock()
        self.section.home_only_checkbox.setChecked(True)
        with mock.patch.object(toolbox_common, "ask_confirmation", return_value=True), \
                mock.patch.object(recovery, "execute") as execute:
            self.section._execute()
            worker = self.pool.start.call_args.args[0]
            worker.execute()
        execute.assert_called_once_with(self.ssh, new_plan)

    def test_fresh_detect_connection_change_stops_before_backend(self):
        with mock.patch.object(recovery, "fresh_intent") as fresh:
            self.section._start_scan()
            worker = self.pool.start.call_args.args[0]
            self.reconnect()
            with self.assertRaisesRegex(RuntimeError, "连接已变化"):
                worker.execute()
        fresh.assert_not_called()
        self.assertIsNone(self.section._plan)

    def test_stale_callback_does_not_pollute_or_close_new_connection(self):
        worker = self.queue_execute()
        self.reconnect()
        self.pool.reset_mock()
        text = self.section.status_label.text()
        with mock.patch.object(self.ssh, "close") as close:
            worker.signals.finished.emit(
                recovery.CapacityResult("deployed", "旧成功", cleaned=True)
            )
            worker.signals.error.emit(RuntimeError("旧错误"))
        close.assert_not_called()
        self.pool.start.assert_not_called()
        self.assertEqual(self.section.status_label.text(), text)

    def test_structured_error_action_routes_only_explicit_acceptance(self):
        handler = mock.Mock()
        _dialogs.set_capacity_recovery_handler(handler)
        self.addCleanup(_dialogs.set_capacity_recovery_handler, None)
        error = capacity_error()
        with mock.patch.object(_dialogs, "_show_message", return_value=1) as message:
            _dialogs.show_error(
                None,
                "title",
                "message",
                error=error,
                recovery_intent=self.intent,
            )
        self.assertEqual(message.call_args.kwargs["primary_text"], "修复空间并继续")
        handler.assert_called_once_with(self.intent, error)

        handler.reset_mock()
        with mock.patch.object(_dialogs, "_show_message", return_value=0) as message:
            _dialogs.show_error(
                None,
                "title",
                "message",
                error=capacity_error(),
                recovery_intent=self.intent,
            )
            _dialogs.show_error(
                None,
                "title",
                "message",
                error=RuntimeError("普通错误"),
                recovery_intent=self.intent,
            )
        handler.assert_not_called()
        self.assertEqual(message.call_args.kwargs["primary_text"], "知道了")

    def test_danger_confirmation_defaults_to_cancel(self):
        dialog = _dialogs._build_dialog(
            None,
            "title",
            "heading",
            "message",
            kind="confirm",
            primary_text="删除",
            secondary_text="取消",
            primary_role="danger",
        )
        primary = dialog.findChild(QtWidgets.QPushButton, "appDialogPrimary")
        secondary = dialog.findChild(QtWidgets.QPushButton, "appDialogSecondary")
        self.assertFalse(primary.isDefault())
        self.assertTrue(secondary.isDefault())
        dialog.deleteLater()

    def test_original_installers_create_bound_intents(self):
        import _pinyin_input as pinyin
        import _qr_upload as qr
        from _toolbox_localization import PinyinInputSection
        from _toolbox_reading import QrUploadSection

        pinyin_section = PinyinInputSection(self.ssh)
        qr_section = QrUploadSection(self.ssh)
        for section in (pinyin_section, qr_section):
            self.addCleanup(section.deleteLater)
        cases = (
            (
                pinyin_section,
                pinyin.PinyinInputStatus(
                    pinyin.PinyinInputState.NOT_INSTALLED, IDENTITY, PACKAGE
                ),
                pinyin.FEATURE_ID,
                pinyin_section._enable,
            ),
            (
                qr_section,
                qr.QrUploadStatus(qr.QrUploadState.NOT_INSTALLED, IDENTITY, PACKAGE),
                qr.FEATURE_ID,
                qr_section._install,
            ),
        )
        for section, status, feature_id, action in cases:
            with self.subTest(feature=feature_id), mock.patch.object(
                toolbox_common, "ask_confirmation", return_value=True
            ), mock.patch.object(toolbox_common, "start_plugin_worker") as start:
                section._status = status
                action()
            intent = start.call_args.kwargs["recovery_intent"]
            self.assertIs(intent.ssh_client, self.ssh)
            self.assertIs(intent.client, self.ssh._client)
            self.assertEqual(intent.device, recovery._identity_tuple(IDENTITY))
            self.assertEqual(intent.feature_id, feature_id)
            self.assertIs(intent.package, PACKAGE)
            self.assertTrue(intent.state_dir)

    def test_worker_passes_original_error_and_intent_to_dialog(self):
        error = capacity_error()
        def fail():
            raise error
        worker = toolbox_common.start_plugin_worker(
            self.section, fail, pending="working", recovery_intent=self.intent
        )
        with self.assertRaisesRegex(RuntimeError, "空间不足"):
            worker.execute()
        with mock.patch.object(toolbox_common, "show_error") as show:
            worker.signals.error.emit(error)
        self.assertIs(show.call_args.kwargs["error"], error)
        self.assertIs(show.call_args.kwargs["recovery_intent"], self.intent)

    def test_compact_and_high_scale_layouts_keep_actions_inside_viewport(self):
        self.section.home_only_checkbox.setVisible(True)
        self.section.show()
        self.addCleanup(self.section.hide)
        for width, height, scale in ((1024, 640, 1), (740, 600, 1), (740, 600, 2)):
            with self.subTest(width=width, height=height, scale=scale):
                self.section.resize(width, height)
                font = self.section.font()
                font.setPixelSize(16 * scale)
                self.section.setFont(font)
                self.section.layout().activate()
                QtWidgets.QApplication.processEvents()
                self.assertLessEqual(self.section.minimumSizeHint().width(), width)
                for widget in (
                    self.section.home_only_checkbox,
                    self.section.detect_button,
                    self.section.execute_button,
                    self.section.diagnostics_button,
                ):
                    self.assertTrue(widget.isVisibleTo(self.section))
                    self.assertTrue(self.section.rect().contains(widget.geometry()))

    def test_fixed_and_error_entries_share_one_section(self):
        with mock.patch.object(
            toolbox.QtCore.QThreadPool, "globalInstance", return_value=self.pool
        ):
            tab = toolbox.ToolboxTab(self.ssh, {})
        self.addCleanup(tab.deleteLater)
        entries = [
            entry for entry in tab._tool_entries
            if entry["title"] == "设备修复与清理"
        ]
        self.assertEqual(len(entries), 1)
        tab.show_capacity_recovery()
        self.assertIs(entries[0]["section"], tab.capacity_recovery_section)
        self.assertEqual(tab.tool_table.currentRow(), tab._tool_entries.index(entries[0]))


if __name__ == "__main__":
    unittest.main()
