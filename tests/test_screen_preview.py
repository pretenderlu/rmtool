import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
from PyQt5 import QtWidgets

import _screen_preview as preview
import rmtool
from _tab_screen_preview import LIVE_PREVIEW_COOLDOWN_MS, ScreenPreviewTab


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


class ScreenPreviewBackendTests(unittest.TestCase):
    def test_status_requires_exact_verified_machine_and_readable_buffer(self):
        supported = FakeSSH(
            commands=[(preview.MOVE_MACHINE + "\n", "", 0), ("", "", 0)]
        )
        other = FakeSSH(commands=[("reMarkable Ferrari\n", "", 0)])

        self.assertTrue(preview.get_status(supported).supported)
        self.assertFalse(preview.get_status(other).supported)

    def test_capture_encodes_visible_screen_and_cleans_remote_buffer(self):
        raw = bytes([255, 255, 255, 255]) * (
            preview.MOVE_BUFFER_WIDTH * preview.MOVE_SCREEN_HEIGHT
        )
        remote = "/tmp/rmtool-screen-preview-capture.raw"
        ssh = FakeSSH(files={remote: raw})
        with (
            mock.patch.object(
                preview,
                "get_status",
                return_value=preview.PreviewStatus(True, preview.MOVE_MACHINE),
            ),
            mock.patch.object(
                preview.uuid, "uuid4", return_value=SimpleNamespace(hex="capture")
            ),
        ):
            frame = preview.capture(ssh)

        self.assertEqual((frame.width, frame.height), (954, 1696))
        self.assertEqual(Image.open(io.BytesIO(frame.png)).size, (954, 1696))
        command = next(command for command in ssh.commands if "/proc/$pid/mem" in command)
        self.assertNotIn("USR2", command)
        ssh.exec_checked.assert_called_once_with(f"rm -f {remote}", timeout=10)

    def test_capture_failure_still_cleans_remote_buffer(self):
        ssh = FakeSSH(commands=[("", "failed", 45)])
        with (
            mock.patch.object(
                preview,
                "get_status",
                return_value=preview.PreviewStatus(True, preview.MOVE_MACHINE),
            ),
            mock.patch.object(
                preview.uuid, "uuid4", return_value=SimpleNamespace(hex="capture")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "读取设备当前画面失败"):
                preview.capture(ssh)
        ssh.exec_checked.assert_called_once_with(
            "rm -f /tmp/rmtool-screen-preview-capture.raw", timeout=10
        )

    def test_atomic_save_preserves_existing_file_on_replace_failure(self):
        data = png_bytes()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "screen.png"
            target.write_bytes(b"old")
            with mock.patch.object(os, "replace", side_effect=OSError("busy")):
                with self.assertRaises(OSError):
                    preview.save_png_atomic(data, target)
            self.assertEqual(target.read_bytes(), b"old")


class ScreenPreviewUiTests(unittest.TestCase):
    def setUp(self):
        self.tab = ScreenPreviewTab(mock.Mock())
        self.addCleanup(self.tab.deleteLater)
        self.pool = mock.Mock()
        self.tab.thread_pool = self.pool
        self.tab.set_connection_state(True)
        self.tab.set_page_active(True)
        detection = self.pool.start.call_args.args[0]
        detection.signals.finished.emit(
            preview.PreviewStatus(True, preview.MOVE_MACHINE)
        )
        self.pool.reset_mock()

    def test_continuous_preview_schedules_next_frame_after_capture(self):
        self.tab.start_button.click()
        worker = self.pool.start.call_args.args[0]
        worker.signals.finished.emit(preview.PreviewFrame(png_bytes(), 24, 18))

        self.assertTrue(self.tab._previewing)
        self.assertFalse(self.tab.start_button.isEnabled())
        self.assertTrue(self.tab.stop_button.isEnabled())
        self.assertTrue(self.tab.timer.isActive())
        self.assertEqual(self.tab.timer.interval(), LIVE_PREVIEW_COOLDOWN_MS)
        self.assertIsNotNone(self.tab._latest_png)

    def test_leaving_page_stops_continuous_preview(self):
        self.tab.start_button.click()
        self.tab.set_page_active(False)

        self.assertFalse(self.tab._previewing)
        self.assertFalse(self.tab.timer.isActive())

    def test_stop_is_available_during_capture_and_cancels_loop(self):
        self.tab.start_button.click()
        worker = self.pool.start.call_args.args[0]

        self.assertTrue(self.tab.stop_button.isEnabled())
        self.tab.stop_button.click()
        worker.signals.finished.emit(preview.PreviewFrame(png_bytes(), 24, 18))

        self.assertFalse(self.tab._previewing)
        self.assertFalse(self.tab.timer.isActive())
        self.assertIsNone(self.tab._latest_png)

    def test_unchanged_frame_is_not_redrawn(self):
        frame = png_bytes()
        self.tab._latest_png = frame
        self.tab.start_button.click()
        worker = self.pool.start.call_args.args[0]
        with mock.patch.object(self.tab.preview, "setPixmap") as set_pixmap:
            worker.signals.finished.emit(preview.PreviewFrame(frame, 24, 18))
        set_pixmap.assert_not_called()

    def test_save_as_uses_latest_frame_without_recapturing(self):
        self.tab._latest_png = png_bytes()
        self.tab._refresh_controls()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "saved"
            with (
                mock.patch.object(
                    QtWidgets.QFileDialog,
                    "getSaveFileName",
                    return_value=(str(target), "PNG 图片 (*.png)"),
                ),
                mock.patch("_tab_screen_preview.show_info"),
            ):
                self.tab._save_as()
            self.assertTrue(target.with_suffix(".png").exists())
            self.pool.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
