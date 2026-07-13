import shlex
import tempfile
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import _rmkit_cn


class FakeSFTP:
    def __init__(self, files):
        self.files = files

    def stat(self, path):
        if path not in self.files:
            raise IOError(path)
        return object()

    def open(self, path, _mode="rb"):
        if path not in self.files:
            raise IOError(path)
        return BytesIO(self.files[path])


class FakeSSH:
    def __init__(self, files=None, *, firmware="20260629074044", fail_transfer_at=None):
        self.files = dict(files or {})
        self.firmware = firmware
        self.fail_transfer_at = fail_transfer_at
        self.transfer_count = 0
        self.close_count = 0
        self.events = []
        self.xochitl_active = True
        self.stop_state = "inactive"

    @contextmanager
    def sftp_session(self):
        yield FakeSFTP(self.files)

    def exec_checked(self, command):
        self.events.append(("exec", command))
        if command == "cat /etc/version":
            return f"{self.firmware}\n"
        if command == "systemctl is-active xochitl":
            return "active\n" if self.xochitl_active else "inactive\n"
        if command == "systemctl stop xochitl":
            self.xochitl_active = self.stop_state != "inactive"
            return ""
        if command == "systemctl show xochitl -p ActiveState --value":
            return "active\n" if self.xochitl_active else "inactive\n"
        if command in ("mount -o remount,rw /", "mount -o remount,ro /"):
            return ""

        args = shlex.split(command)
        if args[:2] == ["mkdir", "-p"]:
            return ""
        if args[:2] == ["cp", "-p"]:
            self.files[args[3]] = self.files[args[2]]
            return ""
        if args[:2] == ["mv", "-f"]:
            self.files[args[3]] = self.files.pop(args[2])
            return ""
        if args and args[0] == "touch":
            self.files[args[1]] = b""
            return ""
        if args[:2] == ["rm", "-f"]:
            for path in args[2:]:
                self.files.pop(path, None)
            return ""
        if args and args[0] == "chmod":
            return ""
        raise AssertionError(f"unexpected command: {command}")

    def transfer_file(self, local_path, remote_path):
        self.transfer_count += 1
        self.events.append(("transfer", remote_path))
        data = Path(local_path).read_bytes()
        if self.transfer_count == self.fail_transfer_at:
            self.files[remote_path] = data[:1]
            raise IOError("simulated upload failure")
        self.files[remote_path] = data

    def close(self):
        self.close_count += 1
        self.events.append(("close", ""))


class RmkitCnLocalizationTests(unittest.TestCase):
    def setUp(self):
        required_api = (
            "LocalizationState",
            "CARRIER_LANGUAGE",
            "CONFIG_PATH",
            "QM_PATH",
            "BACKUP_CONFIG_PATH",
            "BACKUP_QM_PATH",
            "BACKUP_READY_PATH",
            "set_language_config",
            "get_localization_status",
            "enable_localization",
            "restore_localization",
        )
        self.assertEqual(
            [name for name in required_api if not hasattr(_rmkit_cn, name)],
            [],
            "localization backend API is not implemented",
        )

    def make_ssh(self, config=b"[General]\n"):
        return FakeSSH({_rmkit_cn.CONFIG_PATH: config})

    def make_qm(self, data=b"new-qm"):
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".qm")
        temp.write(data)
        temp.close()
        self.addCleanup(Path(temp.name).unlink, missing_ok=True)
        return temp.name

    def test_status_distinguishes_all_four_states(self):
        self.assertEqual(_rmkit_cn.CARRIER_LANGUAGE, "fr_FR")
        cases = (
            ("other", {}, _rmkit_cn.LocalizationState.INCOMPATIBLE),
            ("20260629074044", {}, _rmkit_cn.LocalizationState.NOT_INSTALLED),
            (
                "20260629074044",
                {_rmkit_cn.QM_PATH: b"stock-carrier-qm"},
                _rmkit_cn.LocalizationState.NOT_INSTALLED,
            ),
            (
                "20260629074044",
                {
                    _rmkit_cn.QM_PATH: b"qm",
                    _rmkit_cn.BACKUP_READY_PATH: b"",
                },
                _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED,
            ),
            (
                "20260629074044",
                {
                    _rmkit_cn.CONFIG_PATH: (
                        f"[General]\nlanguage={_rmkit_cn.CARRIER_LANGUAGE}\n"
                    ).encode(),
                    _rmkit_cn.QM_PATH: b"qm",
                    _rmkit_cn.BACKUP_READY_PATH: b"",
                },
                _rmkit_cn.LocalizationState.ENABLED,
            ),
        )
        for firmware, extra_files, expected in cases:
            with self.subTest(expected=expected):
                ssh = self.make_ssh()
                ssh.firmware = firmware
                ssh.files.update(extra_files)
                self.assertEqual(_rmkit_cn.get_localization_status(ssh).state, expected)

    def test_language_config_only_changes_general_key(self):
        original = (
            "# keep exactly\r\n[General]\r\nfoo = bar\r\n\r\n"
            "[Wifi]\r\nlanguage=do-not-touch\r\nssid=x\r\n"
        )

        localized = _rmkit_cn.set_language_config(
            original, _rmkit_cn.CARRIER_LANGUAGE
        )

        carrier_line = f"language={_rmkit_cn.CARRIER_LANGUAGE}"
        self.assertEqual(localized.count(carrier_line), 1)
        self.assertEqual(localized.replace(f"{carrier_line}\r\n", ""), original)
        self.assertEqual(
            _rmkit_cn.set_language_config(localized, None),
            original,
        )

    def test_enable_preserves_config_backs_up_then_installs_atomically(self):
        original = (
            b"# keep exactly\r\n[General]\r\nfoo = bar\r\n\r\n"
            b"[Wifi]\r\nlanguage=do-not-touch\r\nssid=x\r\n"
        )
        ssh = self.make_ssh(original)

        result = _rmkit_cn.enable_localization(ssh, self.make_qm())

        self.assertEqual(result.state, _rmkit_cn.LocalizationState.ENABLED)
        self.assertEqual(ssh.files[_rmkit_cn.BACKUP_CONFIG_PATH], original)
        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], b"new-qm")
        self.assertIn(_rmkit_cn.BACKUP_READY_PATH, ssh.files)
        localized = ssh.files[_rmkit_cn.CONFIG_PATH]
        carrier_line = f"language={_rmkit_cn.CARRIER_LANGUAGE}".encode()
        self.assertEqual(localized.count(carrier_line), 1)
        self.assertEqual(localized.replace(carrier_line + b"\r\n", b""), original)

        backup_index = ssh.events.index(
            ("exec", f"cp -p {_rmkit_cn.CONFIG_PATH} {_rmkit_cn.BACKUP_CONFIG_PATH}.tmp")
        )
        stop_index = ssh.events.index(("exec", "systemctl stop xochitl"))
        qm_upload_index = ssh.events.index(("transfer", f"{_rmkit_cn.QM_PATH}.tmp"))
        config_upload_index = ssh.events.index(("transfer", f"{_rmkit_cn.CONFIG_PATH}.tmp"))
        self.assertLess(stop_index, backup_index)
        self.assertLess(backup_index, qm_upload_index)
        self.assertLess(qm_upload_index, config_upload_index)
        self.assertEqual(ssh.close_count, 1)
        commands = "\n".join(value for kind, value in ssh.events if kind == "exec")
        self.assertIn("mount -o remount,rw /", commands)
        self.assertIn("mount -o remount,ro /", commands)
        self.assertNotIn("restart", commands)
        self.assertNotIn("reboot", commands)
        self.assertNotIn("systemctl start xochitl", commands)
        self.assertNotIn("xovi", commands.lower())
        self.assertNotIn(".qmd", commands.lower())
        self.assertNotIn("systemd", commands.lower())

    def test_enable_is_idempotent_and_restore_removes_new_qm(self):
        original = b"[General]\nfoo=bar\n"
        ssh = self.make_ssh(original)
        qm_path = self.make_qm()
        _rmkit_cn.enable_localization(ssh, qm_path)
        transfers_after_first_enable = ssh.transfer_count
        backup = ssh.files[_rmkit_cn.BACKUP_CONFIG_PATH]

        _rmkit_cn.enable_localization(ssh, qm_path)

        self.assertEqual(ssh.transfer_count, transfers_after_first_enable)
        self.assertEqual(ssh.files[_rmkit_cn.BACKUP_CONFIG_PATH], backup)

        runtime_update = b"RuntimeCounter=2\n"
        ssh.files[_rmkit_cn.CONFIG_PATH] += runtime_update
        result = _rmkit_cn.restore_localization(ssh)
        self.assertEqual(result.state, _rmkit_cn.LocalizationState.NOT_INSTALLED)
        self.assertFalse(ssh.xochitl_active)
        self.assertEqual(
            ssh.files[_rmkit_cn.CONFIG_PATH], original + runtime_update
        )
        self.assertNotIn(_rmkit_cn.QM_PATH, ssh.files)
        self.assertNotIn(_rmkit_cn.BACKUP_READY_PATH, ssh.files)

        snapshot = dict(ssh.files)
        _rmkit_cn.restore_localization(ssh)
        self.assertEqual(ssh.files, snapshot)

    def test_restore_reinstates_preexisting_language_and_qm(self):
        original_config = b"[General]\nlanguage=de_DE\nfoo=bar\n"
        ssh = FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: original_config,
                _rmkit_cn.QM_PATH: b"old-qm",
            }
        )

        _rmkit_cn.enable_localization(ssh, self.make_qm())
        self.assertEqual(ssh.files[_rmkit_cn.BACKUP_QM_PATH], b"old-qm")
        _rmkit_cn.restore_localization(ssh)

        self.assertEqual(ssh.files[_rmkit_cn.CONFIG_PATH], original_config)
        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], b"old-qm")

    def test_upload_failures_roll_back_config_and_qm(self):
        for failure_number in (1, 2):
            with self.subTest(failure_number=failure_number):
                original = b"[General]\nlanguage=de_DE\n"
                ssh = FakeSSH(
                    {
                        _rmkit_cn.CONFIG_PATH: original,
                        _rmkit_cn.QM_PATH: b"old-qm",
                    },
                    fail_transfer_at=failure_number,
                )
                with self.assertRaisesRegex(IOError, "simulated upload failure"):
                    _rmkit_cn.enable_localization(ssh, self.make_qm())
                self.assertEqual(ssh.files[_rmkit_cn.CONFIG_PATH], original)
                self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], b"old-qm")
                self.assertNotIn(_rmkit_cn.BACKUP_READY_PATH, ssh.files)
                self.assertNotIn(f"{_rmkit_cn.QM_PATH}.tmp", ssh.files)
                self.assertNotIn(f"{_rmkit_cn.CONFIG_PATH}.tmp", ssh.files)

    def test_incompatible_firmware_blocks_mutation(self):
        ssh = FakeSSH(
            {_rmkit_cn.CONFIG_PATH: b"[General]\n"},
            firmware="20260629074045",
        )
        before = dict(ssh.files)

        with self.assertRaisesRegex(RuntimeError, "20260629074044"):
            _rmkit_cn.enable_localization(ssh, self.make_qm())
        with self.assertRaisesRegex(RuntimeError, "20260629074044"):
            _rmkit_cn.restore_localization(ssh)

        self.assertEqual(ssh.files, before)
        self.assertFalse(any(kind == "transfer" for kind, _value in ssh.events))

    def test_active_xochitl_after_stop_blocks_all_writes(self):
        ssh = self.make_ssh()
        ssh.stop_state = "active"
        before = dict(ssh.files)

        with self.assertRaisesRegex(RuntimeError, "xochitl"):
            _rmkit_cn.enable_localization(ssh, self.make_qm())

        self.assertEqual(ssh.files, before)
        self.assertFalse(any(kind == "transfer" for kind, _value in ssh.events))


if __name__ == "__main__":
    unittest.main()
