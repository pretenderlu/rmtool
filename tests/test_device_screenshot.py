import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
from PyQt5 import QtCore, QtWidgets

import _device_screenshot as screenshot
import rmtool
import _tab_toolbox


_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def png_bytes(size=(24, 18)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeSFTP:
    def __init__(self, files=None):
        self.files = files or {}

    def open(self, path, _mode="rb"):
        return io.BytesIO(self.files[path])

    def stat(self, path):
        return SimpleNamespace(st_mode=0o100600, st_size=len(self.files[path]))


class FakeSSH:
    def __init__(self, *, commands=None, files=None):
        self.commands = []
        self.responses = list(commands or [])
        self.sftp = FakeSFTP(files)
        self.exec_checked = mock.Mock()

    @contextmanager
    def operation_session(self):
        yield

    @contextmanager
    def sftp_session(self):
        yield self.sftp

    def exec_command(self, command, *, timeout=1800):
        self.commands.append(command)
        if self.responses:
            return self.responses.pop(0)
        return "", "", 0


class FakeUiSSH(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, connected=True):
        super().__init__()
        self.connected = connected

    def is_connected(self):
        return self.connected


class DeviceScreenshotTests(unittest.TestCase):
    def test_config_update_preserves_crlf_and_unrelated_content(self):
        original = (
            b"[General]\r\nlanguage=fr_FR\r\nScreenshot=false\r\n"
            b"Screenshot=true\r\n\r\n[Other]\r\nvalue=\xff\r\n"
        )
        changed = screenshot.set_screenshot_config(original, True)
        self.assertEqual(
            changed,
            b"[General]\r\nlanguage=fr_FR\r\nScreenshot=true\r\n"
            b"\r\n[Other]\r\nvalue=\xff\r\n",
        )
        self.assertTrue(screenshot.screenshot_configured(changed))

    def test_config_update_adds_general_section(self):
        self.assertEqual(
            screenshot.set_screenshot_config(b"[Other]\nvalue=1", False),
            b"[Other]\nvalue=1\n[General]\nScreenshot=false\n",
        )

    def test_status_separates_native_setting_from_direct_capture(self):
        ssh = FakeSSH(commands=[
            ("", "", 0),
            (screenshot.MOVE_MACHINE + "\n", "", 0),
            ("", "", 0),
        ])
        with mock.patch.object(
            screenshot,
            "_read_config",
            return_value=b"[General]\nScreenshot=false\n",
        ):
            status = screenshot.get_status(ssh)
        self.assertEqual(status.state, screenshot.ScreenshotState.DISABLED)
        self.assertTrue(status.direct_supported)
        self.assertTrue(status.ready)

    def test_unverified_machine_is_not_direct_capture_ready(self):
        ssh = FakeSSH(commands=[
            ("", "", 0),
            ("reMarkable Ferrari\n", "", 0),
        ])
        with mock.patch.object(
            screenshot,
            "_read_config",
            return_value=b"[General]\nScreenshot=true\n",
        ):
            status = screenshot.get_status(ssh)
        self.assertEqual(status.state, screenshot.ScreenshotState.READY)
        self.assertFalse(status.ready)

    def test_missing_config_can_be_created_with_safe_mode(self):
        ssh = FakeSSH()
        disabled = screenshot.ScreenshotStatus(
            screenshot.ScreenshotState.DISABLED, False, True, screenshot.MOVE_MACHINE
        )
        enabled = screenshot.ScreenshotStatus(
            screenshot.ScreenshotState.READY, True, True, screenshot.MOVE_MACHINE
        )
        with (
            mock.patch.object(screenshot, "get_status", side_effect=[disabled, enabled]),
            mock.patch.object(screenshot, "_read_config", return_value=None),
            mock.patch.object(screenshot, "_write_remote_atomic") as write,
        ):
            self.assertEqual(screenshot.set_enabled(ssh, True), enabled)
        write.assert_called_once_with(
            ssh, screenshot.CONFIG_PATH, b"[General]\nScreenshot=true\n", 0o600
        )

    def test_move_capture_encodes_png_and_cleans_remote_buffer(self):
        raw = bytes([255, 255, 255, 255]) * (
            screenshot.MOVE_BUFFER_WIDTH * screenshot.MOVE_SCREEN_HEIGHT
        )
        remote = "/tmp/rmtool-screenshot-capture.raw"
        ssh = FakeSSH(files={remote: raw})
        ready = screenshot.ScreenshotStatus(
            screenshot.ScreenshotState.READY, True, True, screenshot.MOVE_MACHINE
        )
        with (
            mock.patch.object(screenshot, "get_status", return_value=ready),
            mock.patch.object(
                screenshot.uuid, "uuid4", return_value=SimpleNamespace(hex="capture")
            ),
        ):
            result = screenshot.capture(ssh)
        self.assertEqual((result.width, result.height), (954, 1696))
        self.assertEqual(Image.open(io.BytesIO(result.png)).size, (954, 1696))
        command = next(command for command in ssh.commands if "/proc/$pid/mem" in command)
        self.assertNotIn("USR2", command)
        ssh.exec_checked.assert_called_once_with(f"rm -f {remote}", timeout=10)

    def test_capture_failure_still_cleans_remote_buffer(self):
        ssh = FakeSSH(commands=[("", "failed", 45)])
        ready = screenshot.ScreenshotStatus(
            screenshot.ScreenshotState.READY, True, True, screenshot.MOVE_MACHINE
        )
        with (
            mock.patch.object(screenshot, "get_status", return_value=ready),
            mock.patch.object(
                screenshot.uuid, "uuid4", return_value=SimpleNamespace(hex="capture")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "读取设备当前画面失败"):
                screenshot.capture(ssh)
        ssh.exec_checked.assert_called_once_with(
            "rm -f /tmp/rmtool-screenshot-capture.raw", timeout=10
        )

    def test_atomic_local_save_preserves_existing_file_on_replace_failure(self):
        data = png_bytes()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "screen.png"
            target.write_bytes(b"old")
            with mock.patch.object(os, "replace", side_effect=OSError("busy")):
                with self.assertRaises(OSError):
                    screenshot.save_png_atomic(data, target)
            self.assertEqual(target.read_bytes(), b"old")


class DeviceScreenshotUiTests(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeUiSSH()
        self.section = _tab_toolbox.DeviceScreenshotSection(self.ssh)
        self.addCleanup(self.section.deleteLater)

    def test_native_and_direct_buttons_have_independent_states(self):
        self.section._apply_status(
            screenshot.ScreenshotStatus(
                screenshot.ScreenshotState.DISABLED,
                False,
                True,
                screenshot.MOVE_MACHINE,
            )
        )
        self.assertTrue(self.section.enable_button.isEnabled())
        self.assertTrue(self.section.capture_button.isEnabled())

    def test_worker_result_from_previous_connection_is_discarded(self):
        pool = mock.Mock()
        self.section.thread_pool = pool
        callback = mock.Mock()
        self.section._start_worker(
            lambda: "old", pending="busy", on_success=callback, error_prefix="failed"
        )
        worker = pool.start.call_args.args[0]
        self.ssh.connected = False
        self.ssh.connection_changed.emit(False)
        worker.signals.finished.emit("old")
        callback.assert_not_called()
        self.assertEqual(self.section.status_label.text(), "设备未连接")


if __name__ == "__main__":
    unittest.main()
