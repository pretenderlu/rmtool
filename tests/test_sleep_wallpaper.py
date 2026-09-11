import contextlib
import hashlib
import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _sleep_wallpaper as sleep


class FakeSleepSSH:
    def __init__(
        self,
        files=None,
        fail_config_once=False,
        *,
        modes=None,
        mutate_before_probe=None,
        unsafe_directory=None,
        unsafe_paths=(),
    ):
        self.files = dict(files or {})
        self.modes = {
            path: (0o644 if path == sleep.MANAGED_IMAGE_PATH else 0o600)
            for path in self.files
        }
        self.modes.update(modes or {})
        self.fail_config_once = fail_config_once
        self.mutate_before_probe = mutate_before_probe
        self.unsafe_directory = unsafe_directory
        self.unsafe_paths = set(unsafe_paths)
        self.probe_count = 0
        self.commands = []

    @contextlib.contextmanager
    def operation_session(self):
        yield

    def exec_command(self, command):
        self.commands.append(command)
        arguments = shlex.split(command)
        path = next(
            path
            for path in (
                sleep.CONFIG_PATH,
                sleep.MARKER_PATH,
                sleep.MANAGED_IMAGE_PATH,
            )
            if path in arguments
        )
        self.probe_count += 1
        if self.mutate_before_probe:
            self.mutate_before_probe(self, path, self.probe_count)
        if path in self.unsafe_paths:
            return ("unsafe\n", "", 0)
        if path not in self.files:
            return ("missing\n", "", 0)
        mode = f"{self.modes[path]:o}"
        return (f"regular:0:0:{mode}\n", "", 0)

    def open_remote(self, path, _mode="rb"):
        if path not in self.files:
            raise FileNotFoundError(path)
        return contextlib.closing(io.BytesIO(self.files[path]))

    def transfer_file(self, local, remote):
        self.files[remote] = Path(local).read_bytes()
        self.modes[remote] = 0o644

    def exec_checked(self, command):
        self.commands.append(command)
        if self.unsafe_directory and self.unsafe_directory in command:
            raise RuntimeError("unsafe directory")
        args = shlex.split(command)
        if args[0] == "sha256sum":
            data = self.files[args[1]]
            return hashlib.sha256(data).hexdigest() + "  " + args[1]
        if "mv" in args:
            if args[0] == "chmod":
                self.modes[args[2]] = int(args[1], 8)
            move = args.index("mv")
            source = args[move + 2] if args[move + 1] == "-f" else args[move + 1]
            target = args[move + 3] if args[move + 1] == "-f" else args[move + 2]
            if self.fail_config_once and target == sleep.CONFIG_PATH:
                self.fail_config_once = False
                raise RuntimeError("injected config replacement failure")
            self.files[target] = self.files.pop(source)
            self.modes[target] = self.modes.pop(source)
            return ""
        if args[:2] == ["rm", "-f"]:
            for path in args[2:]:
                self.files.pop(path, None)
                self.modes.pop(path, None)
        return ""


class SleepWallpaperTests(unittest.TestCase):
    def test_only_exact_move_and_paper_pro_172_identities_are_supported(self):
        self.assertEqual(
            {identity.platform for identity in sleep.SUPPORTED_IDENTITIES},
            {"chiappa", "ferrari"},
        )
        for identity in sleep.SUPPORTED_IDENTITIES:
            self.assertEqual(identity.firmware, "20260827113527")
            self.assertEqual(identity.architecture, "aarch64")

        move = next(
            identity for identity in sleep.SUPPORTED_IDENTITIES
            if identity.platform == "chiappa"
        )
        paper_pro = next(
            identity for identity in sleep.SUPPORTED_IDENTITIES
            if identity.platform == "ferrari"
        )
        forged = sleep.tap.DeviceIdentity(
            move.firmware, move.platform, move.architecture, paper_pro.xochitl_sha256
        )
        self.assertNotIn(forged, sleep.SUPPORTED_IDENTITIES)

    def test_both_exact_identities_complete_enable_disable_transaction(self):
        for identity in sleep.SUPPORTED_IDENTITIES:
            with self.subTest(platform=identity.platform), patch.object(
                sleep.tap, "get_device_identity", return_value=identity
            ):
                original = b"[General]\nFoo=1\n"
                ssh = FakeSleepSSH({sleep.CONFIG_PATH: original})
                self.assertTrue(sleep.get_status(ssh).supported)
                sleep.enable(ssh, b"png")
                self.assertTrue(sleep.get_status(ssh).enabled)
                sleep.disable(ssh)
                self.assertEqual(ssh.files, {sleep.CONFIG_PATH: original})

    def test_config_key_round_trip_preserves_unrelated_changes(self):
        original = b"[General]\nFoo=1\nSleepScreenPath=/custom/old.png\n[Other]\nBar=2\n"
        managed = sleep._set_sleep_line(original, sleep.MANAGED_IMAGE_PATH)
        changed = managed.replace(b"Foo=1", b"Foo=9")
        restored = sleep._set_sleep_line(
            changed, None, "SleepScreenPath=/custom/old.png"
        )
        self.assertIn(b"Foo=9", restored)
        self.assertIn(b"SleepScreenPath=/custom/old.png", restored)
        self.assertIn(b"[Other]\nBar=2", restored)

    def test_duplicate_sleep_keys_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "多个"):
            sleep._general_sleep_line(
                b"[General]\nSleepScreenPath=/one\nSleepScreenPath=/two\n"
            )

    def test_enable_requires_confirmation_for_existing_owner_then_restores_it(self):
        original = b"[General]\nFoo=1\nSleepScreenPath=/other/sleep.png\n"
        ssh = FakeSleepSSH({sleep.CONFIG_PATH: original})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            with self.assertRaisesRegex(RuntimeError, "确认"):
                sleep.enable(ssh, b"png")
            self.assertEqual(ssh.files, {sleep.CONFIG_PATH: original})
            sleep.enable(ssh, b"png", take_over=True)
            status = sleep.get_status(ssh)
            self.assertTrue(status.enabled)
            sleep.disable(ssh)
        self.assertEqual(ssh.files, {sleep.CONFIG_PATH: original})

    def test_new_key_is_removed_on_disable(self):
        original = b"[General]\nFoo=1\n"
        ssh = FakeSleepSSH({sleep.CONFIG_PATH: original})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            sleep.enable(ssh, b"png")
            sleep.disable(ssh)
        self.assertEqual(ssh.files, {sleep.CONFIG_PATH: original})

    def test_failed_config_replace_restores_all_original_bytes(self):
        original = b"[General]\nFoo=1\n"
        ssh = FakeSleepSSH(
            {sleep.CONFIG_PATH: original},
            fail_config_once=True,
        )
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                sleep.enable(ssh, b"new-image")
        self.assertEqual(
            ssh.files,
            {sleep.CONFIG_PATH: original},
        )

    def test_disable_restores_original_config_mode(self):
        original = b"[General]\nSleepScreenPath=/custom/original.png\n"
        ssh = FakeSleepSSH({sleep.CONFIG_PATH: original}, modes={sleep.CONFIG_PATH: 0o644})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            sleep.enable(ssh, b"png", take_over=True)
            self.assertEqual(ssh.modes[sleep.CONFIG_PATH], 0o600)
            sleep.disable(ssh)
        self.assertEqual(ssh.files, {sleep.CONFIG_PATH: original})
        self.assertEqual(ssh.modes[sleep.CONFIG_PATH], 0o644)

    def test_orphan_managed_image_fails_closed(self):
        ssh = FakeSleepSSH({sleep.MANAGED_IMAGE_PATH: b"unknown"})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            status = sleep.get_status(ssh)
            with self.assertRaisesRegex(RuntimeError, "未记录来源"):
                sleep.enable(ssh, b"png")
        self.assertTrue(status.broken)
        self.assertEqual(ssh.files[sleep.MANAGED_IMAGE_PATH], b"unknown")

    def test_dangling_symlink_state_fails_closed(self):
        ssh = FakeSleepSSH(unsafe_paths=(sleep.MANAGED_IMAGE_PATH,))
        with patch.object(
            sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY
        ):
            with self.assertRaisesRegex(RuntimeError, "状态无法确认"):
                sleep.enable(ssh, b"png")
        self.assertFalse(any("sha256sum" in command for command in ssh.commands))

    def test_modified_managed_image_blocks_disable(self):
        ssh = FakeSleepSSH({sleep.CONFIG_PATH: b"[General]\n"})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            sleep.enable(ssh, b"original")
            ssh.files[sleep.MANAGED_IMAGE_PATH] = b"external"
            status = sleep.get_status(ssh)
            with self.assertRaisesRegex(RuntimeError, "不一致"):
                sleep.disable(ssh)
        self.assertTrue(status.broken)
        self.assertEqual(ssh.files[sleep.MANAGED_IMAGE_PATH], b"external")

    def test_concurrent_config_change_is_preserved_and_other_files_roll_back(self):
        original = b"[General]\nFoo=1\n"

        def mutate(ssh, path, probe_count):
            if path == sleep.CONFIG_PATH and probe_count > 3 and ssh.files[path] == original:
                ssh.files[path] = b"[General]\nFoo=external\n"

        ssh = FakeSleepSSH(
            {sleep.CONFIG_PATH: original}, mutate_before_probe=mutate
        )
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            with self.assertRaisesRegex(RuntimeError, "自动回滚未完成"):
                sleep.enable(ssh, b"png")
        self.assertEqual(ssh.files, {sleep.CONFIG_PATH: b"[General]\nFoo=external\n"})

    def test_malformed_previous_marker_is_rejected(self):
        marker = {
            "schema_version": 2,
            "managed_path": sleep.MANAGED_IMAGE_PATH,
            "image_sha256": hashlib.sha256(b"png").hexdigest(),
            "previous": {
                "key_present": True,
                "line": "NotSleepScreenPath=/tmp/a",
                "config_present": True,
                "config_mode": 0o600,
            },
        }
        with self.assertRaisesRegex(RuntimeError, "格式无效"):
            sleep._parse_marker((json.dumps(marker) + "\n").encode())

    def test_unsupported_identity_never_writes(self):
        ssh = FakeSleepSSH()
        with patch.object(sleep.tap, "get_device_identity", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "尚未完成"):
                sleep.enable(ssh, b"png")
        self.assertEqual(ssh.files, {})

    def test_unsafe_parent_directory_fails_before_transfer(self):
        original = b"[General]\n"
        ssh = FakeSleepSSH(
            {sleep.CONFIG_PATH: original}, unsafe_directory="/home/root/.local"
        )
        with patch.object(
            sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY
        ):
            with self.assertRaisesRegex(RuntimeError, "目录状态不安全"):
                sleep.enable(ssh, b"png")
        self.assertEqual(ssh.files, {sleep.CONFIG_PATH: original})
        self.assertFalse(any("reboot" in command for command in ssh.commands))
        self.assertFalse(any("restart" in command for command in ssh.commands))

    def test_enable_and_disable_never_restart_device(self):
        ssh = FakeSleepSSH({sleep.CONFIG_PATH: b"[General]\n"})
        with patch.object(
            sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY
        ):
            sleep.enable(ssh, b"png")
            sleep.disable(ssh)
        commands = "\n".join(ssh.commands)
        self.assertNotIn("reboot", commands)
        self.assertNotIn("restart", commands)


if __name__ == "__main__":
    unittest.main()
