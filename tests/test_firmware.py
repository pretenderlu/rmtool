import copy
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import _firmware as f


def state_text(**changes):
    values = dict(machine="reMarkable Chiappa", arch="aarch64", root="/dev/mmcblk0p2",
                  a="/dev/mmcblk0p2", b="/dev/mmcblk0p3", boot="1", root_part="a",
                  roota_errcnt="0", rootb_errcnt="0", swu_status="0", swu_applied="0",
                  swu_recovery="0", boot_flow="regular", schema="444", battery="100", power="1",
                  free="46843668", tmpfree="1004316", engine="inactive", engine_file="masked-runtime",
                  writer="idle", holders="idle", shared_lock="idle", version="3.28.0.169")
    values.update(changes)
    return "\n".join(f"{k}={v}" for k, v in values.items())


def cpio(entries):
    archive = bytearray()
    for name, data, mode, links in entries + [("TRAILER!!!", b"", 0, 1)]:
        name = name.encode() + b"\0"
        fields = [0, mode, 0, 0, links, 0, len(data), 0, 0, 0, 0, len(name), 0]
        archive += b"070701" + "".join(f"{n:08x}" for n in fields).encode() + name
        archive += b"\0" * (-len(archive) % 4)
        archive += data + b"\0" * (-len(data) % 4)
    return bytes(archive)


def small_swu(platform="chiappa", extra=(), descriptor_change=None):
    payloads = {"root.gz": b"root payload", "imx-boot": b"boot",
                "postinstall.sh": b"#!/bin/sh\nexit 0\n"}
    def item(name, more=""):
        digest = hashlib.sha256(payloads[name]).hexdigest()
        return '{ filename="' + name + '"; sha256="' + digest + '";' + more + '}'
    copies = []
    for i, slot in enumerate(("a", "b"), 1):
        copies.append(f'copy{i}: {{images: ({item("root.gz", f"device=\"/dev/disk/by-partlabel/root_{slot}\";")});'
                      f'files: ({item("imx-boot")}); scripts: ({item("postinstall.sh")});}};')
    board = '{hardware-compatibility:["1.0"];stable:{' + ''.join(copies) + '};}'
    description = 'software={version="3.28.0.172";' + platform + '=' + board + ';'
    if platform == "ferrari":
        description += 'CT-PCBA-IMX8MM=' + board + ';'
    description += '};'
    if descriptor_change:
        description = descriptor_change(description)
    entries = [("sw-description", description.encode(), stat.S_IFREG | 0o644, 1),
               ("sw-description.sig", b"not-an-authenticated-signature", stat.S_IFREG | 0o644, 1)]
    entries += [(name, value, stat.S_IFREG | 0o644, 1) for name, value in payloads.items()]
    return cpio(entries + list(extra))


class FakeSSH:
    def __init__(self):
        self.token = object()
        self.commands = []
        self.files = {"/proc/sys/kernel/random/boot_id": b"boot"}
        self.firmware_guard_reason = ""
        self.probe = state_text()
        self.props = "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0"

    @contextmanager
    def operation_session(self):
        yield

    def ensure_client(self):
        return self.token

    def exec_checked(self, command):
        self.commands.append(command)
        if command == f.PROBE:
            return self.probe
        if command.startswith("if [ -e " + f.BASE):
            return "yes" if f.BASE + "/current" in self.files else "no"
        if command.startswith("systemctl show rmtool-firmware-"):
            return self.props
        if command == "sha256sum /usr/bin/xochitl":
            return "a" * 64 + "  /usr/bin/xochitl"
        if command == "systemd-run --help":
            return "--wait --pipe --collect --property"
        if command in ("rootdev --active", "rootdev --next-boot"):
            return "/dev/mmcblk0p2"
        if command == "systemctl is-enabled rm-apply-ota.service":
            return "enabled"
        return ""

    def exec_command(self, command):
        return self.exec_checked(command), "", 0

    @contextmanager
    def open_remote(self, path, mode):
        if mode == "r":
            yield io.BytesIO(self.files[path])
        else:
            stream = io.BytesIO()
            yield stream
            self.files[path] = stream.getvalue()


class ImageTests(unittest.TestCase):
    def inspect(self, data, expected=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.swu"
            path.write_bytes(data)
            return f.inspect_image(path, expected)

    def test_both_board_layouts(self):
        for platform in ("chiappa", "ferrari"):
            with self.subTest(platform=platform):
                image = self.inspect(small_swu(platform))
                self.assertEqual((image.version, image.platform), ("3.28.0.172", platform))

    def test_mismatched_platform(self):
        with self.assertRaises(RuntimeError):
            self.inspect(small_swu(), "ferrari")

    def test_unsafe_entries(self):
        for name, mode, links in (("../evil", stat.S_IFREG, 1), ("evil", stat.S_IFLNK, 1),
                                  ("evil", stat.S_IFREG, 2), ("evil", stat.S_IFIFO, 1),
                                  ("postinstall.sh", stat.S_IFREG, 1), ("evil", stat.S_IFREG | 0o4000, 1)):
            with self.subTest(name=name, mode=mode, links=links), self.assertRaises(RuntimeError):
                self.inspect(small_swu(extra=((name, b"x", mode, links),)))

    def test_corrupt_truncated_and_trailing_data(self):
        data = small_swu()
        for corrupt in (data[:100], data[:-15], data + b"evil", data.replace(b"root payload", b"evil payload")):
            with self.subTest(size=len(corrupt)), self.assertRaises(RuntimeError):
                self.inspect(corrupt)

    def test_alias_disagreement_and_wrong_target(self):
        for change in (lambda s: s.replace('root_a', 'root_b', 1),
                       lambda s: s.replace('CT-PCBA-IMX8MM=', 'unknown=')):
            with self.assertRaises(RuntimeError):
                self.inspect(small_swu("ferrari", descriptor_change=change))

    def test_descriptor_duplicate_and_unsupported_values(self):
        for raw in (b'a=true;a=false;', b'a=7;', b'a={', b'a="x"; junk'):
            with self.assertRaises(RuntimeError):
                f._description(raw)

    def test_official_url_boundary(self):
        for url in ("http://dlqathbgqp3nv.cloudfront.net/x", "https://evil.example/x",
                    "https://dlqathbgqp3nv.cloudfront.net.evil/x", "https://a@dlqathbgqp3nv.cloudfront.net/x"):
            with self.assertRaises(RuntimeError):
                f._official_url(url)

    def test_official_listing_and_unknown_channel(self):
        xml = b'<ListBucketResult><Contents><Key>remarkable-production-image-3.28.0.172-chiappa-public.swu</Key><Size>10</Size></Contents><IsTruncated>false</IsTruncated></ListBucketResult>'
        with mock.patch.object(f, "_open", return_value=io.BytesIO(xml)):
            releases = f.list_releases("chiappa")
        self.assertEqual(releases[0].channel, "正式版")

    def test_failed_download_does_not_leave_cache_image(self):
        release = f.Release("3.28.0.172", "chiappa", "remarkable-production-image-3.28.0.172-chiappa-public.swu", 20)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(f, "_open", return_value=io.BytesIO(b"short")):
            with self.assertRaises(RuntimeError):
                f.download_release(release, tmp)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_real_172_when_cache_available(self):
        root = Path(os.environ.get("RMTOOL_FIRMWARE_FIXTURES", "E:/remarkable/firmware-cache/official/3.28.0.172"))
        if not root.exists():
            self.skipTest("External official .172 fixtures not present")
        for platform, digest in (("chiappa", "ebbbd8fb3a7c63f0d722988fe2652d6e7c36c090afeb5676134fb0b2b4a8dcbd"),
                                 ("ferrari", "2c920ed07c3862f801c527cdc9fe89d1b07a16aad60e3b76236c71585349dade")):
            image = f.inspect_image(next((root / platform).glob("*.swu")), platform)
            self.assertEqual(image.sha256, digest)


class SafetyTests(unittest.TestCase):
    def test_actual_probe_and_aliases(self):
        for machine, platform in (("reMarkable Chiappa", "chiappa"), ("reMarkable Paper Pro", "ferrari"),
                                  ("CT-PCBA-IMX8MM", "ferrari")):
            self.assertEqual(f.parse_state(state_text(machine=machine)).platform, platform)

    def test_low_power_and_busy_gate_only_writes(self):
        for change in ({"battery": "49"}, {"power": "0"}, {"free": "1"}, {"tmpfree": "1"},
                       {"swu_status": "7"}, {"swu_applied": "1"}, {"boot": "2"},
                       {"roota_errcnt": "1"}, {"holders": "busy"}, {"shared_lock": "busy"},
                       {"engine": "active", "engine_file": "enabled"}):
            state = f.parse_state(state_text(**change))
            image = f.Image(Path("x"), "3.28.0.172", "chiappa", 1024, "a" * 64, 1024)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                f.assert_idle(state, writing=True, image=image)
        f.assert_idle(f.parse_state(state_text(battery="1", power="0", engine="active", engine_file="enabled")))

    def test_malformed_states_fail_closed(self):
        for change in ({"version": ""}, {"boot_flow": "recovery"}, {"schema": "644"},
                       {"arch": "armv7l"}, {"root_part": "b"}, {"battery": "NaN"}, {"a": "/dev/evil"}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                f.parse_state(state_text(**change))

    def test_no_silent_resets_or_cancellation_or_auto_reboot(self):
        state = f.parse_state(state_text())
        image = f.Image(Path("x"), "3.28.0.172", "chiappa", 10, "a" * 64, 10)
        script = f.installation_script("a" * 32, f.Plan(state, image, (), None, object()))
        for forbidden in ("echo 0 >", "kill", "systemctl stop", "systemctl reboot", "swupdate-from-image-file", "source ", "cpio -i"):
            self.assertNotIn(forbidden, script)
        self.assertIn("fuser /dev/mmcblk0p3", script)
        self.assertNotIn("fuser /dev/mmcblk0p2", script)
        self.assertIn("swupdate -c", script)
        self.assertIn("result.tmp", script)
        self.assertIn("sync", script)

    def test_native_command_and_slot_probe(self):
        command = f.native_check_command(f.BASE + "/" + "a" * 32 + "/image.swu", "ferrari", "b")
        self.assertIn("stable,copy1", command)
        with self.assertRaises(RuntimeError):
            f.native_check_command("x; reboot", "ferrari", "a")
        script = f.slot_script("b")
        self.assertIn("ro,noload", script)
        self.assertIn("IMG_VERSION", script)
        self.assertNotIn("RELEASE_VERSION", f.PROBE + script)
        self.assertIn("e2fsck -fn", script)
        command = f.isolated_slot_command("b")
        self.assertIn("systemd-run", command)
        self.assertIn("--wait", command)
        self.assertIn("--pipe", command)
        self.assertIn("--collect", command)
        self.assertIn("PrivateMounts=yes", command)
        self.assertNotIn("unshare", command)

    def test_remote_lock_revalidates_and_releases_on_mismatch(self):
        ssh = FakeSSH()
        plan = f.Plan(f.parse_state(state_text()), None, (), {"version": "3.28.0.172"}, ssh.token)
        ssh.probe = state_text(shared_lock="busy")
        with mock.patch.object(f, "inspect_plugins", return_value=("changed",)):
            with self.assertRaises(RuntimeError):
                f._lock_and_revalidate(ssh, plan)
        self.assertEqual(ssh.commands[0], "mkdir /tmp/rmtool-xovi-standalone.lock")
        self.assertEqual(ssh.commands[-1], "rmdir /tmp/rmtool-xovi-standalone.lock")

    def test_stale_confirmation(self):
        ssh = FakeSSH()
        with self.assertRaises(RuntimeError):
            with f.firmware_session(ssh, object()):
                self.fail("stale device accepted")

    def test_unsafe_staging_root_aborts_before_upload(self):
        ssh = FakeSSH()
        image = f.Image(Path("x"), "3.28.0.172", "chiappa", 10, "a" * 64, 10)
        plan = f.Plan(f.parse_state(state_text()), image, (), None, ssh.token)
        with mock.patch.object(f, "preflight", return_value=plan), \
                mock.patch.object(ssh, "exec_checked", side_effect=RuntimeError("unsafe root")) as execute, \
                mock.patch.object(f, "_write_remote") as write:
            with self.assertRaisesRegex(RuntimeError, "unsafe root"):
                f.start_install(ssh, plan, confirmed=True)
            self.assertTrue(execute.call_args.args[0].startswith("set -eu; test ! -L"))
            write.assert_not_called()

    def test_guard_does_not_probe_battery_platform_or_daemon(self):
        ssh = f.FirmwareSSHClientWrapper()
        with mock.patch.object(ssh, "ensure_client", return_value=object()), mock.patch.object(f, "query_transaction", return_value=("none", "")), mock.patch.object(ssh, "exec_checked") as execute:
            ssh._before_connected()
            self.assertEqual(ssh.firmware_guard_reason, "")
            execute.assert_called_once_with(f.GUARD_PROBE)
        for word in ("battery", "engine", "machine", "pgrep"):
            self.assertNotIn(word, f.GUARD_PROBE)

    def test_guard_rejects_commands_and_sftp_during_transaction(self):
        ssh = f.FirmwareSSHClientWrapper()
        ssh.firmware_guard_reason = "running"
        with self.assertRaises(RuntimeError):
            ssh.exec_command("reboot")
        with self.assertRaises(RuntimeError):
            with ssh.sftp_session():
                self.fail("SFTP allowed")

    def test_guard_once_per_top_level_session(self):
        ssh = f.FirmwareSSHClientWrapper()
        ssh.firmware_guard_reason = ""
        with mock.patch.object(ssh, "ensure_client", return_value=object()), mock.patch.object(ssh, "_before_connected") as probe:
            with ssh.operation_session():
                ssh._firmware_gate()
                with ssh.operation_session():
                    ssh._firmware_gate()
                probe.assert_not_called()
                ssh.firmware_guard_reason = "install started"
                with self.assertRaises(RuntimeError):
                    ssh._firmware_gate()
            ssh.firmware_guard_reason = ""
            with ssh.operation_session():
                pass
            probe.assert_not_called()

    def test_firmware_queries_bypass_locked_session_only_locally(self):
        ssh = f.FirmwareSSHClientWrapper()
        ssh.firmware_guard_reason = "unknown"
        with mock.patch.object(ssh, "ensure_client", return_value=object()):
            with f.firmware_session(ssh):
                ssh._firmware_gate()
            with self.assertRaises(RuntimeError):
                ssh._firmware_gate()

    def test_disconnect_in_connect_hook_never_emits_connected(self):
        ssh = f.FirmwareSSHClientWrapper()
        client = mock.Mock()
        events = []
        ssh.connection_changed.connect(events.append)
        with mock.patch.object(ssh, "_lookup_trusted_host_key", return_value=None), \
                mock.patch.object(ssh, "_build_client", return_value=client), \
                mock.patch.object(ssh, "_connect_client"), \
                mock.patch.object(ssh, "_before_connected", side_effect=ssh.close):
            with self.assertRaises(RuntimeError):
                ssh.connect("offline-test", "unused")
        self.assertNotIn(True, events)

    def test_connect_probe_with_real_wrapper_and_fake_transport(self):
        ssh = f.FirmwareSSHClientWrapper()
        client = mock.Mock()
        client.get_transport.return_value.is_active.return_value = True
        ssh._client = client
        has_record = False

        def execute(command):
            output = (b"yes" if has_record else b"no") if command.startswith("if [ -e") else b""
            stdout = io.BytesIO(output)
            stdout.channel = mock.Mock()
            stdout.channel.recv_exit_status.return_value = 0
            return io.BytesIO(), stdout, io.BytesIO()

        client.exec_command.side_effect = execute
        ssh._before_connected()
        self.assertEqual(ssh.firmware_guard_reason, "")
        self.assertFalse(getattr(ssh._firmware_local, "allowed", False))
        self.assertTrue(any(call.args[0] == f.GUARD_PROBE for call in client.exec_command.call_args_list))
        has_record = True
        client.open_sftp.return_value.open.return_value = io.BytesIO(b"invalid-job")
        ssh._before_connected()
        self.assertTrue(ssh.firmware_guard_reason)
        with self.assertRaises(RuntimeError):
            ssh.exec_command("reboot")
        with self.assertRaises(RuntimeError):
            with ssh.sftp_session():
                self.fail("uncertain state allowed SFTP")


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeSSH()
        self.job = "a" * 32
        self.directory = f.BASE + "/" + self.job
        self.ssh.files = {f.BASE + "/current": self.job.encode(),
                          self.directory + "/job.json": json.dumps(dict(job=self.job, target="b", platform="chiappa", version="3.28.0.172", boot_id="old")).encode(),
                          self.directory + "/result": b"success",
                          self.directory + "/target": ("version=3.28.0.172\nxochitl=" + "a" * 64).encode(),
                          "/proc/sys/kernel/random/boot_id": b"old"}

    def test_success_remain_after_exit_is_not_running(self):
        self.assertEqual(f.query_transaction(self.ssh)[0], "success")

    def test_active_writer_wins_over_result(self):
        self.ssh.props = "LoadState=loaded\nActiveState=activating\nSubState=start"
        self.assertEqual(f.query_transaction(self.ssh)[0], "running")

    def test_missing_result_or_unit_same_boot_is_unknown(self):
        self.ssh.props = "LoadState=not-found\nActiveState=inactive\nSubState=dead"
        self.assertEqual(f.query_transaction(self.ssh)[0], "unknown")
        del self.ssh.files[self.directory + "/result"]
        self.assertEqual(f.query_transaction(self.ssh)[0], "unknown")

    def test_reboot_success_without_transient_unit(self):
        self.ssh.props = "LoadState=not-found\nActiveState=inactive\nSubState=dead"
        self.ssh.files["/proc/sys/kernel/random/boot_id"] = b"new"
        self.ssh.probe = state_text(root="/dev/mmcblk0p3", root_part="b", boot="2", version="3.28.0.172")
        self.assertEqual(f.query_transaction(self.ssh)[0], "completed")
        self.ssh.probe = state_text(version="3.28.0.172")
        self.assertEqual(f.query_transaction(self.ssh)[0], "unknown")

    def test_disconnect_never_means_success(self):
        with mock.patch.object(self.ssh, "exec_checked", side_effect=OSError("disconnect")):
            self.assertEqual(f.query_transaction(self.ssh)[0], "unknown")

    def test_no_reboot_on_failure_or_without_confirmation(self):
        with self.assertRaises(RuntimeError):
            f.reboot_after_success(self.ssh, self.ssh.token)
        self.ssh.files[self.directory + "/result"] = b"failed"
        with self.assertRaises(RuntimeError):
            f.reboot_after_success(self.ssh, self.ssh.token, confirmed=True)
        self.assertNotIn("systemctl reboot", self.ssh.commands)

    def test_switch_is_durable_and_explicit_reboot_rechecks_target(self):
        self.ssh.files.pop(f.BASE + "/current")
        target = dict(version="3.28.0.172", internal="20260828000000", xochitl="a" * 64, hardware="-H chiappa:1.0")
        plan = f.Plan(f.parse_state(state_text()), None, (), target, self.ssh.token)
        with mock.patch.object(f, "preflight", return_value=plan), \
                mock.patch.object(f, "_lock_and_revalidate"), \
                mock.patch.object(f.uuid, "uuid4", return_value=mock.Mock(hex=self.job)):
            self.assertEqual(f.switch_slot(self.ssh, plan, confirmed=True)[0], "running")
        self.ssh.files[f.BASE + "/current"] = self.ssh.files[f.BASE + "/current.tmp"]
        self.assertEqual(json.loads(self.ssh.files[self.directory + "/job.json"])["kind"], "switch")
        self.assertEqual(f.query_transaction(self.ssh)[0], "success")
        with self.assertRaisesRegex(RuntimeError, "下次启动分区"):
            f.reboot_after_success(self.ssh, self.ssh.token, confirmed=True)
        self.ssh.probe = state_text(boot="2")
        original = self.ssh.exec_checked

        def execute(command):
            if command == f.isolated_slot_command("b"):
                return self.ssh.files[self.directory + "/target"].decode()
            return original(command)

        with mock.patch.object(self.ssh, "exec_checked", side_effect=execute), \
                mock.patch.object(self.ssh, "close", create=True) as close:
            f.reboot_after_success(self.ssh, self.ssh.token, confirmed=True)
            close.assert_called_once()
        self.assertIn("systemctl reboot", self.ssh.commands)

    def test_pause_and_restore_preserve_service_policy(self):
        self.ssh.files.pop(f.BASE + "/current")
        self.ssh.probe = state_text(engine="active", engine_file="enabled")
        f.prepare_updater(self.ssh, self.ssh.token, confirmed=True)
        self.assertTrue(any("mask --runtime" in c for c in self.ssh.commands))
        self.assertFalse(any("disable " in c for c in self.ssh.commands))
        self.ssh.files[f.BASE + "/pause.json"] = self.ssh.files[f.BASE + "/pause.json.tmp"]
        self.ssh.probe = state_text()
        f.prepare_updater(self.ssh, self.ssh.token, confirmed=True, restore=True)
        self.assertIn("systemctl start update-engine.service", self.ssh.commands)

    def test_pause_never_stops_busy_writer(self):
        self.ssh.files.pop(f.BASE + "/current")
        self.ssh.probe = state_text(holders="busy", engine="active", engine_file="enabled")
        with self.assertRaises(RuntimeError):
            f.prepare_updater(self.ssh, self.ssh.token, confirmed=True)
        self.assertFalse(any("systemctl stop" in c for c in self.ssh.commands))

    def test_partial_pause_restores_runtime_mask(self):
        state = f.parse_state(state_text(engine="active", engine_file="enabled"))
        original = self.ssh.exec_checked

        def execute(command):
            if "mask --runtime update-engine.service" in command:
                self.ssh.probe = state_text()
                raise RuntimeError("writer appeared")
            return original(command)

        with mock.patch.object(self.ssh, "exec_checked", side_effect=execute), \
                mock.patch.object(f, "_restore_updater_locked") as restore:
            with self.assertRaisesRegex(RuntimeError, "writer appeared"):
                f._pause_updater_locked(self.ssh, state)
            restore.assert_called_once_with(self.ssh)

    def test_prepared_operation_restores_only_before_commit(self):
        state = f.parse_state(state_text(engine="active", engine_file="enabled"))
        plan = f.Plan(state, None, (), {"version": "3.28.0.172"}, self.ssh.token)
        prepared = f.Plan(f.parse_state(state_text()), None, (), plan.slot, self.ssh.token)
        with mock.patch.object(f, "preflight", side_effect=[plan, prepared]), \
                mock.patch.object(f, "_pause_updater_locked", return_value=True), \
                mock.patch.object(f, "_restore_updater_locked") as restore:
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                with f._prepared_operation(self.ssh, plan, switch=True):
                    raise RuntimeError("before commit")
            restore.assert_called_once_with(self.ssh)

        with mock.patch.object(f, "preflight", side_effect=[plan, prepared]), \
                mock.patch.object(f, "_pause_updater_locked", return_value=True), \
                mock.patch.object(f, "_restore_updater_locked") as restore:
            with self.assertRaisesRegex(RuntimeError, "after commit"):
                with f._prepared_operation(self.ssh, plan, switch=True) as (_current, committed):
                    committed["value"] = True
                    raise RuntimeError("after commit")
            restore.assert_not_called()

    def test_preflight_reports_missing_command(self):
        self.ssh.files.pop(f.BASE + "/current", None)
        self.ssh.probe = state_text(engine="active", engine_file="enabled")

        def execute(command):
            return "", "", 1 if command == "command -v mount" else 0

        with mock.patch.object(self.ssh, "exec_command", side_effect=execute), \
                mock.patch.object(f, "inspect_plugins", return_value=()), \
                self.assertRaisesRegex(RuntimeError, "mount"):
            f.preflight(self.ssh, switch=True)


if __name__ == "__main__":
    unittest.main()
