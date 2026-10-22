import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
from PyQt5 import QtWidgets

import _screen_preview as preview
from rmtool import ScreenPreviewTab
from _tab_screen_preview import LIVE_PREVIEW_COOLDOWN_MS


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
    def test_status_recognizes_all_supported_devices(self):
        cases = {
            "reMarkable Ferrari": "Paper Pro",
            "reMarkable Chiappa": "Paper Pro Move",
            "reMarkable Tatsu": "Paper Pure",
            "reMarkable 1.0": "reMarkable 1",
            "reMarkable 2.0": "reMarkable 2",
        }
        for machine, label in cases.items():
            with self.subTest(machine=machine):
                ssh = FakeSSH(commands=[(machine + "\n", "", 0), ("", "", 0)])
                status = preview.get_status(ssh)
                self.assertTrue(status.supported)
                self.assertEqual(status.device_name, label)

    def test_status_rejects_unknown_devices_and_unreadable_buffers(self):
        unknown = FakeSSH(commands=[("reMarkable Unknown\n", "", 0)])
        unavailable = FakeSSH(
            commands=[("reMarkable Ferrari\n", "", 0), ("", "", 1)]
        )

        self.assertFalse(preview.get_status(unknown).supported)
        self.assertFalse(preview.get_status(unavailable).supported)

    def test_capture_encodes_visible_screen_and_cleans_remote_buffer(self):
        profile = preview.PROFILES["chiappa"]
        raw = bytes([255, 255, 255, 255]) * (
            profile.buffer_width * profile.buffer_height
        )
        remote = "/tmp/rmtool-screen-preview-capture.raw"
        ssh = FakeSSH(files={remote: raw})
        with (
            mock.patch.object(
                preview,
                "get_status",
                return_value=preview.PreviewStatus(
                    True, "reMarkable Chiappa", "Paper Pro Move"
                ),
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
                return_value=preview.PreviewStatus(
                    True, "reMarkable Chiappa", "Paper Pro Move"
                ),
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

    def test_legacy_capture_uses_framebuffer_mapping_without_signals(self):
        command = preview._legacy_capture_command(
            preview.PROFILES["rm2"], "/tmp/frame.raw"
        )

        self.assertIn("/dev/fb0", command)
        self.assertIn(str(preview.LEGACY_FRAME_OFFSET), command)
        self.assertNotIn("USR2", command)
        self.assertNotIn("iflag", command)
        self.assertNotIn("skip_bytes", command)
        self.assertNotIn("2>/dev/null", command)

    def test_legacy_capture_trims_aligned_read_before_encoding(self):
        profile = preview.DeviceProfile("rm2", "reMarkable 2", "legacy", 2, 1, 2, 1)
        pixels = bytes([10, 1, 2, 3, 200, 4, 5, 6])
        for prefix in (0, 8, preview.LEGACY_READ_BLOCK - 1):
            with self.subTest(prefix=prefix):
                size = ((prefix + len(pixels) + 4095) // 4096) * 4096
                raw = b"\xff" * prefix + pixels + b"\xff" * (size - prefix - len(pixels))
                frame = self.capture_legacy(raw, f"rmtool-frame-offset={prefix}\n", profile)
                with Image.open(io.BytesIO(frame.png)) as image:
                    self.assertEqual([image.getpixel((x, 0)) for x in range(2)], [10, 200])

    def capture_legacy(self, raw, stdout, profile=None, stderr="", code=0):
        remote = "/tmp/rmtool-screen-preview-capture.raw"
        ssh = FakeSSH(commands=[(stdout, stderr, code)], files={remote: raw})
        profile = profile or preview.PROFILES["rm2"]
        with (
            mock.patch.object(preview, "get_status", return_value=preview.PreviewStatus(
                True, "reMarkable 2.0", "reMarkable 2")),
            mock.patch.object(preview, "_profile_for_machine", return_value=profile),
            mock.patch.object(preview.uuid, "uuid4", return_value=SimpleNamespace(hex="capture")),
        ):
            try:
                return preview.capture(ssh)
            finally:
                ssh.exec_checked.assert_called_once_with(f"rm -f {remote}", timeout=10)

    def test_legacy_invalid_offset_or_short_read_is_rejected_and_cleaned(self):
        for stdout in ("", "rmtool-frame-offset=-1", "rmtool-frame-offset=4096",
                       "rmtool-frame-offset=8\nunknown"):
            with self.subTest(stdout=stdout), self.assertRaisesRegex(RuntimeError, "偏移无效"):
                self.capture_legacy(b"", stdout)
        with self.assertRaisesRegex(RuntimeError, "大小不正确"):
            self.capture_legacy(b"short", "rmtool-frame-offset=8\n")

    def test_legacy_dd_error_is_preserved_and_buffer_cleaned(self):
        with self.assertRaisesRegex(RuntimeError, "dd: Permission denied"):
            self.capture_legacy(b"", "", stderr="dd: Permission denied", code=1)

    def test_legacy_buffer_is_encoded_as_grayscale(self):
        profile = preview.DeviceProfile("test", "Test", "legacy", 2, 1, 2, 1)
        data = preview._encode_png(profile, bytes([10, 1, 2, 3, 200, 4, 5, 6]))

        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.mode, "L")
            self.assertEqual([image.getpixel((0, 0)), image.getpixel((1, 0))], [10, 200])

    def test_atomic_save_preserves_existing_file_on_replace_failure(self):
        data = png_bytes()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "screen.png"
            target.write_bytes(b"old")
            with mock.patch.object(os, "replace", side_effect=OSError("busy")):
                with self.assertRaises(OSError):
                    preview.save_png_atomic(data, target)
            self.assertEqual(target.read_bytes(), b"old")


SH = shutil.which("sh")
if os.name == "nt":
    git_sh = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if git_sh.is_file():
        SH = str(git_sh)


@unittest.skipUnless(SH, "A POSIX shell or Git Bash is required")
class LegacyCaptureShellTests(unittest.TestCase):
    def execute(self, failure=""):
        profile = preview.DeviceProfile("rm2", "reMarkable 2", "legacy", 2, 1, 2, 1)
        command = preview._legacy_capture_command(profile, "./frame.raw")
        command = command.replace("/proc/", "./proc/")
        self.assertNotIn("/proc/", command.replace("./proc/", ""))
        self.assertNotIn("iflag", command)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = root / "proc/123"
            process.mkdir(parents=True)
            (process / "maps").write_text(
                "1000-2000 rw-s 00000000 00:01 1 /dev/fb0\n"
                "2000-2000000 rw-p 00000000 00:00 0\n"
            )
            (process / "stat").write_text(" ".join(["0"] * 21 + ["123"]))
            pointer = 0x2000 + preview.LEGACY_FRAME_OFFSET
            prefix = pointer % 4096
            padded = ((prefix + 8 + 4095) // 4096) * 4096
            with (process / "mem").open("wb") as memory:
                memory.seek(pointer)
                memory.write(bytes([10, 1, 2, 3, 200, 4, 5, 6]))
                if failure != "short":
                    memory.truncate(pointer - prefix + padded)
            prelude = r'''
pidof() { printf '123\n'; }
dd() {
    for arg do
        case "$arg" in if=*|of=*|bs=*|skip=*|count=*) ;; *) exit 98 ;; esac
    done
    if [ "__FAILURE__" = denied ]; then echo 'dd: Permission denied' >&2; return 1; fi
    command dd "$@" || return $?
    if [ "__FAILURE__" = restart ]; then printf 'changed\n' > ./proc/123/stat; fi
}
'''.replace("__FAILURE__", failure)
            result = subprocess.run([SH], input=prelude + command, cwd=root,
                                    capture_output=True, encoding="utf-8", timeout=15)
            raw = (root / "frame.raw").read_bytes() if (root / "frame.raw").exists() else b""
        return result, raw, prefix, padded

    def test_basic_dd_reads_correct_offset_with_bounded_padding(self):
        result, raw, prefix, padded = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"rmtool-frame-offset={prefix}\n")
        self.assertEqual(len(raw), padded)
        self.assertEqual(raw[prefix:prefix + 8], bytes([10, 1, 2, 3, 200, 4, 5, 6]))

    def test_short_read_restart_and_read_error_are_not_reported_as_frames(self):
        for failure, code in (("short", 52), ("restart", 54), ("denied", 1)):
            with self.subTest(failure=failure):
                result, _raw, _prefix, _padded = self.execute(failure)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertNotIn("rmtool-frame-offset=", result.stdout)
                if failure == "denied":
                    self.assertIn("dd: Permission denied", result.stderr)


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
            preview.PreviewStatus(True, "reMarkable Chiappa", "Paper Pro Move")
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
