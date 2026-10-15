import os
import threading
import unittest
from datetime import datetime
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

import rmtool
import _device_actions
import _ssh
import _tab_toolbox
import _toolbox_common
import _ui_tasks

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class RecordingClient(_ssh.SSHClientWrapper):
    def __init__(self, outputs=None, fail_on=None):
        super().__init__()
        self.commands = []
        self.timeouts = []
        self.threads = []
        self.outputs = outputs or {}
        self.fail_on = fail_on
        self._client = mock.Mock()

    def reconnect(self):
        with self._transport_lock:
            self.close()
            with self._state_lock:
                self._client = mock.Mock()
            self.connection_changed.emit(True)

    def exec_checked(self, command, *, timeout=None):
        with self._transport_lock:
            self.ensure_client()
            self.commands.append(command)
            self.timeouts.append(timeout)
            self.threads.append(threading.current_thread())
            if self.fail_on and self.fail_on in command:
                raise RuntimeError("boom")
            return self.outputs.get(command, "")

    def exec_command(self, command, **_kwargs):
        return self.exec_checked(command), "", 0


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
    def test_stale_callbacks_do_not_report_or_unlock_a_newer_task(self):
        for failed in (False, True):
            for old_first in (False, True):
                with self.subTest(failed=failed, old_first=old_first):
                    client = RecordingClient()
                    owner = QtWidgets.QWidget()
                    button = QtWidgets.QPushButton(owner)
                    self.addCleanup(owner.deleteLater)
                    succeeded = mock.Mock()
                    with mock.patch.object(QtCore.QThreadPool, "start"), \
                            mock.patch.object(_ui_tasks, "show_error") as error:
                        old = _ui_tasks.run_in_background(
                            owner, lambda _client: "old", client,
                            on_success=succeeded, disable=(button,),
                        )
                        result = old.fn()
                        client.reconnect()
                        new = _ui_tasks.run_in_background(
                            owner, lambda _client: "new", client,
                            on_success=succeeded, disable=(button,),
                        )
                        if not old_first:
                            new.run()
                        if failed:
                            old.signals.error.emit(RuntimeError("old failure"))
                        else:
                            old.signals.finished.emit(result)
                        if old_first:
                            self.assertFalse(button.isEnabled())
                            succeeded.assert_not_called()
                            new.run()
                        succeeded.assert_called_once_with("new")
                        error.assert_not_called()
                        self.assertTrue(button.isEnabled())

    def test_disconnect_cancels_remaining_commands_before_reconnect(self):
        client = RecordingClient()
        owner = QtWidgets.QWidget()
        self.addCleanup(owner.deleteLater)
        paused = threading.Event()
        release = threading.Event()
        reconnect_started = threading.Event()
        reconnected = threading.Event()
        original = client.exec_checked
        succeeded = mock.Mock()

        def execute(command, **kwargs):
            result = original(command, **kwargs)
            if command.startswith("date -s "):
                paused.set()
                if not release.wait(5):
                    raise RuntimeError("Test did not release device action")
            return result

        def reconnect():
            reconnect_started.set()
            client.reconnect()
            reconnected.set()

        with mock.patch.object(client, "exec_checked", side_effect=execute), \
                mock.patch.object(QtCore.QThreadPool, "start"), \
                mock.patch.object(_ui_tasks, "show_error") as error:
            worker = _ui_tasks.run_in_background(
                owner, _device_actions.sync_time, client, on_success=succeeded
            )
            failures = []
            worker.signals.error.connect(failures.append)
            first = threading.Thread(target=worker.run)
            second = threading.Thread(target=reconnect)
            first.start()
            try:
                self.assertTrue(paused.wait(3))
                client.close()
                self.assertFalse(client.is_connected())
                second.start()
                self.assertTrue(reconnect_started.wait(3))
                self.assertFalse(reconnected.wait(0.1))
            finally:
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
                QtWidgets.QApplication.processEvents()
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertTrue(reconnected.is_set())
            self.assertEqual(len(failures), 1)
            self.assertEqual(len(client.commands), 2)
            self.assertEqual(client.commands[0], "mount -o remount,rw /")
            self.assertTrue(client.commands[1].startswith("date -s "))
            succeeded.assert_not_called()
            error.assert_not_called()

    def test_queued_reboots_never_run_on_a_replacement_connection(self):
        for kind in (_tab_toolbox.ControlTab, _tab_toolbox.FontTab):
            with self.subTest(kind=kind.__name__):
                client = RecordingClient()
                tab = kind(client) if kind is _tab_toolbox.ControlTab else kind(client, rmtool._default_config())
                self.addCleanup(tab.deleteLater)
                with mock.patch.object(_toolbox_common, "ask_confirmation", return_value=True), \
                        mock.patch.object(QtCore.QThreadPool, "start") as queued, \
                        mock.patch.object(_toolbox_common, "show_info") as info, \
                        mock.patch.object(_ui_tasks, "show_error") as error, \
                        mock.patch.object(rmtool.logging, "exception"):
                    tab._restart_device()
                    worker = queued.call_args.args[0]
                    failures = []
                    worker.signals.error.connect(failures.append)
                    client.reconnect()
                    worker.run()
                self.assertEqual(client.commands, [])
                self.assertEqual(len(failures), 1)
                self.assertIn("连接已变化", str(failures[0]))
                info.assert_not_called()
                error.assert_not_called()
                self.assertTrue(tab.restart_button.isEnabled())

    def test_cross_page_actions_hold_the_lock_until_cleanup_finishes(self):
        for fail_on in (None, "hwclock"):
            with self.subTest(fail_on=fail_on):
                client = RecordingClient(fail_on=fail_on)
                time_tab = _tab_toolbox.TimeTab(client)
                control_tab = _tab_toolbox.ControlTab(client)
                self.addCleanup(time_tab.deleteLater)
                self.addCleanup(control_tab.deleteLater)
                time_paused = threading.Event()
                release_time = threading.Event()
                brightness_started = threading.Event()
                brightness_command = threading.Event()
                original = client.exec_checked

                def execute(command, **kwargs):
                    if threading.current_thread().name == "brightness":
                        brightness_command.set()
                    result = original(command, **kwargs)
                    if command.startswith("date -s "):
                        time_paused.set()
                        if not release_time.wait(5):
                            raise RuntimeError("Test did not release time sync")
                    return result

                with mock.patch.object(client, "exec_checked", side_effect=execute), \
                        mock.patch.object(QtCore.QThreadPool, "start") as queued, \
                        mock.patch.object(_toolbox_common, "show_info"), \
                        mock.patch.object(_ui_tasks, "show_error") as errors:
                    time_tab._sync_time()
                    time_worker = queued.call_args.args[0]
                    control_tab._increase_brightness()
                    brightness_worker = queued.call_args.args[0]

                    def brightness():
                        brightness_started.set()
                        brightness_worker.run()

                    first = threading.Thread(target=time_worker.run, name="sync")
                    second = threading.Thread(target=brightness, name="brightness")
                    first.start()
                    try:
                        self.assertTrue(time_paused.wait(3))
                        second.start()
                        self.assertTrue(brightness_started.wait(3))
                        self.assertFalse(brightness_command.wait(0.1))
                    finally:
                        release_time.set()
                        first.join(5)
                        if second.ident is not None:
                            second.join(5)
                        QtWidgets.QApplication.processEvents()
                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                    self.assertEqual(errors.call_count, int(fail_on is not None))
                names = [thread.name for thread in client.threads]
                split = names.index("brightness")
                self.assertTrue(all(name == "sync" for name in names[:split]))
                self.assertTrue(all(name == "brightness" for name in names[split:]))
                self.assertEqual(client.commands[split - 1], "mount -o remount,ro /")
                self.assertEqual(client.commands[-1], "mount -o remount,ro /")
                self.assertIn("systemctl enable --now tweak-brightness-slider.service", client.commands)
                self.assertTrue(time_tab.sync_button.isEnabled())
                self.assertTrue(control_tab.brightness_button.isEnabled())

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
        _ui_tasks.run_in_background(
            widget, lambda _client: gate.wait(5), RecordingClient(), on_success=called.append
        )
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        gate.set()
        _drain()
        self.assertEqual(called, [])

    def test_worker_class_is_the_shared_rmtool_worker(self):
        worker = _ui_tasks.run_in_background(QtWidgets.QWidget(), lambda _client: None, RecordingClient())
        self.assertIsInstance(worker, rmtool.Worker)
        _drain()


if __name__ == "__main__":
    unittest.main()
