"""Failed, aborted and uncertain firmware jobs must not lock a device forever."""

import json
import os
import shlex
import unittest
from contextlib import nullcontext
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _firmware as f
from tests.test_firmware import FakeSSH, state_text


JOB = "a" * 32
DIRECTORY = f.BASE + "/" + JOB
LOCK = "rmdir " + f.LOCK_PATH
LOAD_STATE = f.shell("systemctl", "show", f.UNIT + "-" + JOB, "-p", "LoadState")


class DeviceSSH(FakeSSH):
    """FakeSSH that also applies the file moves used by archiving."""

    def __init__(self):
        super().__init__()
        self.locked = True
        self.files.update({
            f.BASE + "/current": (JOB + "\n").encode(),
            DIRECTORY + "/job.json": json.dumps(dict(
                job=JOB, kind="install", target="b", platform="chiappa",
                version="3.28.0.172", boot_id="boot")).encode(),
            DIRECTORY + "/image.swu": b"image",
        })
        self.props = "LoadState=loaded\nActiveState=inactive\nSubState=exited\nResult=exit-code\nExecMainStatus=1"
        self.jobs_running = False
        self.start_props = None

    def exec_checked(self, command):
        if command == f.JOBS_RUNNING_PROBE:
            self.commands.append(command)
            return "busy" if self.jobs_running else "idle"
        if command == LOAD_STATE and self.start_props is not None:
            self.commands.append(command)
            return self.start_props
        if ".withdrawn" in command:
            for script in ("install.sh", "switch.sh"):
                name = DIRECTORY + "/" + script
                if name in self.files:
                    self.files[name + ".withdrawn"] = self.files.pop(name)
        for segment in command.split("rm -f ")[1:]:
            for name in segment.split(";", 1)[0].split():
                self.files.pop(name, None)
        if "/released.json" in command and "mv -f " in command:
            source, target = shlex.split(command.split("mv -f ", 1)[1].split(";", 1)[0])
            self.files[target] = self.files.pop(source)
        if LOCK in command:
            self.locked = False
        if command.startswith('if [ "$(cat ' + f.BASE + '/current'):
            self.commands.append(command)
            current = self.files.get(f.BASE + "/current", b"").decode().strip()
            return "yes" if current == JOB else "no"
        if command.startswith(f"if [ -e {DIRECTORY}/result ]"):
            self.commands.append(command)
            return "yes" if DIRECTORY + "/result" in self.files else "no"
        return super().exec_checked(command)


class AbortedJobTests(unittest.TestCase):
    def setUp(self):
        self.ssh = DeviceSSH()
        self.ssh.files[DIRECTORY + "/result"] = b"aborted\n"

    def test_job_stopped_before_writing_is_released_automatically(self):
        status, message = f.query_transaction(self.ssh)
        self.assertEqual(status, "none")
        self.assertIn("未改动", message)
        self.assertNotIn(f.BASE + "/current", self.ssh.files)
        self.assertNotIn(DIRECTORY + "/image.swu", self.ssh.files)
        released = json.loads(self.ssh.files[DIRECTORY + "/released.json"])
        self.assertEqual(released["released"], "aborted")
        self.assertIn(DIRECTORY + "/job.json", self.ssh.files)
        self.assertFalse(self.ssh.locked)
        f.inspect_device(self.ssh)
        self.assertEqual(self.ssh.firmware_guard_reason, "")

    def test_aborted_job_from_earlier_boot_keeps_lock_of_new_boot(self):
        self.ssh.files["/proc/sys/kernel/random/boot_id"] = b"later"
        self.assertEqual(f.query_transaction(self.ssh)[0], "none")
        self.assertTrue(self.ssh.locked)

    def test_aborted_job_stays_locked_when_device_is_not_idle(self):
        for change in ({"swu_status": "1"}, {"boot": "2"}, {"roota_errcnt": "1"},
                       {"holders": "busy"}):
            with self.subTest(change=change):
                self.ssh.probe = state_text(**change)
                self.assertEqual(f.query_transaction(self.ssh)[0], "unknown")
                self.assertIn(f.BASE + "/current", self.ssh.files)
                self.assertTrue(self.ssh.locked)

    def test_scripts_report_aborted_only_before_the_first_write(self):
        state = f.parse_state(state_text())
        image = f.Image(mock.Mock(), "3.28.0.172", "chiappa", 10, "a" * 64, 10)
        script = f.installation_script(JOB, f.Plan(state, image, None, object()))
        write = script.index("WRITE_STARTED=1")
        self.assertLess(script.index("require_safe_power /sys"), write)
        self.assertLess(script.index("swupdate -c"), write)
        native = f.native_check_command(DIRECTORY + "/image.swu", "chiappa", "a")
        self.assertLess(write, script.index(native.replace(" -c ", " ")))
        self.assertIn('elif [ "$WRITE_STARTED" = 0 ]; then echo aborted', script)

        ssh = FakeSSH()
        plan = f.Plan(state, None, {"version": "3.28.0.172"}, ssh.token)
        with mock.patch.object(f, "preflight", return_value=plan), \
                mock.patch.object(f, "_lock_and_revalidate"), \
                mock.patch.object(f.uuid, "uuid4", return_value=mock.Mock(hex=JOB)):
            f.switch_slot(ssh, plan, confirmed=True)
        switch = ssh.files[DIRECTORY + "/switch.sh"].decode()
        self.assertLess(switch.index("WRITE_STARTED=1"), switch.index("rootdev --switch"))
        self.assertLess(switch.index("require_safe_power /sys"), switch.index("WRITE_STARTED=1"))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.ssh = DeviceSSH()
        self.ssh.files[DIRECTORY + "/result"] = b"failed\n"

    def test_requires_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "确认"):
            f.release_transaction(self.ssh, self.ssh.token)
        self.assertIn(f.BASE + "/current", self.ssh.files)

    def test_failed_job_on_idle_device_is_released_and_logs_kept(self):
        self.ssh.firmware_guard_reason = "locked"
        message = f.release_transaction(self.ssh, self.ssh.token, confirmed=True)
        self.assertIn("不要切换到备用分区", message)
        self.assertNotIn(f.BASE + "/current", self.ssh.files)
        self.assertIn(DIRECTORY + "/job.json", self.ssh.files)
        self.assertIn(DIRECTORY + "/result", self.ssh.files)
        self.assertNotIn(DIRECTORY + "/image.swu", self.ssh.files)
        self.assertEqual(json.loads(self.ssh.files[DIRECTORY + "/released.json"])["released"], "failed")
        self.assertFalse(self.ssh.locked)
        self.assertEqual(self.ssh.firmware_guard_reason, "")
        self.assertEqual(f.query_transaction(self.ssh)[0], "none")
        commands = "\n".join(self.ssh.commands)
        for forbidden in ("echo 0 >", "rootdev --switch", "systemctl reboot", "reset-failed"):
            self.assertNotIn(forbidden, commands)

    def test_uncertain_job_without_result_can_be_released_in_same_boot(self):
        del self.ssh.files[DIRECTORY + "/result"]
        self.ssh.props = "LoadState=not-found\nActiveState=inactive\nSubState=dead"
        self.assertEqual(f.query_transaction(self.ssh)[0], "unknown")
        f.release_transaction(self.ssh, self.ssh.token, confirmed=True)
        self.assertEqual(f.query_transaction(self.ssh)[0], "none")

    def test_refuses_while_device_shows_update_or_partition_risk(self):
        for change in ({"swu_status": "1"}, {"swu_applied": "1"}, {"boot": "2"},
                       {"rootb_errcnt": "1"}, {"writer": "busy"}, {"holders": "busy"}):
            with self.subTest(change=change):
                self.ssh.probe = state_text(**change)
                with self.assertRaises(RuntimeError):
                    f.release_transaction(self.ssh, self.ssh.token, confirmed=True)
                self.assertIn(f.BASE + "/current", self.ssh.files)
                self.assertTrue(self.ssh.locked)

    def test_refuses_running_job_and_jobs_that_need_no_release(self):
        self.ssh.props = "LoadState=loaded\nActiveState=activating\nSubState=start"
        with self.assertRaisesRegex(RuntimeError, "不需要解除"):
            f.release_transaction(self.ssh, self.ssh.token, confirmed=True)
        self.ssh.props = "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0"
        self.ssh.files[DIRECTORY + "/result"] = b"success\n"
        with self.assertRaisesRegex(RuntimeError, "不需要解除"):
            f.release_transaction(self.ssh, self.ssh.token, confirmed=True)
        self.assertIn(f.BASE + "/current", self.ssh.files)

    def test_refuses_when_any_firmware_unit_or_writer_is_running(self):
        # query_transaction failed (unknown) although the unit is still running.
        self.ssh.jobs_running = True
        with mock.patch.object(f, "query_transaction", return_value=("unknown", "uncertain")):
            with self.assertRaisesRegex(RuntimeError, "仍有固件任务"):
                f.release_transaction(self.ssh, self.ssh.token, confirmed=True)
        self.assertIn(f.BASE + "/current", self.ssh.files)
        self.assertTrue(self.ssh.locked)

    def test_invalid_pointer_is_released_but_unknown_lock_is_reported(self):
        self.ssh.files[f.BASE + "/current"] = b"garbage\n"
        self.assertEqual(f.query_transaction(self.ssh)[0], "unknown")
        self.ssh.probe = state_text(shared_lock="busy")
        message = f.release_transaction(self.ssh, self.ssh.token, confirmed=True)
        self.assertNotIn(f.BASE + "/current", self.ssh.files)
        self.assertTrue(self.ssh.locked)
        self.assertIn("插件事务锁仍在", message)

    def test_stale_connection_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "连接已变化"):
            f.release_transaction(self.ssh, object(), confirmed=True)
        self.assertIn(f.BASE + "/current", self.ssh.files)

    def test_guard_message_points_to_release(self):
        f.inspect_device(self.ssh)
        self.assertIn("解除锁定", self.ssh.firmware_guard_reason)
        self.ssh.files[DIRECTORY + "/result"] = b"success\n"
        self.ssh.props = "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0"
        f.inspect_device(self.ssh)
        self.assertNotIn("解除锁定", self.ssh.firmware_guard_reason)


class CommitAndStartTests(unittest.TestCase):
    def setUp(self):
        self.ssh = DeviceSSH()
        self.ssh.files.pop(f.BASE + "/current")
        self.plan = f.Plan(f.parse_state(state_text()), None, {"version": "3.28.0.172"}, self.ssh.token)
        self.committed = {"value": False}

    def commit(self):
        with mock.patch.object(f, "_lock_and_revalidate"):
            f._commit_job(self.ssh, self.plan, JOB, self.committed, "locked")

    def test_leftover_temporary_pointer_is_removed_before_commit(self):
        self.ssh.files[f.BASE + "/current.tmp"] = b"stale\n"
        writes = []
        original = f._write_remote

        def write(ssh, path, data):
            writes.append(path)
            if path in ssh.files:
                raise FileExistsError(path)
            original(ssh, path, data)

        with mock.patch.object(f, "_write_remote", side_effect=write):
            self.commit()
        self.assertEqual(writes, [f.BASE + "/current.tmp"])
        self.assertTrue(self.committed["value"])
        removal = self.ssh.commands.index(f"rm -f {f.BASE}/current.tmp")
        self.assertLess(removal, next(i for i, c in enumerate(self.ssh.commands) if c.startswith("sync ")))

    def test_failed_commit_releases_lock_only_when_pointer_was_not_moved(self):
        original = self.ssh.exec_checked

        def execute(command):
            if command.startswith("sync ") and "mv " in command:
                raise RuntimeError("timeout")
            return original(command)

        with mock.patch.object(self.ssh, "exec_checked", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                self.commit()
        self.assertFalse(self.ssh.locked)
        self.assertEqual(self.ssh.firmware_guard_reason, "")
        self.assertFalse(self.committed["value"])

        self.ssh.locked = True
        self.ssh.firmware_guard_reason = ""

        def execute_after_move(command):
            if command.startswith("sync ") and "mv " in command:
                self.ssh.files[f.BASE + "/current"] = (JOB + "\n").encode()
                raise RuntimeError("lost acknowledgement")
            return original(command)

        with mock.patch.object(self.ssh, "exec_checked", side_effect=execute_after_move):
            with self.assertRaises(RuntimeError):
                self.commit()
        self.assertTrue(self.ssh.locked)
        self.assertEqual(self.ssh.firmware_guard_reason, "locked")

    def start(self, failure):
        self.ssh.files[f.BASE + "/current"] = (JOB + "\n").encode()
        self.committed["value"] = True
        original = self.ssh.exec_checked

        def execute(command):
            if command.startswith("systemd-run --unit="):
                raise RuntimeError(failure)
            return original(command)

        with mock.patch.object(self.ssh, "exec_checked", side_effect=execute):
            return f._start_job(self.ssh, JOB, self.committed, "systemd-run --unit=x",
                                ("unknown", "uncertain"), ("running", "started"))

    def test_unit_that_never_started_is_withdrawn(self):
        self.ssh.props = "LoadState=not-found\nActiveState=inactive\nSubState=dead"
        with self.assertRaisesRegex(RuntimeError, "已撤回"):
            self.start("invalid property")
        self.assertNotIn(f.BASE + "/current", self.ssh.files)
        self.assertNotIn(DIRECTORY + "/image.swu", self.ssh.files)
        self.assertFalse(self.ssh.locked)
        self.assertFalse(self.committed["value"])
        self.assertEqual(self.ssh.firmware_guard_reason, "")

    def test_job_that_stopped_before_writing_is_reported_and_released(self):
        self.ssh.files[DIRECTORY + "/result"] = b"aborted\n"
        with self.assertRaisesRegex(RuntimeError, "未改动"):
            self.start("Job failed")
        self.assertNotIn(f.BASE + "/current", self.ssh.files)
        self.assertFalse(self.committed["value"])

    def test_job_that_failed_while_writing_stays_committed(self):
        self.ssh.files[DIRECTORY + "/result"] = b"failed\n"
        self.assertEqual(self.start("Job failed")[0], "failed")
        self.assertIn(f.BASE + "/current", self.ssh.files)
        self.assertTrue(self.committed["value"])
        self.assertTrue(self.ssh.locked)

    def test_unit_that_started_and_died_without_result_stays_locked(self):
        for props in ("LoadState=loaded\nActiveState=failed\nSubState=failed",
                      "LoadState=loaded\nActiveState=inactive\nSubState=dead"):
            with self.subTest(props=props):
                self.ssh.locked = True
                self.ssh.props = props
                self.ssh.files[DIRECTORY + "/install.sh"] = b"script"
                result = self.start("Job failed")
                self.assertEqual(result, ("unknown", "uncertain"))
                self.assertIn(f.BASE + "/current", self.ssh.files)
                self.assertTrue(self.ssh.locked)
                self.assertTrue(self.committed["value"])
                self.assertIn(DIRECTORY + "/install.sh", self.ssh.files)

    def test_unit_that_appears_after_disarming_stays_locked(self):
        self.ssh.files[DIRECTORY + "/install.sh"] = b"script"
        self.ssh.props = "LoadState=loaded\nActiveState=activating\nSubState=start"
        answers = iter(["LoadState=not-found", "LoadState=loaded"])
        original = self.ssh.exec_checked

        def execute(command):
            if command == LOAD_STATE:
                self.ssh.commands.append(command)
                return next(answers)
            return original(command)

        self.ssh.files[f.BASE + "/current"] = (JOB + "\n").encode()
        self.committed["value"] = True
        with mock.patch.object(self.ssh, "exec_checked", side_effect=execute):
            def start(command):
                if command.startswith("systemd-run --unit="):
                    raise RuntimeError("timeout")
                return execute(command)
            with mock.patch.object(self.ssh, "exec_checked", side_effect=start):
                result = f._start_job(self.ssh, JOB, self.committed, "systemd-run --unit=x",
                                      ("unknown", "uncertain"), ("running", "started"))
        self.assertEqual(result[0], "running")
        self.assertIn(f.BASE + "/current", self.ssh.files)
        self.assertTrue(self.ssh.locked)
        self.assertIn(DIRECTORY + "/install.sh.withdrawn", self.ssh.files)

    def test_release_by_another_client_is_not_reported_as_unchanged(self):
        self.ssh.files[DIRECTORY + "/result"] = b"failed\n"
        original = f.query_transaction

        def released_elsewhere(ssh):
            ssh.files.pop(f.BASE + "/current", None)
            ssh.files[DIRECTORY + "/released.json"] = json.dumps({"released": "failed"}).encode()
            return original(ssh)

        with mock.patch.object(f, "query_transaction", side_effect=released_elsewhere):
            self.assertEqual(self.start("Job failed"), ("unknown", "uncertain"))

    def test_commit_refuses_to_replace_another_active_job(self):
        other = "b" * 32
        self.ssh.files[f.BASE + "/current"] = (other + "\n").encode()
        with mock.patch.object(f, "query_transaction", return_value=("failed", "other job")):
            with self.assertRaisesRegex(RuntimeError, "其他固件任务"):
                self.commit()
        self.assertEqual(self.ssh.files[f.BASE + "/current"], (other + "\n").encode())
        self.assertFalse(self.committed["value"])

    def test_lost_connection_stays_uncertain(self):
        original = self.ssh.exec_checked

        def execute(command):
            if command.startswith(("systemd-run --unit=", "systemctl show")):
                raise OSError("disconnected")
            return original(command)

        self.ssh.files[f.BASE + "/current"] = (JOB + "\n").encode()
        self.committed["value"] = True
        with mock.patch.object(self.ssh, "exec_checked", side_effect=execute):
            result = f._start_job(self.ssh, JOB, self.committed, "systemd-run --unit=x",
                                  ("unknown", "uncertain"), ("running", "started"))
        self.assertEqual(result, ("unknown", "uncertain"))
        self.assertIn(f.BASE + "/current", self.ssh.files)
        self.assertTrue(self.committed["value"])

    def test_withdrawn_start_restores_paused_updater(self):
        self.ssh.props = "LoadState=not-found\nActiveState=inactive\nSubState=dead"
        plan = f.Plan(f.parse_state(state_text(engine="active", engine_file="enabled")), None,
                      {"version": "3.28.0.172"}, self.ssh.token)
        original = self.ssh.exec_checked

        def execute(command):
            if command.startswith("systemd-run --unit=rmtool-firmware-"):
                raise RuntimeError("invalid property")
            return original(command)

        with mock.patch.object(f, "preflight", return_value=plan), \
                mock.patch.object(f, "_pause_updater_locked", return_value=True), \
                mock.patch.object(f, "_restore_updater_locked") as restore, \
                mock.patch.object(f, "_lock_and_revalidate"), \
                mock.patch.object(f.uuid, "uuid4", return_value=mock.Mock(hex=JOB)), \
                mock.patch.object(self.ssh, "exec_checked", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "已撤回"):
                f.switch_slot(self.ssh, plan, confirmed=True)
        restore.assert_called_once_with(self.ssh)


class PauseReceiptTests(unittest.TestCase):
    def test_leftover_pause_receipt_is_replaced(self):
        ssh = FakeSSH()
        ssh.files[f.BASE + "/pause.json.tmp"] = b"stale"
        state = f.parse_state(state_text(engine="active", engine_file="enabled"))
        original = f._write_remote

        def write(target, path, data):
            if path in target.files:
                raise FileExistsError(path)
            original(target, path, data)

        def execute(command):
            if command == f"rm -f {f.BASE}/pause.json.tmp":
                ssh.files.pop(f.BASE + "/pause.json.tmp", None)
            return FakeSSH.exec_checked(ssh, command)

        with mock.patch.object(f, "_write_remote", side_effect=write), \
                mock.patch.object(ssh, "exec_checked", side_effect=execute), \
                mock.patch.object(ssh, "operation_session", return_value=nullcontext()):
            self.assertTrue(f._pause_updater_locked(ssh, state))
        self.assertIn(b"active", ssh.files[f.BASE + "/pause.json.tmp"])


if __name__ == "__main__":
    unittest.main()
