"""Offline transport-window and wallpaper transaction regressions."""

import os
import shlex
import socket
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import paramiko

import rmtool
import _ssh
import _tab_wallpaper


class TransportSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = paramiko.RSAKey.generate(1024)

    def connection(self, output=b"", error=b"", *, stalled=False, handshake=False,
                   status=0, delay=0):
        left, right = socket.socketpair()
        server = paramiko.Transport(left)
        transport = paramiko.Transport(right)
        server.add_server_key(self.key)
        entered = threading.Event()
        release = threading.Event()
        writers = []

        class Server(paramiko.ServerInterface):
            def check_auth_none(self, username):
                return paramiko.AUTH_SUCCESSFUL

            def check_channel_request(self, kind, channel_id):
                return paramiko.OPEN_SUCCEEDED

            def check_channel_exec_request(self, channel, command):
                entered.set()
                if handshake:
                    release.wait(3)
                    return False

                def write():
                    try:
                        if stalled:
                            release.wait(3)
                            return
                        # Let the server send SSH_MSG_CHANNEL_SUCCESS first.
                        release.wait(0.02)
                        if delay:
                            release.wait(delay)
                        if output:
                            channel.sendall(output)
                        if error:
                            channel.sendall_stderr(error)
                        if status is not None:
                            channel.send_exit_status(status)
                        channel.close()
                    except (EOFError, OSError):
                        pass

                writer = threading.Thread(target=write)
                writers.append(writer)
                writer.start()
                return True

        ready = threading.Event()
        server.start_server(event=ready, server=Server())
        transport.start_client(timeout=3)
        transport.auth_none("test")
        client = paramiko.SSHClient()
        client._transport = transport
        wrapper = _ssh.SSHClientWrapper()
        wrapper._client = client

        def cleanup():
            release.set()
            wrapper.close()
            server.close()
            transport.close()
            for writer in writers:
                writer.join(3)
                self.assertFalse(writer.is_alive())

        self.addCleanup(cleanup)
        return wrapper, entered

    def test_real_paramiko_window_stdout_and_stderr_flood(self):
        for output, error in (
            (b"a" * (3 * 1024 * 1024), b""),
            (b"", b"b" * (3 * 1024 * 1024)),
            (b"a" * (3 * 1024 * 1024), b"b" * (3 * 1024 * 1024)),
        ):
            with self.subTest(stdout=len(output), stderr=len(error)):
                wrapper, _ = self.connection(output, error, status=7)
                self.assertEqual(wrapper.exec_command("flood", timeout=5),
                                 (output.decode(), error.decode(), 7))

    def test_timeout_bounds_silent_command_and_exec_handshake(self):
        for handshake in (False, True):
            with self.subTest(handshake=handshake):
                wrapper, _ = self.connection(stalled=True, handshake=handshake)
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    wrapper.exec_command("stall", timeout=0.15)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertFalse(wrapper.is_connected())
                self.assertIsNone(wrapper._client)

    def test_connection_cancellation_unblocks_and_releases_lock(self):
        wrapper, entered = self.connection(stalled=True)
        errors = []

        def execute():
            try:
                wrapper.exec_command("stall")
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute)
        worker.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        wrapper.close()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertTrue(wrapper._transport_lock.acquire(timeout=0.1))
        wrapper._transport_lock.release()

    def test_silent_longer_operation_and_checked_timeout_override(self):
        wrapper, _ = self.connection(b"ok", delay=0.25)
        self.assertEqual(wrapper.exec_checked("silent", timeout=2), "ok")

    def test_missing_status_is_not_success(self):
        wrapper, _ = self.connection(b"partial", status=None)
        with self.assertRaisesRegex(RuntimeError, "退出状态"):
            wrapper.exec_command("closed", timeout=2)

    def test_utf8_decoding_remains_strict(self):
        wrapper, _ = self.connection(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            wrapper.exec_command("invalid", timeout=2)

    def test_read_and_close_failures_finish_without_masking_read_error(self):
        for failing_stream in (0, 1):
            with self.subTest(stream=failing_stream):
                closed = threading.Event()
                failure = OSError("read failed")
                channel = SimpleNamespace(close=closed.set, recv_exit_status=lambda: 0)

                def read(index):
                    if index == failing_stream:
                        raise failure
                    self.assertTrue(closed.wait(1))
                    return b""

                streams = [mock.Mock(channel=channel) for _ in range(2)]
                for index, stream in enumerate(streams):
                    stream.read.side_effect = lambda index=index: read(index)
                    stream.close.side_effect = OSError("close failed")
                client = mock.Mock()
                client.exec_command.return_value = (None, *streams)
                wrapper = _ssh.SSHClientWrapper()
                wrapper._client = client
                with self.assertLogs(level="ERROR"):
                    with self.assertRaises(OSError) as caught:
                        wrapper.exec_command("bad read", timeout=2)
                self.assertIs(caught.exception, failure)
                self.assertTrue(closed.is_set())

    def test_rejects_unbounded_timeouts(self):
        wrapper = _ssh.SSHClientWrapper()
        for value in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                wrapper.exec_command("unused", timeout=value)

    def test_channel_close_failure_does_not_mask_completed_command(self):
        channel = mock.Mock()
        channel.recv_exit_status.return_value = 0
        channel.close.side_effect = OSError("close failed")
        stdout = mock.Mock(channel=channel)
        stdout.read.return_value = b"ok"
        stderr = mock.Mock(channel=channel)
        stderr.read.return_value = b""
        client = mock.Mock()
        client.exec_command.return_value = (None, stdout, stderr)
        wrapper = _ssh.SSHClientWrapper()
        wrapper._client = client
        with self.assertLogs(level="ERROR"):
            self.assertEqual(wrapper.exec_command("ok", timeout=2), ("ok", "", 0))

    def test_operation_session_blocks_reconnect_until_cancelled_cleanup_exits(self):
        wrapper = _ssh.SSHClientWrapper()
        wrapper._client = mock.Mock()
        contender_started = threading.Event()
        reconnected = threading.Event()
        replacement = mock.Mock()

        def reconnect():
            contender_started.set()
            with wrapper._transport_lock:
                with wrapper._state_lock:
                    wrapper._client = replacement
                reconnected.set()

        with wrapper.operation_session():
            contender = threading.Thread(target=reconnect)
            contender.start()
            self.assertTrue(contender_started.wait(1))
            wrapper.close()
            self.assertFalse(reconnected.wait(0.05))
            with self.assertRaises(RuntimeError):
                wrapper.ensure_client()
        contender.join(1)
        self.assertFalse(contender.is_alive())
        self.assertIs(wrapper._client, replacement)

    def test_cancel_does_not_close_replacement_session(self):
        wrapper, entered = self.connection(stalled=True)
        original = wrapper._client
        replacement = mock.Mock()
        errors = []

        def execute():
            try:
                wrapper.exec_command("stall", timeout=2)
            except RuntimeError as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute)
        worker.start()
        self.assertTrue(entered.wait(1))
        with wrapper._state_lock:
            wrapper._client = replacement
        original.close()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)
        replacement.close.assert_not_called()


class MemorySSH:
    def __init__(self, files, fail=None):
        self.files = dict(files)
        self.fail = fail
        self.commands = []
        self.transfers = []

    def fault(self, action, source, target):
        if self.fail:
            self.fail(self, action, source, target)

    @contextmanager
    def operation_session(self):
        yield

    def file_exists(self, path):
        return path in self.files

    def listdir_attr(self, path):
        prefix = path + "/"
        return [SimpleNamespace(filename=name[len(prefix):]) for name in self.files
                if name.startswith(prefix) and "/" not in name[len(prefix):]]

    @contextmanager
    def open_remote(self, path, mode="rb"):
        yield BytesIO(self.files[path])

    def transfer_file(self, local, remote):
        self.transfers.append(remote)
        self.files[remote] = Path(local).read_bytes()
        self.fault("put", local, remote)

    def exec_checked(self, command):
        self.commands.append(command)
        parts = shlex.split(command)
        action = parts[0]
        if action in ("cp", "mv"):
            source, target = parts[-2:]
            self.fault(action, source, target)
            self.files[target] = self.files[source]
            if action == "mv":
                del self.files[source]
            self.fault(action + "_after", source, target)
        elif action == "rm":
            prefix = parts[-1] + "/"
            for name in list(self.files):
                if name.startswith(prefix):
                    del self.files[name]
        return ""


class WallpaperSafetyTests(unittest.TestCase):
    primary = "/usr/share/remarkable/suspended.png"
    overlay = _tab_wallpaper._CAROUSEL_DIR + "/sleep_Illustration_01.png"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name) / "new.png"
        _tab_wallpaper.Image.new("RGB", (8, 8), "red").save(self.local)
        self.original = BytesIO()
        _tab_wallpaper.Image.new("RGB", (8, 8), "blue").save(self.original, format="PNG")
        self.original = self.original.getvalue()
        self.backup = self.primary + ".backup"
        self.files = {self.primary: self.original, self.overlay: self.original,
                      self.backup: b"older-good-backup"}

    def upload(self, client, target=None):
        tab = SimpleNamespace(ssh_client=client)
        for name in ("_replace_wallpaper_files_locked", "_blank_carousel_overlays_locked",
                     "_migrate_legacy_carousel_backups"):
            setattr(tab, name, getattr(_tab_wallpaper.WallpaperTab, name).__get__(tab))
        _tab_wallpaper.WallpaperTab._do_upload_wallpaper(
            tab, str(self.local), target or self.primary
        )

    def assert_preserved(self, client):
        self.assertEqual(client.files[self.primary], self.original)
        self.assertEqual(client.files[self.overlay], self.original)
        self.assertEqual(client.files[self.backup], b"older-good-backup")
        self.assertFalse(any(".rmtool-wallpaper-" in name for name in client.files))
        self.assertEqual(client.commands[-1], "mount -o remount,ro /")

    def test_success_is_staged_verified_and_preserves_good_backups(self):
        client = MemorySSH(self.files)
        self.upload(client)
        self.assertEqual(client.files[self.primary], self.local.read_bytes())
        self.assertEqual(client.files[self.backup], b"older-good-backup")
        self.assertTrue(_tab_wallpaper._is_transparent_placeholder(client.files[self.overlay]))
        self.assertEqual(client.files[_tab_wallpaper._CAROUSEL_BACKUP_DIR +
                                     "/sleep_Illustration_01.png"], self.original)
        self.assertTrue(all(".rmtool-wallpaper-" in name for name in client.transfers))
        self.assertFalse(any(".rmtool-wallpaper-" in name for name in client.files))

    def test_upload_interruption_and_hash_mismatch_leave_originals(self):
        for interruption in (True, False):
            for index in (1, 2):
                count = 0

                def fail(client, action, source, target):
                    nonlocal count
                    if action == "put":
                        count += 1
                        if count == index:
                            client.files[target] = b"truncated"
                            if interruption:
                                raise OSError("connection lost")

                with self.subTest(interruption=interruption, upload=index):
                    client = MemorySSH(self.files, fail)
                    with self.assertRaises((OSError, RuntimeError)):
                        self.upload(client)
                    self.assert_preserved(client)

    def test_backup_copy_failure_never_publishes_partial_backup(self):
        def fail(client, action, source, target):
            if action == "cp_after" and target.endswith("/backup"):
                client.files[target] = b"partial"
                raise OSError("full disk")

        client = MemorySSH(self.files, fail)
        with self.assertRaises(OSError):
            self.upload(client)
        self.assert_preserved(client)
        self.assertNotIn(_tab_wallpaper._CAROUSEL_BACKUP_DIR +
                         "/sleep_Illustration_01.png", client.files)

    def test_commit_failure_or_lost_ack_rolls_back_entire_group(self):
        for point in ("mv", "mv_after"):
            failed = False

            def fail(client, action, source, target):
                nonlocal failed
                if not failed and action == point and target == self.overlay:
                    failed = True
                    raise OSError("rename failed")

            with self.subTest(point=point):
                client = MemorySSH(self.files, fail)
                with self.assertRaises(OSError):
                    self.upload(client)
                self.assert_preserved(client)

    def test_post_commit_corruption_rolls_back(self):
        def fail(client, action, source, target):
            if action == "mv_after" and source.endswith("/incoming") and target == self.primary:
                client.files[target] = b"corrupt"

        client = MemorySSH(self.files, fail)
        with self.assertRaisesRegex(RuntimeError, "替换后"):
            self.upload(client)
        self.assert_preserved(client)

    def test_rollback_failure_retains_recovery_original(self):
        def fail(client, action, source, target):
            if action == "mv" and target == self.overlay:
                raise OSError("device unavailable")

        client = MemorySSH(self.files, fail)
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(OSError):
                self.upload(client)
        originals = [data for name, data in client.files.items() if name.endswith("/original")]
        self.assertEqual(originals, [self.original])
        self.assertEqual(client.files[self.backup], b"older-good-backup")

    def test_direct_overlay_upload_keeps_backup_out_of_rotation(self):
        client = MemorySSH(self.files)
        self.upload(client, self.overlay)
        self.assertNotIn(self.overlay + ".backup", client.files)
        self.assertEqual(client.files[self.overlay], self.local.read_bytes())


if __name__ == "__main__":
    unittest.main()
