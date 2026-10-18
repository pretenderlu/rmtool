from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
import http.server
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import _native_document_import as native
import _native_import_control as control


class NativeImportTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.statuses = [201]
        self.ready_status = 200
        self.free_kb = 1024 * 1024
        self.native_addresses = "    inet 10.11.99.1/27 brd 10.11.99.31 scope global usb1\n"
        self.locked = False
        self.channels = []
        self.ssh = Mock()
        self.client = self.ssh.ensure_client.return_value
        self.transport = self.client.get_transport.return_value
        self.transport.open_channel.side_effect = self.open_channel
        self.ssh.exec_checked.side_effect = self.disk_space
        self.ssh.operation_session.side_effect = self.session
        self.importer = native.NativeDocumentImporter(self.ssh, "/home/root/library", self.client)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner.requests.append((self.command, self.path, self.headers, b""))
                self.send_response(owner.ready_status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((self.command, self.path, self.headers, body))
                status = owner.statuses.pop(0)
                if status is not None:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.close_channels)

    def close_channels(self):
        for channel in self.channels:
            channel.close()

    @contextmanager
    def session(self):
        self.assertFalse(self.locked)
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def open_channel(self, kind, target, origin, timeout):
        self.assertTrue(self.locked)
        self.assertEqual((kind, target, origin), ("direct-tcpip", ("10.11.99.1", 80), ("127.0.0.1", 0)))
        channel = socket.create_connection(self.server.server_address, timeout)
        self.channels.append(channel)
        return channel

    def disk_space(self, command):
        self.assertTrue(self.locked)
        if command == native._CONTROL_STATE:
            return "free"
        if command == native._NATIVE_ADDRESS_STATE:
            return self.native_addresses
        self.assertTrue(command.startswith("df -Pk /home/root/library | awk "), command)
        return str(self.free_kb)

    def file(self, name="book.pdf", data=b"%PDF-1.7\n" + b"x" * 200000 + b"\n%%EOF"):
        path = Path(self.folder.name) / name
        path.write_bytes(data)
        return str(path)

    def test_streams_unicode_filenames_and_bytes_through_selected_ssh(self):
        paths = [self.file("测试书籍.pdf"), self.file("another.epub", b"epub content")]
        self.statuses = [200, 201]
        progress, phases = [], []
        count = self.importer.transfer_batch(paths, lambda a, b: progress.append((a, b)), phases.append)
        self.assertEqual(count, 2)
        self.assertEqual([r[:2] for r in self.requests], [("GET", "/"), ("POST", "/upload"), ("POST", "/upload")])
        for path, (_method, _url, headers, body) in zip(paths, self.requests[1:]):
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + headers["Content-Type"] + "\r\n\r\n").encode() + body
            )
            part, = message.iter_parts()
            self.assertEqual(part.get_param("name", header="Content-Disposition"), "file")
            self.assertEqual(part.get_filename(), Path(path).name)
            self.assertEqual(part.get_payload(decode=True), Path(path).read_bytes())
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertEqual(headers["Host"], "10.11.99.1")
        total = sum(Path(p).stat().st_size for p in paths)
        self.assertEqual(progress[-1], (total, total))
        self.assertTrue(all(a < b for a, b in progress[:-1]))
        self.assertEqual(len(phases), 4)
        self.assertTrue(phases[-1].startswith("等待设备确认导入"))
        self.ssh.sftp_session.assert_not_called()
        self.transport.open_session.assert_not_called()
        self.assertTrue(all(sock.fileno() == -1 for sock in self.channels))

    def test_missing_native_service_never_sends_file_or_restarts(self):
        for status in (302, 403, 503):
            with self.subTest(status=status), patch.object(control, "loaded_helper", return_value=None):
                self.ready_status = status
                with self.assertRaisesRegex(RuntimeError, "自动传输组件"):
                    self.importer.transfer_batch([self.file()])
        self.assertTrue(all(r[0] == "GET" for r in self.requests))
        self.assertTrue(all(c.args[0].startswith("df -Pk ") or c.args[0] in (native._CONTROL_STATE, native._NATIVE_ADDRESS_STATE)
                            for c in self.ssh.exec_checked.call_args_list))
        self.ssh.sftp_session.assert_not_called()

    def test_native_timeout_is_not_retried_and_preserves_accepted_files(self):
        self.statuses = [201, None]
        paths = [self.file("one.pdf"), self.file("two.pdf"), self.file("three.pdf")]
        progress = []
        with self.assertRaisesRegex(RuntimeError, "已确认导入 1/3.*结果未确认.*后续文件未发送"):
            self.importer.transfer_batch(paths, lambda a, b: progress.append((a, b)))
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(a < b for a, b in progress))
        self.ssh.sftp_session.assert_not_called()

    def test_http_error_and_redirect_do_not_retry_or_report_success(self):
        for status in (302, 400, 413, 500):
            with self.subTest(status=status):
                self.requests.clear()
                self.statuses = [status]
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                    self.importer.transfer_batch([self.file()])
                self.assertEqual(len(self.requests), 2)

    def test_space_probe_fails_closed_before_post(self):
        for free in (0, "unknown"):
            with self.subTest(free=free):
                self.free_kb = free
                with self.assertRaises(RuntimeError):
                    self.importer.transfer_batch([self.file()])
        self.assertTrue(all(r[0] == "GET" for r in self.requests))

    def test_invalid_batch_closes_files_before_any_device_request(self):
        opened = []
        builtin_open = open

        def track(*args, **kwargs):
            handle = builtin_open(*args, **kwargs)
            opened.append(handle)
            return handle

        for bad in ("missing.epub", "invalid.txt"):
            with self.subTest(bad=bad), patch.object(native, "open", side_effect=track, create=True):
                with self.assertRaises((OSError, RuntimeError)):
                    self.importer.transfer_batch([self.file(), str(Path(self.folder.name) / bad)])
                self.assertTrue(all(f.closed for f in opened))
        self.assertEqual(self.requests, [])
        self.assertEqual(list(Path(self.folder.name).iterdir()), [Path(self.folder.name) / "book.pdf"])

    def test_empty_file_and_header_injection_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "为空"):
            self.importer.transfer_batch([self.file(data=b"")])
        with self.assertRaisesRegex(RuntimeError, "文件名"):
            self.importer.transfer_batch([str(Path(self.folder.name) / 'bad\r\nheader.pdf')])
        self.assertEqual(self.requests, [])

    def test_reconnected_device_is_rejected_before_readiness_probe(self):
        self.ssh.ensure_client.return_value = object()
        with self.assertRaisesRegex(RuntimeError, "连接已变化"):
            self.importer.transfer_batch([self.file()])
        self.transport.open_channel.assert_not_called()
        self.ssh.exec_checked.assert_not_called()

    def test_file_change_mid_send_is_ambiguous_and_never_accepted(self):
        path = self.file()
        changed = False

        def mutate(sent, _total):
            nonlocal changed
            if sent and not changed:
                changed = True
                with open(path, "ab") as f:
                    f.write(b"modified")

        with self.assertRaisesRegex(RuntimeError, r"结果未确认.*[\s\S]*本地文件已变化"):
            self.importer.transfer_batch([path], mutate)

    def test_more_than_phone_limit_streams_in_bounded_chunks(self):
        path = self.file(data=b"x")
        with open(path, "r+b") as f:
            f.truncate(200_000_001)
        sent_sizes = []
        with patch.object(native, "_NativeHTTPConnection") as factory:
            factory.return_value.getresponse.return_value.status = 200
            factory.return_value.send.side_effect = lambda chunk: sent_sizes.append(len(chunk))
            self.assertEqual(self.importer.transfer_batch([path]), 1)
        self.assertGreater(len(sent_sizes), 3000)
        self.assertLessEqual(max(sent_sizes), 64 * 1024)

    def test_firmware_guard_blocks_all_network_requests(self):
        self.ssh.operation_session.side_effect = RuntimeError("pending firmware update")
        with self.assertRaisesRegex(RuntimeError, "pending firmware"):
            self.importer.transfer_batch([self.file()])
        self.transport.open_channel.assert_not_called()

    def managed_control(self, response=b"ready\nrestored\n", helper="/data/rmtool/xovi-standalone/helpers/rmtool-native-import"):
        import io
        channel = self.transport.open_session.return_value
        channel.makefile.return_value = io.BytesIO(response)
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 0
        return channel, patch.object(control, "loaded_helper", return_value=helper)

    def test_disabled_service_is_prepared_and_restored_after_native_acceptance(self):
        channel, trusted = self.managed_control()
        readiness = [RuntimeError("off"), None]
        progress, phases = [], []
        with trusted, patch.object(self.importer, "_check_ready", side_effect=readiness):
            count = self.importer.transfer_batch([self.file()], lambda a, b: progress.append((a, b)), phases.append)
        self.assertEqual(count, 1)
        channel.exec_command.assert_called_once_with(
            "/data/rmtool/xovi-standalone/helpers/rmtool-native-import --desktop-native-lease"
        )
        self.assertEqual(channel.settimeout.call_args_list, [unittest.mock.call(30), unittest.mock.call(5)])
        self.assertEqual(channel.sendall.call_args_list[-1].args, (b"release\n",))
        self.assertTrue(channel.makefile.return_value.closed)
        channel.close.assert_called_once()
        self.assertEqual(progress[-1][0], progress[-1][1])
        self.assertTrue(phases[-1].startswith("正在恢复"))
        self.ssh.sftp_session.assert_not_called()

    def test_all_five_devices_on_stable_and_beta_use_native_control_and_stock_post(self):
        import _xovi_standalone as shared
        catalog = control._trusted_catalog()
        self.assertEqual({(p.platform, p.release_version) for p in catalog},
                         {(platform, release) for platform in ("rm1", "rm2", "tatsu", "ferrari", "chiappa")
                          for release in ("3.28.0.172", "3.29.0.153")})
        for package in catalog:
            with self.subTest(platform=package.platform, release=package.release_version):
                runtime, _feature = control._shared_specs(package)
                helper = f"{shared.preferred_layout(runtime).remote_base}/{control.HELPER_PAYLOAD_PATH}"
                channel, trusted = self.managed_control(helper=helper)
                channel.reset_mock()
                self.requests.clear()
                self.statuses = [201]
                with trusted, patch.object(self.importer, "_check_ready", side_effect=[RuntimeError("off"), None]):
                    self.assertEqual(self.importer.transfer_batch([self.file(f"{package.platform}.pdf")]), 1)
                channel.exec_command.assert_called_once_with(helper + " --desktop-native-lease")
                self.assertEqual([r[:2] for r in self.requests], [("POST", "/upload")])
                self.assertEqual(channel.sendall.call_args_list[-1].args, (b"release\n",))

    def test_verified_legacy_qr_control_is_still_reusable(self):
        helper = "/data/rmtool/xovi-standalone/helpers/rmtool-qr-upload"
        channel, trusted = self.managed_control(helper=helper)
        with trusted, patch.object(self.importer, "_check_ready", side_effect=[RuntimeError("off"), None]):
            self.assertEqual(self.importer.transfer_batch([self.file()]), 1)
        channel.exec_command.assert_called_once_with(helper + " --desktop-native-lease")

    def test_same_address_on_lan_is_not_used_without_device_local_assignment(self):
        self.native_addresses = "    inet 10.11.99.15/27 scope global wlan0\n"
        with patch.object(control, "loaded_helper", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "自动传输组件"):
                self.importer.transfer_batch([self.file()])
        self.assertEqual(self.requests, [])
        self.transport.open_channel.assert_not_called()

    def test_lost_native_address_never_sends_file_and_releases_control(self):
        self.native_addresses = "    inet 192.168.1.5/24 scope global wlan0\n"
        channel, trusted = self.managed_control()
        with trusted, patch.object(native.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "本机的原生导入地址"):
                self.importer.transfer_batch([self.file()])
        self.assertEqual(self.requests, [])
        self.assertEqual(channel.sendall.call_args_list[-1].args, (b"release\n",))

    def test_qr_session_markers_are_also_checked_before_reusing_native_server(self):
        for path in ("/run/rmtool-qr-upload-ui", "/home/root/.rmtool-qr-upload-web-state"):
            self.assertIn(f"[ -e {path} ]", native._CONTROL_STATE)
            self.assertIn(f"[ -L {path} ]", native._CONTROL_STATE)

    def test_failed_import_still_restores_original_setting(self):
        channel, trusted = self.managed_control()
        self.statuses = [500]
        with trusted, patch.object(self.importer, "_check_ready", side_effect=[RuntimeError("off"), None]):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                self.importer.transfer_batch([self.file()])
        self.assertEqual(channel.sendall.call_args_list[-1].args, (b"release\n",))
        channel.close.assert_called_once()

    def test_restore_failure_is_not_upload_success_and_retains_primary_error(self):
        for http_status in (201, 500):
            with self.subTest(status=http_status):
                channel, trusted = self.managed_control(b"ready\nrestore-failed\n")
                channel.reset_mock()
                self.statuses = [http_status]
                progress = []
                with trusted, patch.object(self.importer, "_check_ready", side_effect=[RuntimeError("off"), None]):
                    with self.assertRaisesRegex(RuntimeError, "原设置恢复未确认") as error:
                        self.importer.transfer_batch([self.file()], lambda a, b: progress.append((a, b)))
                if http_status == 500:
                    self.assertIn("HTTP 500", str(error.exception))
                self.assertTrue(all(a < b for a, b in progress))
                channel.close.assert_called_once()

    def test_unavailable_control_never_posts_and_closes_session(self):
        channel, trusted = self.managed_control(b"busy\n")
        with trusted, patch.object(self.importer, "_check_ready", side_effect=RuntimeError("off")):
            with self.assertRaisesRegex(RuntimeError, "停止扫码接收"):
                self.importer.transfer_batch([self.file()])
        channel.close.assert_called_once()
        self.assertEqual(self.requests, [])

    def test_activation_failure_after_lease_still_releases(self):
        channel, trusted = self.managed_control()
        with trusted, patch.object(self.importer, "_check_ready", side_effect=RuntimeError("not ready")), \
                patch.object(native.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                self.importer.transfer_batch([self.file()])
        self.assertEqual(channel.sendall.call_args_list[-1].args, (b"release\n",))
        channel.close.assert_called_once()

    def test_existing_automatic_lease_is_not_reused_as_a_manual_service(self):
        self.ssh.exec_checked.side_effect = lambda c: "busy" if c == native._CONTROL_STATE else str(self.free_kb)
        with self.assertRaisesRegex(RuntimeError, "已有传输会话"):
            self.importer.transfer_batch([self.file()])
        self.assertEqual(self.requests, [])
        self.transport.open_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
