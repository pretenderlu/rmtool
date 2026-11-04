"""Offline regression model for review M6, M38-M40 and firmware Low items."""

import hashlib
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

import _device_actions
import _firmware as f
import rmtool  # noqa: F401 - initializes the tab import cycle
import _tab_firmware as ui
from tests.test_firmware import FakeSSH, state_text
from tests.test_firmware_release import DeviceSSH, DIRECTORY, JOB


BASH = "C:/Program Files/Git/bin/bash.exe"


class StagingSSH(FakeSSH):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure
        self.created = False
        self.removed = False

    @contextmanager
    def sftp_session(self):
        def put(_source, target, callback=None):
            self.files[target] = b"partial"
            if self.failure == "upload":
                raise OSError("upload failed")
        yield mock.Mock(put=put)

    def exec_checked(self, command):
        if "mkdir -m 700 " + DIRECTORY in command:
            self.created = True
        if "rm -rf -- " + DIRECTORY in command:
            self.removed = True
            self.files = {k: v for k, v in self.files.items() if not k.startswith(DIRECTORY + "/")}
        if self.failure == "hash" and "sha256sum -c" in command:
            raise RuntimeError("hash failed")
        if command.startswith("sync ") and "/current.tmp" in command:
            self.files[f.BASE + "/current"] = self.files.pop(f.BASE + "/current.tmp")
            if self.failure in ("commit_ack", "commit_probe"):
                raise OSError("commit acknowledgement lost")
        if command.startswith("set -eu; if [ -e " + f.BASE + "/current"):
            if self.failure == "commit_probe":
                raise OSError("probe disconnected")
            return "yes" if self.files.get(f.BASE + "/current", b"").decode().strip() == JOB else "no"
        return super().exec_checked(command)


class FirmwareAuditTests(unittest.TestCase):
    def test_uploaded_image_is_not_reserved_twice_but_reserve_and_tmp_still_apply(self):
        ssh = FakeSSH()
        image = f.Image(Path("unused"), "3.28.0.172", "chiappa", 400 * 1024**2, "a" * 64, 100)
        plan = f.Plan(f.parse_state(state_text()), image, None, ssh.token)
        for free, tmpfree, accepted in ((128 * 1024, 200000, True),
                                        (128 * 1024 - 1, 200000, False),
                                        (128 * 1024, 1, False)):
            with self.subTest(free=free, tmpfree=tmpfree):
                ssh.probe = state_text(free=str(free), tmpfree=str(tmpfree))
                if accepted:
                    f._lock_and_revalidate(ssh, plan)
                else:
                    with self.assertRaises(RuntimeError):
                        f._lock_and_revalidate(ssh, plan)

    def test_own_unsubmitted_stage_is_cleaned_on_upload_hash_and_revalidation_failure(self):
        for failure in ("upload", "hash", "revalidate"):
            with self.subTest(failure=failure):
                ssh = StagingSSH(failure)
                image = f.Image(Path("unused"), "3.28.0.172", "chiappa", 10, "a" * 64, 10)
                plan = f.Plan(f.parse_state(state_text()), image, None, ssh.token)
                with mock.patch.object(f, "preflight", return_value=plan), \
                        mock.patch.object(f.uuid, "uuid4", return_value=mock.Mock(hex=JOB)), \
                        mock.patch.object(f, "_lock_and_revalidate", side_effect=RuntimeError("revalidate failed")):
                    with self.assertRaises((RuntimeError, OSError)):
                        f.start_install(ssh, plan, confirmed=True)
                self.assertTrue(ssh.created)
                self.assertTrue(ssh.removed)
                self.assertNotIn(DIRECTORY + "/image.tmp", ssh.files)

    def test_lost_commit_ack_preserves_commit_flag_and_guard(self):
        ssh = DeviceSSH()
        ssh.files.pop(f.BASE + "/current")
        plan = f.Plan(f.parse_state(state_text()), None, {}, ssh.token)
        committed = {"value": False}
        original = ssh.exec_checked

        def execute(command):
            if command.startswith("sync ") and "mv " in command:
                ssh.files[f.BASE + "/current"] = (JOB + "\n").encode()
                raise OSError("lost acknowledgement")
            return original(command)

        with mock.patch.object(f, "_lock_and_revalidate"), \
                mock.patch.object(ssh, "exec_checked", side_effect=execute):
            with self.assertRaises(OSError):
                f._commit_job(ssh, plan, JOB, committed, "locked")
        self.assertTrue(committed["value"])
        self.assertEqual(ssh.firmware_guard_reason, "locked")
        self.assertTrue(ssh.locked)

    def test_full_install_never_cleans_or_restores_updater_after_uncertain_commit(self):
        for failure in ("commit_ack", "commit_probe"):
            with self.subTest(failure=failure):
                ssh = StagingSSH(failure)
                image = f.Image(Path("unused"), "3.28.0.172", "chiappa", 10, "a" * 64, 10)
                plan = f.Plan(f.parse_state(state_text()), image, None, ssh.token)
                with mock.patch.object(f, "preflight", return_value=plan), \
                        mock.patch.object(f, "_pause_updater_locked", return_value=True), \
                        mock.patch.object(f, "_restore_updater_locked") as restore, \
                        mock.patch.object(f, "_lock_and_revalidate"), \
                        mock.patch.object(f.uuid, "uuid4", return_value=mock.Mock(hex=JOB)):
                    with self.assertRaisesRegex(OSError, "acknowledgement"):
                        f.start_install(ssh, plan, confirmed=True)
                restore.assert_not_called()
                self.assertFalse(ssh.removed)
                self.assertTrue(ssh.firmware_guard_reason)
                self.assertIn(DIRECTORY + "/job.json", ssh.files)
                self.assertIn(f.BASE + "/current", ssh.files)

    def test_existing_or_unacknowledged_stage_is_never_removed(self):
        for error in (FileExistsError("borrowed directory"), OSError("mkdir acknowledgement lost")):
            ssh = StagingSSH("")
            with mock.patch.object(ssh, "exec_checked", side_effect=error), \
                    mock.patch.object(f.uuid, "uuid4", return_value=mock.Mock(hex=JOB)):
                with self.assertRaises(type(error)):
                    with f._job_stage(ssh, {"value": False}):
                        self.fail("Must not enter an unowned stage")
            self.assertFalse(ssh.removed)

    def test_submitted_but_aborted_job_keeps_diagnostic_files(self):
        ssh = StagingSSH("")
        committed = {"value": False}
        with mock.patch.object(f.uuid, "uuid4", return_value=mock.Mock(hex=JOB)):
            with f._job_stage(ssh, committed):
                ssh.files[DIRECTORY + "/install.log"] = b"prewrite check failed"
                committed["submitted"] = True
        self.assertFalse(ssh.removed)
        self.assertIn(DIRECTORY + "/install.log", ssh.files)

    def test_failed_recovery_is_not_new_write_admission(self):
        for changes in ({"swu_status": "3"}, {"rootb_errcnt": "1"}):
            with self.subTest(changes=changes):
                ssh = DeviceSSH()
                ssh.files[DIRECTORY + "/result"] = b"failed\n"
                ssh.probe = state_text(**changes)
                message = f.release_transaction(ssh, ssh.token, confirmed=True)
                self.assertNotIn(f.BASE + "/current", ssh.files)
                self.assertIn("电源键", message)
                self.assertTrue(ssh.firmware_guard_reason)
                with self.assertRaises(RuntimeError):
                    f.assert_idle(f.parse_state(ssh.probe), writing=True)

    def test_unknown_outcome_cannot_discard_job_or_guard(self):
        ssh = DeviceSSH()
        ssh.firmware_guard_reason = "uncertain"
        with self.assertRaises(RuntimeError):
            f.release_transaction(ssh, ssh.token, confirmed=True)
        self.assertIn(f.BASE + "/current", ssh.files)
        self.assertIn(DIRECTORY + "/image.swu", ssh.files)
        self.assertEqual(ssh.firmware_guard_reason, "uncertain")

    def test_failed_release_persists_target_quarantine_before_archiving(self):
        ssh = DeviceSSH()
        ssh.files[DIRECTORY + "/result"] = b"failed\n"
        f.release_transaction(ssh, ssh.token, confirmed=True)
        quarantine = next((i for i, c in enumerate(ssh.commands) if f.BASE + "/unsafe-b" in c), None)
        archive = next(i for i, c in enumerate(ssh.commands) if "/released.json" in c)
        self.assertIsNotNone(quarantine)
        self.assertLess(quarantine, archive)

    def test_switch_checks_quarantine_before_mounting_healthy_rootfs(self):
        ssh = FakeSSH()
        state = f.parse_state(state_text())
        original = ssh.exec_checked

        def execute(command):
            if "unsafe-b" in command:
                raise RuntimeError("quarantined")
            return original(command)

        with mock.patch.object(ssh, "exec_checked", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "quarantined"):
                f.inspect_slot(ssh, state)
        self.assertFalse(any("--property=PrivateMounts" in c for c in ssh.commands))

    def test_install_marks_target_before_native_write_and_clears_only_after_validation(self):
        image = f.Image(Path("unused"), "3.28.0.172", "chiappa", 10, "a" * 64, 10)
        plan = f.Plan(f.parse_state(state_text()), image, None, object())
        script = f.installation_script(JOB, plan)
        marker = f.BASE + "/unsafe-b"
        self.assertIn(marker, script)
        self.assertLess(script.index(marker), script.index("WRITE_STARTED=1"))
        self.assertGreater(script.index("rmdir " + marker), script.index("mv " + DIRECTORY + "/target.tmp"))

    def test_reboot_has_bounded_deadline_and_never_swallows_unknown_outcome(self):
        ssh = mock.Mock()
        ssh.exec_command.return_value = ("", "", 0)
        _device_actions.reboot(ssh)
        self.assertEqual(ssh.exec_command.call_args.kwargs.get("timeout"), _device_actions.QUICK_COMMAND_TIMEOUT)
        ssh.exec_command.side_effect = TimeoutError("result unknown")
        with self.assertRaises(TimeoutError):
            _device_actions.reboot(ssh)
        ssh.exec_command.side_effect = None
        ssh.exec_command.return_value = ("", "permission denied", 1)
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            _device_actions.reboot(ssh)

    @unittest.skipUnless(Path(BASH).exists(), "Git Bash required")
    def test_busybox_style_full_cmdline_and_probe_errors_fail_closed(self):
        # BusyBox pgrep -x compares the whole argv0, not just its basename.
        for cmdline, code, expected in (("/usr/bin/swupdate -i image", 0, "busy"),
                                         ("/bin/bash /usr/sbin/swupdate-from-image-file image", 0, "busy"),
                                         ("", 1, "idle"), ("", 2, None)):
            prelude = f'''systemctl() {{ return 0; }}
pgrep() {{
    [ "$1" = -f ] || return 1
    printf '%s\\n' {f.shell(cmdline)} | grep -Eq "$2" || return 1
    return {code}
}}
'''
            if not cmdline:
                prelude = f"systemctl() {{ return 0; }}\npgrep() {{ return {code}; }}\n"
            result = subprocess.run([BASH, "-c", prelude + f.JOBS_RUNNING_PROBE], capture_output=True, text=True, timeout=10)
            if expected:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
            else:
                self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(Path(BASH).exists(), "Git Bash required")
    def test_unit_probe_errors_and_active_states_are_not_idle(self):
        for units, code, expected in (("", 1, None),
                                      ("job loaded activating start", 0, "busy"),
                                      ("job loaded active running", 0, "busy"),
                                      ("job loaded failed failed", 0, "idle")):
            with self.subTest(units=units, code=code):
                prelude = f"systemctl() {{ printf '%s\\n' {f.shell(units)}; return {code}; }}; pgrep() {{ return 1; }}; "
                result = subprocess.run([BASH, "-c", prelude + f.JOBS_RUNNING_PROBE],
                                        capture_output=True, text=True, timeout=10)
                if expected:
                    self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(Path(BASH).exists(), "Git Bash required")
    def test_standalone_writer_probe_accepts_interpreters_but_not_its_parent_shell(self):
        probe = f.PROBE.split("printf 'writer='", 1)[1].split("printf 'holders='", 1)[0]
        for cmdline, expected in (("/usr/sbin/swupdate-from-image-file image", "busy"),
                                  ("bash /usr/sbin/swupdate-from-image-file image", "busy"),
                                  ("/bin/bash -c " + f.PROBE, "idle"),
                                  ("/usr/bin/swupdate -d -x", "idle")):
            with self.subTest(cmdline=cmdline[:60]):
                prelude = f'''set -eu
pgrep() {{ [ "$1" = -f ]; printf '%s\\n' {f.shell(cmdline)} | grep -Eq "$2"; }}
'''
                result = subprocess.run([BASH, "-c", prelude + probe], capture_output=True, text=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), result.stderr)

    @unittest.skipUnless(Path(BASH).exists(), "Git Bash required")
    def test_shell_stage_cleanup_removes_only_owned_unsubmitted_job(self):
        class ShellSSH:
            def exec_checked(self, command):
                # NTFS cannot enforce Unix modes; keep real path/file operations.
                prelude = 'stat() { echo 0:700; }; mkdir() { [ "$1" != -m ] || shift 2; command mkdir "$@"; }; '
                result = subprocess.run([BASH, "-c", prelude + command],
                                        cwd=tmp, capture_output=True, text=True, timeout=10)
                if result.returncode:
                    raise RuntimeError(result.stderr)
                return result.stdout

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(f, "BASE", "stage"), \
                mock.patch.object(f.uuid, "uuid4", return_value=mock.Mock(hex=JOB)):
            old = Path(tmp) / "stage/old-job/image.swu"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"keep")
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                with f._job_stage(ShellSSH(), {"value": False}) as job:
                    (Path(tmp) / "stage" / job / "image.tmp").write_bytes(b"partial")
                    raise RuntimeError("upload failed")
            self.assertEqual(old.read_bytes(), b"keep")
            self.assertFalse((Path(tmp) / "stage" / JOB).exists())
            with f._job_stage(ShellSSH(), {"value": False}) as job:
                (Path(tmp) / "stage/current").write_text(job)
            self.assertTrue((Path(tmp) / "stage" / JOB).exists())

    @unittest.skipUnless(Path(BASH).exists(), "Git Bash required")
    def test_cached_oem_status_reporting_and_reset_are_not_guessed(self):
        root = Path(__file__).resolve().parents[1] / "build/firmware-cache/official/3.29.1.0"
        hashes = {"chiappa": "bc6c0f0cb99df7337a7c7eb69562ef9ffaaedb4b09f733b7353c6d54856d944d",
                  "ferrari": "228a8f587035a1a3e84b9d05dd9f4b17a751e53b6cbb4023c254d43cb72a1f10"}
        for platform, digest in hashes.items():
            path = root / platform / "extracted/selected-rootfs/usr/lib/swupdate/conf.d/09-swupdate-args"
            if not path.exists():
                self.skipTest("Cached OEM rootfs unavailable")
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            for status, report in (("0", ""), ("1", "-c 2"), ("3", "-c 3"), ("7", "-c 1")):
                with self.subTest(platform=platform, status=status), tempfile.TemporaryDirectory() as tmp:
                    state = Path(tmp) / "swu_status"
                    state.write_text(status)
                    script = raw.decode().replace("/sys/devices/platform/lpgpr/swu_status", "swu_status")
                    prelude = "swupdate() { echo /dev/a; }; realpath() { echo /dev/a; }; "
                    result = subprocess.run([BASH, "-c", prelude + script + '\nprintf "%s" "$SWUPDATE_SURICATTA_ARGS"'],
                                            cwd=tmp, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "-d -x" + (" " + report if report else ""))
                    self.assertEqual(state.read_text().strip(), "0")

    @unittest.skipUnless(Path(BASH).exists(), "Git Bash required")
    def test_install_shell_quarantines_partial_boot_write_until_full_success(self):
        for failure, old_marker, expected in (("check", False, "aborted"),
                                               ("check", True, "aborted"),
                                               ("native", False, "failed"),
                                               ("validation", False, "failed"),
                                               ("none", True, "success")):
            with self.subTest(failure=failure, old_marker=old_marker), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(f, "BASE", "stage"):
                root = Path(tmp)
                directory = root / "stage" / JOB
                directory.mkdir(parents=True)
                marker = root / "stage/unsafe-b"
                if old_marker:
                    marker.mkdir()
                (root / "status").write_text("0")
                for supply, attrs in {"battery": {"type": "Battery", "capacity": "100"},
                                      "charger": {"type": "Mains", "online": "1"}}.items():
                    node = root / "power" / supply
                    node.mkdir(parents=True)
                    for key, value in attrs.items():
                        (node / key).write_text(value)
                image = f.Image(Path("unused"), "3.28.0.172", "chiappa", 10, "a" * 64, 10)
                plan = f.Plan(f.parse_state(state_text()), image, None, object())
                script = f.installation_script(JOB, plan).replace("/sys/class/power_supply", "power")
                prelude = f'''mkdir() {{ [ "$1" != -m ] || shift 2; command mkdir "$@"; }}
sync() {{ :; }}
rootdev() {{ echo /dev/mmcblk0p2; }}
cat() {{
 case "$1" in
  {f.LPGPR}/swu_status) command cat status;;
  {f.LPGPR}/*) echo 0;;
  *) command cat "$@";;
 esac
}}
systemctl() {{ case "$*" in *ActiveState*) echo inactive;; *UnitFileState*) echo masked-runtime;; *) return 2;; esac; }}
fuser() {{ return 1; }}
sha256sum() {{ command cat >/dev/null; }}
swupdate() {{
 if [ "$1" = -c ]; then return {1 if failure == 'check' else 0}; fi
 echo boot-written > boot-write
 echo 1 > status
 return {1 if failure == 'native' else 0}
}}
systemd-run() {{ echo version=3.28.0.172; return {1 if failure == 'validation' else 0}; }}
'''
                result = subprocess.run([BASH, "-c", prelude + script], cwd=tmp,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual((directory / "result").read_text().strip(), expected, result.stderr)
                self.assertEqual(result.returncode == 0, expected == "success")
                self.assertEqual(marker.exists(), old_marker if expected == "aborted" else expected == "failed")
                self.assertEqual((root / "boot-write").exists(), expected != "aborted")

    def test_all_firmware_reboot_entries_have_a_bounded_deadline(self):
        ssh = FakeSSH()
        with mock.patch.object(f, "query_transaction", return_value=("none", "")), \
                mock.patch.object(ssh, "exec_checked", wraps=ssh.exec_checked) as execute:
            f.reboot_for_plugin_recovery(ssh)
        execute.assert_called_with("systemctl reboot", timeout=_device_actions.QUICK_COMMAND_TIMEOUT)

        ssh = DeviceSSH()
        target = "version=3.28.0.172\ninternal=20260828000000\nxochitl=" + "a" * 64 + "\nhardware=-H chiappa:1.0\n"
        ssh.files[DIRECTORY + "/target"] = target.encode()
        ssh.probe = state_text(swu_status="1")

        def checked(command, **kwargs):
            if command == f.PROBE:
                return ssh.probe
            if command == f.isolated_slot_command("b"):
                return target
            if command == "systemctl is-enabled rm-apply-ota.service":
                return "enabled"
            return ""

        with mock.patch.object(f, "query_transaction", return_value=("success", "")), \
                mock.patch.object(ssh, "exec_checked", side_effect=checked) as execute, \
                mock.patch.object(ssh, "close", create=True):
            f.reboot_after_success(ssh, ssh.token, confirmed=True)
        execute.assert_called_with("systemctl reboot", timeout=_device_actions.QUICK_COMMAND_TIMEOUT)


class FirmwareUIAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.ssh = f.FirmwareSSHClientWrapper()
        self.ssh._client = mock.Mock()
        self.ssh._client.get_transport.return_value.is_active.return_value = True
        self.page = ui.FirmwareTab(self.ssh)

    def tearDown(self):
        QtCore.QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()
        self.page.deleteLater()

    def test_all_ui_submission_paths_snapshot_without_ensure_client(self):
        with mock.patch.object(self.ssh, "ensure_client", side_effect=AssertionError("transport lock on GUI")), \
                mock.patch.object(self.page, "_run"), \
                mock.patch.object(ui, "ask_confirmation", return_value=False):
            self.page.refresh()
            self.page.restore()
            self.page.release_lock()
            self.page._reboot_device()

    def test_worker_submission_does_not_acquire_transport_lock_on_gui(self):
        delivered = []
        with mock.patch.object(self.ssh, "ensure_client", side_effect=AssertionError("GUI lock")), \
                mock.patch.object(QtCore.QThreadPool, "start") as start:
            self.page._run(lambda: "ok", delivered.append, device=True)
        self.assertEqual(start.call_count, 1)

    def test_running_guard_does_not_turn_unrelated_success_into_unknown(self):
        self.page.state = f.parse_state(state_text())
        self.page.transaction = ("running", "writing")
        self.ssh.firmware_guard_reason = "write in progress"
        done = mock.Mock()
        with mock.patch.object(QtCore.QThreadPool, "start"):
            self.page._run(lambda: None, done, device=True)
            self.page.worker.signals.finished.emit("done")
        self.assertEqual(self.page.transaction[0], "running")
        self.assertTrue(self.page.buttons["release"].isHidden())
        done.assert_called_once_with("done")

    def test_worker_rejects_replaced_connection_without_running_action_or_stale_dialog(self):
        action = mock.Mock()
        done = mock.Mock()
        with mock.patch.object(QtCore.QThreadPool, "start"), \
                mock.patch.object(ui, "show_error") as error:
            self.page._run(action, done, device=True)
            self.ssh._client = mock.Mock()
            self.ssh._client.get_transport.return_value.is_active.return_value = True
            self.page.worker.run()
        action.assert_not_called()
        done.assert_not_called()
        error.assert_not_called()

    def test_refresh_returns_while_transport_is_owned_by_another_worker(self):
        held, release = threading.Event(), threading.Event()

        def hold_transport():
            with self.ssh._transport_lock:
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold_transport)
        thread.start()
        try:
            self.assertTrue(held.wait(2))
            with mock.patch.object(self.ssh, "ensure_client", side_effect=AssertionError("GUI waited")), \
                    mock.patch.object(QtCore.QThreadPool, "start") as start:
                self.page.refresh()
            self.assertEqual(start.call_count, 1)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_background_submission_does_not_bypass_plugin_guard(self):
        self.ssh.firmware_guard_reason = "firmware writing"
        with mock.patch.object(QtCore.QThreadPool, "start"), \
                mock.patch.object(ui, "show_error") as error:
            self.page._run(lambda: self.ssh._firmware_gate(), lambda _: self.fail("Guard bypassed"), device=True)
            self.page.worker.run()
        self.assertIn("firmware writing", str(error.call_args.args))


if __name__ == "__main__":
    unittest.main()
