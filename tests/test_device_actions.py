import os
import threading
import unittest
from datetime import datetime
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

import rmtool
import _device_actions
import _tab_toolbox
import _ui_tasks

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class RecordingClient(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, outputs=None, fail_on=None):
        super().__init__()
        self.commands = []
        self.timeouts = []
        self.threads = []
        self.outputs = outputs or {}
        self.fail_on = fail_on

    def is_connected(self):
        return True

    def exec_checked(self, command, *, timeout=None):
        self.commands.append(command)
        self.timeouts.append(timeout)
        self.threads.append(threading.current_thread())
        if self.fail_on and self.fail_on in command:
            raise RuntimeError("boom")
        return self.outputs.get(command, "")

    def exec_command(self, command, **_kwargs):
        self.commands.append(command)
        self.threads.append(threading.current_thread())
        return "", "", 0


def _drain():
    QtCore.QThreadPool.globalInstance().waitForDone(5000)
    QtWidgets.QApplication.processEvents()


class DeviceActionsTests(unittest.TestCase):
    def test_sync_time_remounts_and_restores_read_only(self):
        client = RecordingClient()
        stamp = _device_actions.sync_time(client, datetime(2026, 1, 2, 3, 4, 5))
        self.assertEqual(stamp, "2026-01-02 03:04:05")
        self.assertEqual(
            client.commands,
            [
                "mount -o remount,rw /",
                'date -s "2026-01-02 03:04:05"',
                "hwclock -w",
                "mount -o remount,ro /",
            ],
        )
        # Quick commands must not inherit the 30-minute installer deadline.
        self.assertIn(_device_actions.QUICK_COMMAND_TIMEOUT, client.timeouts)

    def test_failure_still_remounts_read_only(self):
        client = RecordingClient(fail_on="hwclock")
        with self.assertRaises(RuntimeError):
            _device_actions.sync_time(client)
        self.assertEqual(client.commands[-1], "mount -o remount,ro /")

    def test_set_timezone_rejects_shell_metacharacters(self):
        client = RecordingClient()
        with self.assertRaises(ValueError):
            _device_actions.set_timezone(client, "Asia/Shanghai; reboot")
        self.assertEqual(client.commands, [])
        self.assertEqual(_device_actions.set_timezone(client, "America/New_York"), "America/New_York")
        self.assertEqual(_device_actions.set_timezone(client, "Etc/GMT-8"), "Etc/GMT-8")
        for bad in ("../etc", "a b", "Asia/$(id)", ""):
            with self.assertRaises(ValueError):
                _device_actions.set_timezone(client, bad)
        self.assertIn("timedatectl set-timezone America/New_York", client.commands)

    def test_time_info_strips_output(self):
        client = RecordingClient(outputs={"date": " now \n"})
        rows = _device_actions.time_info(client)
        self.assertEqual(rows[0], ("系统时间", "now"))
        self.assertEqual(len(rows), 3)

    def test_brightness_writes_persistent_service(self):
        client = RecordingClient()
        _device_actions.increase_brightness(client)
        joined = "\n".join(client.commands)
        self.assertIn("tweak-brightness-slider.service", joined)
        self.assertIn("systemctl enable --now tweak-brightness-slider.service", joined)
        self.assertEqual(client.commands[-1], "mount -o remount,ro /")


class BackgroundSlotTests(unittest.TestCase):
    def test_time_tab_runs_ssh_off_the_main_thread(self):
        client = RecordingClient()
        tab = _tab_toolbox.TimeTab(client)
        self.addCleanup(tab.deleteLater)
        tab._sync_time()
        self.assertFalse(tab.sync_button.isEnabled())
        _drain()
        self.assertTrue(client.threads)
        self.assertTrue(all(t is not threading.main_thread() for t in client.threads))
        self.assertTrue(tab.sync_button.isEnabled())
        self.assertIn("已同步设备时间到", tab.output.toPlainText())

    def test_control_tab_reports_errors_and_reenables(self):
        client = RecordingClient(fail_on="rm-ssh-over-wlan")
        tab = _tab_toolbox.ControlTab(client)
        self.addCleanup(tab.deleteLater)
        with mock.patch.object(_ui_tasks, "show_error") as show_error:
            tab._enable_wifi_ssh()
            _drain()
        show_error.assert_called_once()
        self.assertIn("操作失败", show_error.call_args.args[2])
        self.assertTrue(tab.enable_wifi_ssh_button.isEnabled())

    def test_callbacks_skipped_after_owner_deleted(self):
        widget = QtWidgets.QWidget()
        gate = threading.Event()
        called = []
        _ui_tasks.run_in_background(widget, gate.wait, 5, on_success=called.append)
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        gate.set()
        _drain()
        self.assertEqual(called, [])

    def test_worker_class_is_the_shared_rmtool_worker(self):
        worker = _ui_tasks.run_in_background(QtWidgets.QWidget(), lambda: None)
        self.assertIsInstance(worker, rmtool.Worker)
        _drain()


if __name__ == "__main__":
    unittest.main()
