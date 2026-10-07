"""Offline failure-path checks for the seven 2026-10-07 audit findings."""

import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets
import rmtool
import _diagnostics as diagnostics
import _firmware as firmware
import _koreader as koreader
import _tab_documents as documents
import _tab_koreader as koreader_ui
from _ssh import SSHClientWrapper
from _tab_firmware import FirmwareTab
from tests.test_audit_ui_safety import PendingWorker
from tests import test_firmware as firmware_tests
from tests.test_firmware import state_text
from tests.test_koreader import FakeSSH, FakeSFTP

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
DOC_ID = "11111111-1111-1111-1111-111111111111"


class FirmwareRegressionTests(unittest.TestCase):
    def setUp(self):
        fixture = firmware_tests.TransactionTests()
        fixture.setUp()
        self.ssh, self.directory = fixture.ssh, fixture.directory
        self.ssh.files["/proc/sys/kernel/random/boot_id"] = b"new"
        self.ssh.probe = state_text(root="/dev/mmcblk0p3", root_part="b", boot="2",
                                   version="3.28.0.172")

    def test_completed_record_survives_later_upgrade_but_current_busy_gate_remains(self):
        self.assertEqual(firmware.inspect_device(self.ssh)[1][0], "completed")
        record = self.ssh.files[self.directory + "/completed.json"]
        self.ssh.files["/proc/sys/kernel/random/boot_id"] = b"later"
        self.ssh.probe = state_text(version="3.29.0.100")  # Synthetic future version.
        self.assertEqual(firmware.inspect_device(self.ssh)[1][0], "completed")
        self.assertEqual(self.ssh.firmware_guard_reason, "")
        self.assertEqual(self.ssh.files[self.directory + "/completed.json"], record)
        for busy in ({"writer": "busy"}, {"holders": "busy"}, {"swu_status": "1"},
                     {"boot": "2"}, {"shared_lock": "busy"}, {"roota_errcnt": "1"},
                     {"swu_recovery": "1"}):
            with self.subTest(busy=busy):
                self.ssh.probe = state_text(**busy)
                firmware.inspect_device(self.ssh)
                self.assertTrue(self.ssh.firmware_guard_reason)

    def test_missing_result_is_persistently_reconciled(self):
        del self.ssh.files[self.directory + "/result"]
        self.assertEqual(firmware.query_transaction(self.ssh)[0], "completed")
        self.ssh.probe = state_text()
        self.assertEqual(firmware.query_transaction(self.ssh)[0], "completed")

    def test_failed_persistence_never_reports_completed(self):
        with mock.patch.object(firmware, "_write_remote", side_effect=OSError("disk full")):
            self.assertEqual(firmware.query_transaction(self.ssh)[0], "unknown")
        self.assertNotIn(self.directory + "/completed.json", self.ssh.files)

    def test_invalid_or_unsafe_completion_is_not_trusted(self):
        for value in (None, {}, {"completed_boot_id": "new", "job": "other"}):
            self.ssh.files[self.directory + "/completed.json"] = json.dumps(value).encode()
            self.assertEqual(firmware.query_transaction(self.ssh)[0], "unknown")
        self.ssh.files.pop(self.directory + "/completed.json")
        self.assertEqual(firmware.query_transaction(self.ssh)[0], "completed")
        original = self.ssh.exec_checked

        def execute(command):
            if command.startswith("test ! -L ") and "completed.json" in command:
                raise RuntimeError("unsafe marker")
            return original(command)

        with mock.patch.object(self.ssh, "exec_checked", side_effect=execute):
            self.assertEqual(firmware.query_transaction(self.ssh)[0], "unknown")

    def test_failed_result_and_same_boot_do_not_create_completion(self):
        self.ssh.files[self.directory + "/result"] = b"failed"
        self.assertEqual(firmware.query_transaction(self.ssh)[0], "failed")
        self.ssh.files[self.directory + "/result"] = b"success"
        self.ssh.files["/proc/sys/kernel/random/boot_id"] = b"old"
        self.assertEqual(firmware.query_transaction(self.ssh)[0], "success")
        self.assertNotIn(self.directory + "/completed.json", self.ssh.files)

    def test_status_refresh_bypasses_only_fixed_probe_and_pins_connection(self):
        ssh = firmware.FirmwareSSHClientWrapper()
        token = object()
        ssh.firmware_guard_reason = "pending firmware"
        page = FirmwareTab(ssh)
        self.addCleanup(page.deleteLater)

        def execute(command):
            ssh._firmware_gate()
            self.assertIn("cat /sys/devices/soc0/machine", command)
            return "reMarkable Chiappa"

        with mock.patch.object(ssh, "ensure_client", return_value=token), \
                mock.patch.object(ssh, "exec_checked", side_effect=execute), \
                mock.patch.object(page, "_run") as run, \
                mock.patch.object(firmware, "inspect_device", return_value=(
                    firmware.parse_state(state_text()), ("running", "running"))) as inspect, \
                mock.patch.object(firmware, "inspect_slot_metadata", return_value={}):
            page.refresh()
            operation = run.call_args.args[0]
            result = operation()
            inspect.assert_called_once()
            self.assertEqual(result[1][0], "running")
            with mock.patch.object(page, "_schedule_transaction_poll") as poll:
                page._device_loaded(result)
                poll.assert_called_once()
            with mock.patch.object(ssh, "ensure_client", return_value=object()), \
                    self.assertRaises(RuntimeError):
                operation()
        with self.assertRaisesRegex(RuntimeError, "pending firmware"):
            ssh._firmware_gate()


class DocumentRegressionTests(unittest.TestCase):
    def test_invalid_identifiers_are_neither_listed_nor_deleted(self):
        invalid = ["", "*", "../x", "x;reboot", "$(touch x)", "-rf"]
        sftp = mock.Mock()
        sftp.listdir_attr.return_value = [SimpleNamespace(filename=value + ".metadata", st_mtime=0)
                                        for value in invalid + [DOC_ID]]
        sftp.open.side_effect = lambda *_: io.StringIO("{}")
        self.assertEqual([item.identifier for item in rmtool.load_document_items(sftp)], [DOC_ID])
        tab = SimpleNamespace(ssh_client=mock.Mock(), _connection_generation=0)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                documents.DocumentsTab._perform_delete_documents(
                    tab, [SimpleNamespace(identifier=DOC_ID), SimpleNamespace(identifier=value)], 0, object())
        tab.ssh_client.exec_checked.assert_not_called()
        tab.ssh_client.operation_session.assert_not_called()

    def test_delete_enumerates_and_quotes_only_exact_document_children(self):
        ssh = SSHClientWrapper()
        token = object()
        names = [DOC_ID, DOC_ID + ".pdf", DOC_ID + ".weird *;name", DOC_ID + "-other.pdf",
                 "another.metadata", DOC_ID + ".dir/../../evil"]
        tab = SimpleNamespace(ssh_client=ssh, _connection_generation=0)
        with mock.patch.object(ssh, "ensure_client", return_value=token), \
                mock.patch.object(ssh, "sftp_session") as session, \
                mock.patch.object(ssh, "exec_checked") as execute:
            session.return_value.__enter__.return_value.listdir.return_value = names
            documents.DocumentsTab._perform_delete_documents(tab, [SimpleNamespace(identifier=DOC_ID)], 0, token)
        expected = [["rm", "-rf", "--", rmtool.DOCUMENT_ROOT + "/" + name] for name in names[:3]]
        self.assertEqual([shlex.split(call.args[0]) for call in execute.call_args_list[:-1]], expected)
        self.assertEqual(execute.call_args.args[0], "systemctl restart xochitl")

    def test_batch_preparation_failure_removes_earlier_private_copies(self):
        service = documents._DocumentTransferService(None, "/unused", lambda _: 1)
        with tempfile.TemporaryDirectory() as folder:
            prepared = Path(folder) / "prepared"
            prepared.mkdir()
            (prepared / "private.pdf").write_bytes(b"private contents")
            package = documents._PreparedDocumentUpload(DOC_ID, str(prepared), [], [])
            with mock.patch.object(service, "_prepare_upload", side_effect=[package, OSError("missing")]), \
                    self.assertRaises(OSError):
                service.transfer_batch(["first.pdf", "second.pdf"])
            self.assertFalse(prepared.exists())


class KOReaderRegressionTests(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeSSH()
        self.ssh.install_official()
        self.remote = "/home/root/books/example.epub"
        self.ssh.add_file(self.remote, b"original complete book")
        self.tab = koreader_ui.KOReaderTab(self.ssh)
        self.tab.thread_pool = mock.Mock()
        self.tab.set_connection_state(True)
        self.tab._on_listing_loaded((koreader.OFFICIAL_INSTALL_DIR, "/home/root/books",
                                     "/home/root/books", [koreader.KOReaderEntry(
                                         "example.epub", self.remote, 22, None, False)]))
        self.tab.table.selectRow(0)
        self.addCleanup(self.tab.deleteLater)
        patch = mock.patch.object(rmtool, "Worker", PendingWorker)
        patch.start()
        self.addCleanup(patch.stop)

    def reconnect(self):
        self.ssh._client = object()
        self.tab.set_connection_state(False)
        self.tab.set_connection_state(True)

    def test_delete_created_on_a_rejects_b_and_stale_results(self):
        with mock.patch.object(koreader_ui, "ask_confirmation", return_value=True), \
                mock.patch.object(self.tab, "_show_progress_dialog"):
            self.tab._delete_entries()
        worker = self.tab.thread_pool.start.call_args.args[0]
        self.reconnect()
        with self.assertRaises(RuntimeError):
            worker.fn(**worker.kwargs)
        self.assertEqual(self.ssh.files[self.remote], b"original complete book")
        with mock.patch.object(self.tab, "_reload_current") as reload, \
                mock.patch.object(self.tab, "_on_error") as error:
            worker.signals.finished.emit(None)
            worker.signals.error.emit(RuntimeError("old task"))
            reload.assert_not_called()
            error.assert_not_called()

    def test_all_mutation_dialogs_capture_device_before_confirmation(self):
        for entry, dialog, answer in (
            ("upload_books", "QFileDialog.getOpenFileNames", (["example.epub"], "")),
            ("_create_folder", "QInputDialog.getText", ("new-folder", True)),
            ("_download_books", "QFileDialog.getExistingDirectory", "unused-output"),
        ):
            with self.subTest(entry=entry):
                self.tab._install_dir = koreader.OFFICIAL_INSTALL_DIR
                self.tab._library_root = self.tab._current_dir = "/home/root/books"
                if entry == "_download_books":
                    self.tab._selected_entries = lambda: [SimpleNamespace(
                        path=self.remote, name="example.epub", is_dir=False)]
                def choose(*_args, **_kwargs):
                    self.reconnect()
                    return answer
                with mock.patch("_tab_koreader.QtWidgets." + dialog, side_effect=choose), \
                        mock.patch.object(koreader_ui, "ask_confirmation", return_value=True), \
                        mock.patch.object(self.tab, "_show_progress_dialog"):
                    getattr(self.tab, entry)()
                worker = self.tab.thread_pool.start.call_args.args[0]
                with self.assertRaisesRegex(RuntimeError, "设备连接已改变"):
                    worker.fn(**worker.kwargs)
        self.assertEqual(self.ssh.files[self.remote], b"original complete book")

    def test_worker_rechecks_after_lock_and_serializes_whole_operation(self):
        operation = mock.Mock()
        worker = self.tab._session_worker(self.ssh._client, operation)
        errors = []
        started = threading.Event()
        def run():
            started.set()
            try:
                worker.fn()
            except RuntimeError as exc:
                errors.append(exc)
        with self.ssh._transport_lock:
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(started.wait(2))
            self.ssh._client = object()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        operation.assert_not_called()

        def locked_operation():
            self.assertTrue(self.ssh._transport_lock._is_owned())
        self.tab._session_worker(self.ssh._client, locked_operation).fn()

    def test_old_listing_does_not_populate_new_device(self):
        self.tab.refresh()
        worker = self.tab.thread_pool.start.call_args.args[0]
        self.reconnect()
        with mock.patch.object(self.tab, "_on_listing_loaded") as loaded:
            worker.signals.finished.emit((None, "", "", []))
            loaded.assert_not_called()

    def test_upload_failures_preserve_original_and_clean_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "example.epub"
            local.write_bytes(b"replacement complete book")
            for failure in ("partial", "short", "rename"):
                def put(sftp, source, target, callback=None):
                    sftp.ssh.files[target] = Path(source).read_bytes()[:3]
                    if failure == "partial":
                        raise OSError("connection lost")
                patch = (mock.patch.object(FakeSFTP, "posix_rename", side_effect=OSError("rename refused"))
                         if failure == "rename" else mock.patch.object(FakeSFTP, "put", put))
                with self.subTest(failure=failure), patch, self.assertRaises((OSError, RuntimeError)):
                    koreader.upload_file(self.ssh, str(local), "/home/root/books",
                                         "/home/root/books", overwrite=True)
                self.assertEqual(self.ssh.files[self.remote], b"original complete book")
                self.assertFalse(any(".rmtool-upload-" in path for path in self.ssh.files))

    def test_non_overwrite_race_does_not_clobber_newly_created_book(self):
        del self.ssh.files[self.remote]
        original_put = FakeSFTP.put
        def put(sftp, *args, **kwargs):
            original_put(sftp, *args, **kwargs)
            self.ssh.files[self.remote] = b"created concurrently"
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "example.epub"
            local.write_bytes(b"upload")
            with mock.patch.object(FakeSFTP, "put", put), self.assertRaises(FileExistsError):
                koreader.upload_file(self.ssh, str(local), "/home/root/books", "/home/root/books")
        self.assertEqual(self.ssh.files[self.remote], b"created concurrently")
        self.assertFalse(any(".rmtool-upload-" in path for path in self.ssh.files))


class DownloadRegressionTests(unittest.TestCase):
    def test_failed_and_successful_downloads_replace_only_complete_files(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "example.epub"
            target.write_bytes(b"original")
            for failure in ("partial", "short", "replace", None):
                def get(remote, temporary, callback=None):
                    Path(temporary).write_bytes(b"new" if failure in ("partial", "short") else b"new complete")
                    if failure == "partial":
                        raise OSError("disconnected")
                sftp = SimpleNamespace(stat=lambda _: SimpleNamespace(st_size=12), get=get)
                if failure:
                    with self.subTest(failure=failure), self.assertRaises((RuntimeError, OSError)):
                        if failure == "replace":
                            with mock.patch("_ssh.os.replace", side_effect=OSError("in use")):
                                SSHClientWrapper._download_atomic(sftp, "/book", str(target))
                        else:
                            SSHClientWrapper._download_atomic(sftp, "/book", str(target))
                    self.assertEqual(target.read_bytes(), b"original")
                else:
                    SSHClientWrapper._download_atomic(sftp, "/book", str(target))
                    self.assertEqual(target.read_bytes(), b"new complete")
                self.assertEqual(list(Path(folder).iterdir()), [target])


class DiagnosticRegressionTests(unittest.TestCase):
    def test_real_posix_pipeline_preserves_status_and_byte_limit(self):
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        shell = str(git_bash) if git_bash.is_file() else shutil.which("sh")
        if not shell:
            self.skipTest("POSIX shell unavailable")

        class LocalShell:
            def exec_command(self, command, *, decode_errors="strict"):
                result = subprocess.run([shell, "-c", command], capture_output=True)
                return (result.stdout.decode("utf-8", decode_errors),
                        result.stderr.decode("utf-8", decode_errors), result.returncode)

        cases = [("printf evidence; exit 7", "evidence", "7", False),
                 ("printf ''; exit 1", "", "1", False),
                 ("printf '%065536d' 0", None, "", False),
                 ("printf '%065537d' 0", None, "", True),
                 ("printf '%070000d' 0; exit 13", None, "13", True),
                 ("i=0; while [ $i -lt 23000 ]; do printf '中'; i=$((i+1)); done", None, "", True)]
        for command, expected, error, truncated in cases:
            with self.subTest(command=command):
                item = diagnostics._collect_device_item(LocalShell(), diagnostics.DiagItem("test", "test", command))
                self.assertEqual(item.truncated, truncated)
                self.assertLessEqual(len(item.text.encode()), diagnostics.ITEM_CAP_BYTES)
                self.assertEqual(bool(item.error), bool(error))
                self.assertIn(error, item.error)
                if expected is not None:
                    self.assertEqual(item.text, expected)

    def test_missing_status_is_error_not_success(self):
        ssh = mock.Mock()
        ssh.exec_command.return_value = ("partial output", "", 0)
        result = diagnostics._collect_device_item(ssh, diagnostics.DEVICE_ITEMS[0])
        self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
