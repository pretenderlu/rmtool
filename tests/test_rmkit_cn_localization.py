import hashlib
import json
import shlex
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _rmkit_cn


class FakeSFTP:
    def __init__(self, files):
        self.files = files

    def stat(self, path):
        if path not in self.files:
            raise IOError(path)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)

    def lstat(self, path):
        return self.stat(path)

    def open(self, path, _mode="rb"):
        if path not in self.files:
            raise IOError(path)
        return BytesIO(self.files[path])

    def listdir_attr(self, remote_dir):
        prefix = remote_dir.rstrip("/") + "/"
        return [
            SimpleNamespace(filename=path[len(prefix):], st_mode=stat.S_IFREG | 0o644)
            for path in sorted(self.files)
            if path.startswith(prefix) and "/" not in path[len(prefix):]
        ]

    def rename(self, source, target):
        if source not in self.files:
            raise IOError(source)
        if target in self.files:
            raise IOError(target)
        self.files[target] = self.files.pop(source)

    def remove(self, path):
        if path not in self.files:
            raise IOError(path)
        del self.files[path]


class FakeSSH:
    def __init__(
        self,
        files=None,
        *,
        firmware="20260612085811",
        fail_transfer_at=None,
        corrupt_transfer_at=None,
        cjk_available=True,
        cjk_font_data=(),
        active_font=None,
        cjk_files=(),
        fail_exec_commands=(),
        device_font_family="Device Font Family",
        font_match_paths=None,
        fail_cache_count=0,
    ):
        self.files = dict(files or {})
        self.firmware = firmware
        self.fail_transfer_at = fail_transfer_at
        self.corrupt_transfer_at = corrupt_transfer_at
        self.cjk_available = cjk_available
        self.cjk_font_data = set(cjk_font_data)
        self.active_font = active_font or (
            "/usr/share/fonts/active-cjk.otf"
            if cjk_available
            else "/usr/share/fonts/latin-only.otf"
        )
        self.cjk_files = set(cjk_files)
        self.fail_exec_commands = set(fail_exec_commands)
        self.device_font_family = device_font_family
        self.font_match_paths = dict(font_match_paths or {})
        self.fail_cache_count = fail_cache_count
        if cjk_available:
            self.cjk_files.add(self.active_font)
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
        if command in self.fail_exec_commands:
            raise IOError(f"simulated command failure: {command}")
        if command == "cat /etc/version":
            return f"{self.firmware}\n"
        if command == "systemctl is-active xochitl":
            return "active\n" if self.xochitl_active else "inactive\n"
        if command == "systemctl stop xochitl":
            self.xochitl_active = self.stop_state != "inactive"
            return ""
        if command == "systemctl show xochitl -p ActiveState --value":
            return "active\n" if self.xochitl_active else "inactive\n"
        if command == _rmkit_cn.PRIMARY_FONT_COMMAND:
            managed = next(
                (
                    path
                    for path in _rmkit_cn.MANAGED_FONT_PATHS
                    if path in self.files
                    and _rmkit_cn.FONTCONFIG_FILE in self.files
                ),
                None,
            )
            return f"{managed or self.active_font}\n"
        if command == _rmkit_cn.CJK_FONT_LIST_COMMAND:
            files = set(self.cjk_files)
            files.update(
                path
                for path in _rmkit_cn.MANAGED_FONT_PATHS
                if self.files.get(path) in self.cjk_font_data
            )
            return "".join(f"{path}\n" for path in sorted(files))
        if command.startswith("fc-scan "):
            scanned_path = shlex.split(command)[1]
            if scanned_path not in self.files:
                raise IOError(scanned_path)
            return f'family: "{self.device_font_family}"(s)\n'
        if command.startswith("fc-match --format='%{file}\\n' "):
            pattern = shlex.split(command)[2]
            matched = self.font_match_paths.get(pattern)
            if matched is None and _rmkit_cn.FONTCONFIG_FILE in self.files:
                config = ET.fromstring(
                    self.files[_rmkit_cn.FONTCONFIG_FILE].decode("utf-8")
                )
                scan_path = config.find(
                    "./match[@target='scan']/test[@name='file']/string"
                )
                if scan_path is not None:
                    matched = scan_path.text
            return f"{matched or self.active_font}\n"
        if command.startswith("fc-cache -f -v "):
            if self.fail_cache_count:
                self.fail_cache_count -= 1
                raise IOError("simulated cache failure")
            return "cache refreshed\n"
        if command in ("mount -o remount,rw /", "mount -o remount,ro /"):
            return ""
        if command == "sync":
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
        if self.transfer_count == self.corrupt_transfer_at:
            data += b"corrupt"
        self.files[remote_path] = data

    def close(self):
        self.close_count += 1
        self.events.append(("close", ""))


class RmkitCnLocalizationTests(unittest.TestCase):
    STOCK_QM = b"stock-carrier-qm"
    LOCALIZED_QM = b"localized-qm"

    def setUp(self):
        required_api = (
            "LocalizationState",
            "CARRIER_LANGUAGE",
            "CONFIG_PATH",
            "QM_PATH",
            "BACKUP_CONFIG_PATH",
            "BACKUP_QM_PATH",
            "BACKUP_READY_PATH",
            "STOCK_FRENCH_QM_SHA256",
            "LOCALIZED_QM_SHA256",
            "BUNDLED_FONT_SHA256",
            "FONT_MARKER_PATH",
            "has_cjk_font",
            "install_user_font_override",
            "list_user_fonts",
            "upload_user_font",
            "set_active_user_font",
            "delete_user_font",
            "upload_font",
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
        self.assertEqual(_rmkit_cn.SUPPORTED_FIRMWARE, "20260612085811")
        self.assertEqual(
            _rmkit_cn.STOCK_FRENCH_QM_SHA256,
            "8e0db0f7a2d3116469e1aae4f52657ccc38d0422b5b958ae512554bd018f285e",
        )
        self.assertEqual(
            _rmkit_cn.LOCALIZED_QM_SHA256,
            "47ba9d8a6f38b3763d013ecc489d44e8742704404b50a5de102b42e33dfebbfb",
        )
        self.assertEqual(
            _rmkit_cn.BUNDLED_FONT_SHA256,
            "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
        )
        for name, data in (
            ("STOCK_FRENCH_QM_SHA256", self.STOCK_QM),
            ("LOCALIZED_QM_SHA256", self.LOCALIZED_QM),
        ):
            mocked = patch.object(_rmkit_cn, name, hashlib.sha256(data).hexdigest())
            mocked.start()
            self.addCleanup(mocked.stop)

    def make_ssh(self, config=b"[General]\n", **kwargs):
        return FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: config,
                _rmkit_cn.QM_PATH: self.STOCK_QM,
            },
            **kwargs,
        )

    def make_qm(self, data=None):
        data = self.LOCALIZED_QM if data is None else data
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".qm")
        temp.write(data)
        temp.close()
        self.addCleanup(Path(temp.name).unlink, missing_ok=True)
        return temp.name

    def make_font(self, data=b"cjk-font", name="custom.ttf"):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / name
        path.write_bytes(data)
        return str(path)

    def make_translation_package(self):
        return _rmkit_cn.TranslationPackage(
            firmware=_rmkit_cn.SUPPORTED_FIRMWARE,
            stock_french_sha256=hashlib.sha256(self.STOCK_QM).hexdigest(),
            localized_qm_sha256=hashlib.sha256(self.LOCALIZED_QM).hexdigest(),
            asset=f"reMarkable_zh_CN-{_rmkit_cn.SUPPORTED_FIRMWARE}.qm",
            size=len(self.LOCALIZED_QM),
            release_version="3.27.3.0",
            channel="stable",
        )

    def make_variant_packages(self):
        ferrari_stock = b"ferrari-stock-carrier-qm"
        tatsu_stock = b"tatsu-stock-carrier-qm"
        common = {
            "firmware": _rmkit_cn.SUPPORTED_FIRMWARE,
            "localized_qm_sha256": hashlib.sha256(self.LOCALIZED_QM).hexdigest(),
            "asset": f"reMarkable_zh_CN-{_rmkit_cn.SUPPORTED_FIRMWARE}.qm",
            "size": len(self.LOCALIZED_QM),
            "release_version": "3.27.3.0",
            "channel": "stable",
        }
        ferrari = _rmkit_cn.TranslationPackage(
            stock_french_sha256=hashlib.sha256(ferrari_stock).hexdigest(),
            platform="ferrari",
            **common,
        )
        tatsu = _rmkit_cn.TranslationPackage(
            stock_french_sha256=hashlib.sha256(tatsu_stock).hexdigest(),
            platform="tatsu",
            **common,
        )
        chiappa = _rmkit_cn.TranslationPackage(
            stock_french_sha256=hashlib.sha256(self.STOCK_QM).hexdigest(),
            platform="chiappa",
            variants=(ferrari, tatsu),
            **common,
        )
        return chiappa, ferrari, ferrari_stock, tatsu, tatsu_stock

    def managed_files(self, carrier=None):
        return {
            _rmkit_cn.QM_PATH: self.LOCALIZED_QM if carrier is None else carrier,
            _rmkit_cn.BACKUP_CONFIG_PATH: b"[General]\n",
            _rmkit_cn.BACKUP_QM_PATH: self.STOCK_QM,
            _rmkit_cn.BACKUP_READY_PATH: b"",
        }

    def test_status_distinguishes_all_four_states(self):
        self.assertEqual(_rmkit_cn.CARRIER_LANGUAGE, "fr_FR")
        cases = (
            ("other", {}, _rmkit_cn.LocalizationState.INCOMPATIBLE),
            ("20260612085811", {}, _rmkit_cn.LocalizationState.NOT_INSTALLED),
            (
                "20260612085811",
                self.managed_files(),
                _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED,
            ),
            (
                "20260612085811",
                {
                    _rmkit_cn.CONFIG_PATH: (
                        f"[General]\nlanguage={_rmkit_cn.CARRIER_LANGUAGE}\n"
                    ).encode(),
                    **self.managed_files(),
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
        self.assertEqual(ssh.files[_rmkit_cn.BACKUP_QM_PATH], self.STOCK_QM)
        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], self.LOCALIZED_QM)
        self.assertIn(_rmkit_cn.BACKUP_READY_PATH, ssh.files)
        localized = ssh.files[_rmkit_cn.CONFIG_PATH]
        carrier_line = f"language={_rmkit_cn.CARRIER_LANGUAGE}".encode()
        self.assertEqual(localized.count(carrier_line), 1)
        self.assertEqual(localized.replace(carrier_line + b"\r\n", b""), original)

        backup_index = ssh.events.index(
            ("exec", f"cp -p {_rmkit_cn.CONFIG_PATH} {_rmkit_cn.BACKUP_CONFIG_PATH}.tmp")
        )
        ready_index = ssh.events.index(
            ("exec", f"touch {_rmkit_cn.BACKUP_READY_PATH}")
        )
        stop_index = ssh.events.index(("exec", "systemctl stop xochitl"))
        qm_upload_index = ssh.events.index(("transfer", f"{_rmkit_cn.QM_PATH}.tmp"))
        config_upload_index = ssh.events.index(("transfer", f"{_rmkit_cn.CONFIG_PATH}.tmp"))
        self.assertLess(stop_index, backup_index)
        self.assertLess(backup_index, qm_upload_index)
        self.assertLess(ready_index, qm_upload_index)
        self.assertLess(qm_upload_index, config_upload_index)
        # Writes must hit disk before the ready marker and before the
        # operation reports success (hard power-off zeroed unsynced
        # backups on real hardware).
        sync_index = ssh.events.index(("exec", "sync"))
        self.assertLess(sync_index, ready_index)
        exec_events = [value for kind, value in ssh.events if kind == "exec"]
        self.assertEqual(exec_events[-1], "sync")
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

    def test_enable_is_idempotent_and_restore_reinstates_stock_qm(self):
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
        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], self.STOCK_QM)
        self.assertNotIn(_rmkit_cn.BACKUP_READY_PATH, ssh.files)

        snapshot = dict(ssh.files)
        _rmkit_cn.restore_localization(ssh)
        self.assertEqual(ssh.files, snapshot)

    def test_restore_reinstates_preexisting_language_and_qm(self):
        original_config = b"[General]\nlanguage=de_DE\nfoo=bar\n"
        ssh = FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: original_config,
                _rmkit_cn.QM_PATH: self.STOCK_QM,
            }
        )

        _rmkit_cn.enable_localization(ssh, self.make_qm())
        self.assertEqual(ssh.files[_rmkit_cn.BACKUP_QM_PATH], self.STOCK_QM)
        _rmkit_cn.restore_localization(ssh)

        self.assertEqual(ssh.files[_rmkit_cn.CONFIG_PATH], original_config)
        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], self.STOCK_QM)

    def test_upload_failures_roll_back_config_and_qm(self):
        for failure_number in (1, 2):
            with self.subTest(failure_number=failure_number):
                original = b"[General]\nlanguage=de_DE\n"
                ssh = FakeSSH(
                    {
                        _rmkit_cn.CONFIG_PATH: original,
                        _rmkit_cn.QM_PATH: self.STOCK_QM,
                    },
                    fail_transfer_at=failure_number,
                )
                with self.assertRaisesRegex(IOError, "simulated upload failure"):
                    _rmkit_cn.enable_localization(ssh, self.make_qm())
                self.assertEqual(ssh.files[_rmkit_cn.CONFIG_PATH], original)
                self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], self.STOCK_QM)
                self.assertNotIn(_rmkit_cn.BACKUP_READY_PATH, ssh.files)
                self.assertNotIn(f"{_rmkit_cn.QM_PATH}.tmp", ssh.files)
                self.assertNotIn(f"{_rmkit_cn.CONFIG_PATH}.tmp", ssh.files)

    def test_retry_failure_preserves_existing_backup_marker(self):
        ssh = FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: b"[General]\nlanguage=de_DE\n",
                **self.managed_files(),
            },
            fail_transfer_at=1,
        )

        with self.assertRaisesRegex(IOError, "simulated upload failure"):
            _rmkit_cn.enable_localization(ssh, self.make_qm())

        self.assertIn(_rmkit_cn.BACKUP_READY_PATH, ssh.files)
        self.assertEqual(ssh.files[_rmkit_cn.BACKUP_QM_PATH], self.STOCK_QM)
        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], self.STOCK_QM)

    def test_incompatible_firmware_blocks_mutation(self):
        ssh = FakeSSH(
            {_rmkit_cn.CONFIG_PATH: b"[General]\n"},
            firmware="20260612085812",
        )
        before = dict(ssh.files)

        with self.assertRaisesRegex(RuntimeError, "20260612085811"):
            _rmkit_cn.enable_localization(ssh, self.make_qm())
        with self.assertRaisesRegex(RuntimeError, "20260612085811"):
            _rmkit_cn.restore_localization(ssh)

        self.assertEqual(ssh.files, before)
        self.assertFalse(any(kind == "transfer" for kind, _value in ssh.events))

    def test_status_probe_is_read_only_and_rejects_unmanaged_localized_qm(self):
        ssh = self.make_ssh()
        before = dict(ssh.files)

        self.assertEqual(
            _rmkit_cn.get_localization_status(ssh).state,
            _rmkit_cn.LocalizationState.NOT_INSTALLED,
        )
        self.assertEqual(ssh.files, before)
        self.assertFalse(any(kind == "transfer" for kind, _value in ssh.events))
        commands = [value for kind, value in ssh.events if kind == "exec"]
        self.assertEqual(
            commands,
            [
                "cat /etc/version",
                _rmkit_cn.PRIMARY_FONT_COMMAND,
                _rmkit_cn.CJK_FONT_LIST_COMMAND,
            ],
        )

        ssh.files[_rmkit_cn.QM_PATH] = self.LOCALIZED_QM
        with self.assertRaisesRegex(RuntimeError, "缺少可还原的备份"):
            _rmkit_cn.get_localization_status(ssh)

    def test_missing_font_without_selection_performs_no_writes(self):
        ssh = self.make_ssh(cjk_available=False)
        before = dict(ssh.files)

        with self.assertRaisesRegex(RuntimeError, "未选择"):
            _rmkit_cn.enable_localization(ssh, self.make_qm())

        self.assertEqual(ssh.files, before)
        self.assertFalse(any(kind == "transfer" for kind, _value in ssh.events))
        self.assertNotIn(("exec", "systemctl stop xochitl"), ssh.events)
        self.assertFalse(
            any(
                kind == "exec"
                and value.startswith(
                    ("mkdir ", "cp ", "mv ", "rm ", "touch ", "chmod ", "fc-cache ", "mount ", "systemctl ")
                )
                for kind, value in ssh.events
            )
        )
        self.assertEqual(ssh.close_count, 1)

    def test_detection_checks_active_sans_serif_not_any_cjk_font(self):
        other_cjk = "/home/root/.local/share/fonts/other-cjk.otf"
        ssh = self.make_ssh(
            cjk_available=False,
            active_font="/usr/share/fonts/latin-ui.otf",
            cjk_files=(other_cjk,),
        )
        before = dict(ssh.files)

        status = _rmkit_cn.get_localization_status(ssh)
        self.assertFalse(status.has_cjk_font)
        ssh.active_font = other_cjk
        self.assertTrue(_rmkit_cn.get_localization_status(ssh).has_cjk_font)
        self.assertEqual(ssh.files, before)
        self.assertFalse(any(kind == "transfer" for kind, _value in ssh.events))

    def test_user_owned_active_cjk_font_is_preserved_without_font_writes(self):
        user_font = "/home/root/.local/share/fonts/user-ui.ttf"
        user_config = b"<fontconfig>user-owned</fontconfig>\n"
        ssh = self.make_ssh(
            active_font=user_font,
            cjk_files=(user_font,),
            cjk_available=False,
        )
        ssh.files[_rmkit_cn.FONTCONFIG_FILE] = user_config

        _rmkit_cn.enable_localization(ssh, self.make_qm())

        self.assertEqual(ssh.files[_rmkit_cn.FONTCONFIG_FILE], user_config)
        self.assertNotIn(_rmkit_cn.FONT_MARKER_PATH, ssh.files)
        self.assertNotIn(_rmkit_cn.FONTCONFIG_BACKUP_PATH, ssh.files)
        self.assertFalse(
            any(
                kind == "transfer" and value.startswith(_rmkit_cn.FONT_DIR)
                for kind, value in ssh.events
            )
        )

    def test_restore_recovers_preexisting_fontconfig_byte_for_byte(self):
        font_data = b"managed-full-ui-font"
        original_fontconfig = b"\x00<fontconfig>exact user bytes</fontconfig>\r\n"
        ssh = self.make_ssh(cjk_available=False, cjk_font_data=(font_data,))
        ssh.files[_rmkit_cn.FONTCONFIG_FILE] = original_fontconfig

        _rmkit_cn.enable_localization(
            ssh,
            self.make_qm(),
            self.make_font(font_data, "managed.otf"),
            "Managed Full UI",
        )
        self.assertEqual(
            ssh.files[_rmkit_cn.FONTCONFIG_BACKUP_PATH], original_fontconfig
        )

        _rmkit_cn.restore_localization(ssh)

        self.assertEqual(ssh.files[_rmkit_cn.FONTCONFIG_FILE], original_fontconfig)
        self.assertNotIn(_rmkit_cn.FONTCONFIG_BACKUP_PATH, ssh.files)
        self.assertNotIn(_rmkit_cn.FONT_MARKER_PATH, ssh.files)

    def test_restore_removes_generated_fontconfig_when_none_existed(self):
        font_data = b"managed-ui-font-without-prior-config"
        ssh = self.make_ssh(cjk_available=False, cjk_font_data=(font_data,))

        _rmkit_cn.enable_localization(
            ssh,
            self.make_qm(),
            self.make_font(font_data),
            "Managed UI Font",
        )
        self.assertIn(_rmkit_cn.FONTCONFIG_FILE, ssh.files)

        _rmkit_cn.restore_localization(ssh)

        self.assertNotIn(_rmkit_cn.FONTCONFIG_FILE, ssh.files)
        self.assertNotIn(_rmkit_cn.FONT_MARKER_PATH, ssh.files)

    def test_selected_cjk_font_is_verified_before_translation(self):
        font_data = b"user-selected-cjk-font"
        ssh = self.make_ssh(cjk_available=False)
        ssh.cjk_font_data.add(font_data)

        result = _rmkit_cn.enable_localization(
            ssh, self.make_qm(), self.make_font(font_data), "Selected UI Font"
        )

        self.assertTrue(result.has_cjk_font)
        self.assertEqual(
            ssh.files[_rmkit_cn.CUSTOM_FONT_PATHS[".ttf"]], font_data
        )
        marker = json.loads(ssh.files[_rmkit_cn.FONT_MARKER_PATH])
        self.assertEqual(marker["path"], _rmkit_cn.CUSTOM_FONT_PATHS[".ttf"])
        self.assertEqual(marker["sha256"], hashlib.sha256(font_data).hexdigest())
        self.assertFalse(marker["had_fontconfig"])
        stop_index = ssh.events.index(("exec", "systemctl stop xochitl"))
        cache_index = next(
            i
            for i, event in enumerate(ssh.events)
            if event[0] == "exec" and event[1].startswith("fc-cache -f -v ")
        )
        validation_index = max(
            i
            for i, event in enumerate(ssh.events)
            if event == ("exec", _rmkit_cn.CJK_FONT_LIST_COMMAND)
        )
        translation_index = ssh.events.index(
            ("transfer", f"{_rmkit_cn.QM_PATH}.tmp")
        )
        self.assertLess(cache_index, stop_index)
        self.assertLess(validation_index, stop_index)
        self.assertLess(validation_index, translation_index)
        commands = "\n".join(value for kind, value in ssh.events if kind == "exec")
        self.assertIn(_rmkit_cn.FONTCONFIG_FILE, ssh.files)
        target = _rmkit_cn.CUSTOM_FONT_PATHS[".ttf"]
        private_family = "rmtool UI Font (Selected UI Font)"
        config = ssh.files[_rmkit_cn.FONTCONFIG_FILE].decode("utf-8")
        self.assertIn(f"<string>{target}</string>", config)
        self.assertIn(
            f"<prefer><family>{private_family}</family></prefer>",
            config,
        )
        for pattern in _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS:
            self.assertIn(
                "fc-match --format='%{file}\\n' "
                f"{shlex.quote(pattern)} | head -n 1",
                commands,
            )
        self.assertNotIn("restart", commands)
        self.assertNotIn("reboot", commands)

    def test_font_without_cjk_coverage_is_removed_before_translation_writes(self):
        ssh = self.make_ssh(cjk_available=False)
        before = dict(ssh.files)

        with self.assertRaisesRegex(RuntimeError, "主界面字体"):
            _rmkit_cn.enable_localization(
                ssh, self.make_qm(), self.make_font(b"latin-only"), "Latin Only"
            )

        self.assertEqual(ssh.files, before)
        self.assertNotIn(("exec", "systemctl stop xochitl"), ssh.events)
        self.assertFalse(
            any(
                kind == "transfer"
                and value in (f"{_rmkit_cn.QM_PATH}.tmp", f"{_rmkit_cn.CONFIG_PATH}.tmp")
                for kind, value in ssh.events
            )
        )
        self.assertGreaterEqual(
            sum(
                kind == "exec" and value.startswith("fc-cache -f -v ")
                for kind, value in ssh.events
            ),
            2,
        )

    def test_managed_font_match_failure_rolls_back_before_translation_writes(self):
        font_data = b"managed-font-with-cjk"
        mismatched_pattern = _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS[1]
        ssh = self.make_ssh(cjk_available=False)
        ssh.cjk_font_data.add(font_data)
        ssh.font_match_paths[mismatched_pattern] = "/usr/share/fonts/older-name.ttf"
        before = dict(ssh.files)

        with self.assertRaisesRegex(RuntimeError, "字体匹配校验失败"):
            _rmkit_cn.enable_localization(
                ssh,
                self.make_qm(),
                self.make_font(font_data),
                "Duplicate Managed Family",
            )

        self.assertEqual(ssh.files, before)
        self.assertNotIn(("exec", "systemctl stop xochitl"), ssh.events)
        self.assertFalse(
            any(
                kind == "transfer"
                and value
                in (f"{_rmkit_cn.QM_PATH}.tmp", f"{_rmkit_cn.CONFIG_PATH}.tmp")
                for kind, value in ssh.events
            )
        )

    def test_corrupt_remote_font_is_removed_before_cache_or_translation(self):
        font_data = b"valid-local-cjk-font"
        ssh = FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: b"[General]\n",
                _rmkit_cn.QM_PATH: self.STOCK_QM,
            },
            cjk_available=False,
            cjk_font_data=(font_data,),
            corrupt_transfer_at=1,
        )

        with self.assertRaisesRegex(RuntimeError, "上传后校验失败"):
            _rmkit_cn.enable_localization(
                ssh, self.make_qm(), self.make_font(font_data), "Corrupt Font"
            )

        self.assertNotIn(_rmkit_cn.CUSTOM_FONT_PATHS[".ttf"], ssh.files)
        self.assertNotIn(_rmkit_cn.FONT_MARKER_PATH, ssh.files)
        self.assertNotIn(("exec", "systemctl stop xochitl"), ssh.events)
        self.assertGreaterEqual(
            sum(
                kind == "exec" and value.startswith("fc-cache -f -v ")
                for kind, value in ssh.events
            ),
            2,
        )

    def test_enabled_translation_can_repair_only_the_missing_font(self):
        font_data = b"repair-cjk-font"
        ssh = FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: b"[General]\nlanguage=fr_FR\n",
                **self.managed_files(),
            },
            cjk_available=False,
            cjk_font_data=(font_data,),
        )
        config_before = ssh.files[_rmkit_cn.CONFIG_PATH]
        qm_before = ssh.files[_rmkit_cn.QM_PATH]

        result = _rmkit_cn.enable_localization(
            ssh,
            "missing-qm-is-not-needed",
            self.make_font(font_data, "repair.otf"),
            "Repair UI Font",
        )

        self.assertEqual(result.state, _rmkit_cn.LocalizationState.ENABLED)
        self.assertTrue(result.has_cjk_font)
        self.assertEqual(ssh.files[_rmkit_cn.CONFIG_PATH], config_before)
        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], qm_before)
        transferred = [value for kind, value in ssh.events if kind == "transfer"]
        self.assertNotIn(f"{_rmkit_cn.QM_PATH}.tmp", transferred)
        self.assertNotIn(f"{_rmkit_cn.CONFIG_PATH}.tmp", transferred)
        self.assertNotIn(("exec", "systemctl stop xochitl"), ssh.events)

    def test_replacing_managed_font_at_same_path_preserves_original_backup(self):
        old_font = b"old-managed-font"
        new_font = b"new-managed-cjk-font"
        target = _rmkit_cn.CUSTOM_FONT_PATHS[".ttf"]
        original_fontconfig = b"user-fontconfig\x00\r\n"
        managed_fontconfig = b"<fontconfig>old managed override</fontconfig>\n"
        old_marker = json.dumps(
            {
                "path": target,
                "sha256": hashlib.sha256(old_font).hexdigest(),
                "had_fontconfig": True,
            },
            separators=(",", ":"),
        ).encode("ascii")
        ssh = FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: b"[General]\nlanguage=fr_FR\n",
                **self.managed_files(),
                target: old_font,
                _rmkit_cn.FONTCONFIG_FILE: managed_fontconfig,
                _rmkit_cn.FONTCONFIG_BACKUP_PATH: original_fontconfig,
                _rmkit_cn.FONT_MARKER_PATH: old_marker,
            },
            cjk_available=False,
            cjk_font_data=(new_font,),
        )
        config_before = ssh.files[_rmkit_cn.CONFIG_PATH]
        qm_before = ssh.files[_rmkit_cn.QM_PATH]

        result = _rmkit_cn.enable_localization(
            ssh,
            "translation-is-already-installed",
            self.make_font(new_font, "replacement.ttf"),
            "Replacement UI Font",
        )

        self.assertTrue(result.has_cjk_font)
        self.assertEqual(ssh.files[target], new_font)
        self.assertEqual(
            ssh.files[_rmkit_cn.FONTCONFIG_BACKUP_PATH], original_fontconfig
        )
        self.assertEqual(ssh.files[_rmkit_cn.CONFIG_PATH], config_before)
        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], qm_before)
        marker = json.loads(ssh.files[_rmkit_cn.FONT_MARKER_PATH])
        self.assertEqual(marker["sha256"], hashlib.sha256(new_font).hexdigest())
        self.assertFalse(
            any(path.endswith(".rmtool-rollback") for path in ssh.files)
        )

    def test_same_path_replacement_failures_restore_font_config_and_marker(self):
        old_font = b"old-managed-font"
        new_font = b"new-managed-cjk-font"
        target = _rmkit_cn.CUSTOM_FONT_PATHS[".ttf"]
        old_marker = json.dumps(
            {
                "path": target,
                "sha256": hashlib.sha256(old_font).hexdigest(),
                "had_fontconfig": True,
            },
            separators=(",", ":"),
        ).encode("ascii")

        for failure_number in (1, 2, 3):
            with self.subTest(failure_number=failure_number):
                ssh = FakeSSH(
                    {
                        _rmkit_cn.CONFIG_PATH: b"[General]\nlanguage=fr_FR\n",
                        **self.managed_files(),
                        target: old_font,
                        _rmkit_cn.FONTCONFIG_FILE: b"old managed fontconfig",
                        _rmkit_cn.FONTCONFIG_BACKUP_PATH: b"original user fontconfig",
                        _rmkit_cn.FONT_MARKER_PATH: old_marker,
                    },
                    cjk_available=False,
                    cjk_font_data=(new_font,),
                    fail_transfer_at=failure_number,
                )
                before = dict(ssh.files)

                with self.assertRaisesRegex(IOError, "simulated upload failure"):
                    _rmkit_cn.enable_localization(
                        ssh,
                        "translation-is-already-installed",
                        self.make_font(new_font, "replacement.ttf"),
                        "Replacement UI Font",
                    )

                self.assertEqual(ssh.files, before)
                self.assertFalse(
                    any(
                        kind == "transfer"
                        and value
                        in (
                            f"{_rmkit_cn.QM_PATH}.tmp",
                            f"{_rmkit_cn.CONFIG_PATH}.tmp",
                        )
                        for kind, value in ssh.events
                    )
                )

    def test_same_path_validation_failure_restores_previous_managed_font(self):
        old_font = b"old-managed-font"
        invalid_font = b"new-font-without-cjk"
        target = _rmkit_cn.CUSTOM_FONT_PATHS[".ttf"]
        old_marker = json.dumps(
            {
                "path": target,
                "sha256": hashlib.sha256(old_font).hexdigest(),
                "had_fontconfig": False,
            },
            separators=(",", ":"),
        ).encode("ascii")
        ssh = FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: b"[General]\nlanguage=fr_FR\n",
                **self.managed_files(),
                target: old_font,
                _rmkit_cn.FONTCONFIG_FILE: b"old managed fontconfig",
                _rmkit_cn.FONT_MARKER_PATH: old_marker,
            },
            cjk_available=False,
        )
        before = dict(ssh.files)

        with self.assertRaisesRegex(RuntimeError, "主界面字体"):
            _rmkit_cn.enable_localization(
                ssh,
                "translation-is-already-installed",
                self.make_font(invalid_font, "replacement.ttf"),
                "Invalid UI Font",
            )

        self.assertEqual(ssh.files, before)

    def test_font_and_fontconfig_uploads_are_staged_before_replacement(self):
        target = _rmkit_cn.CUSTOM_FONT_PATHS[".ttf"]
        ssh = FakeSSH(
            {
                target: b"existing font",
                _rmkit_cn.FONTCONFIG_FILE: b"existing fontconfig",
            },
            fail_transfer_at=2,
        )
        with tempfile.NamedTemporaryFile(delete=False) as config_file:
            config_file.write(b"new fontconfig")
            config_path = config_file.name
        self.addCleanup(Path(config_path).unlink, missing_ok=True)

        with self.assertRaisesRegex(IOError, "simulated upload failure"):
            _rmkit_cn.upload_font(
                ssh,
                self.make_font(b"new font"),
                _rmkit_cn.FONT_DIR,
                Path(target).name,
                fontconfig_local_path=config_path,
                fontconfig_remote_path=_rmkit_cn.FONTCONFIG_FILE,
            )

        self.assertEqual(ssh.files[target], b"existing font")
        self.assertEqual(
            ssh.files[_rmkit_cn.FONTCONFIG_FILE], b"existing fontconfig"
        )
        self.assertNotIn(f"{target}.tmp", ssh.files)
        self.assertNotIn(f"{_rmkit_cn.FONTCONFIG_FILE}.tmp", ssh.files)

    def test_user_font_override_uses_device_family_and_verifies_all_matches(self):
        target_dir = "/home/root/.local/share/fonts/"
        target = f"{target_dir}zwzt.ttf"
        ssh = FakeSSH(
            device_font_family="Linux Device Family",
            font_match_paths={
                pattern: target for pattern in _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS
            },
        )

        result = _rmkit_cn.install_user_font_override(
            ssh,
            self.make_font(b"new user font"),
            target_dir,
            "zwzt.ttf",
        )

        self.assertEqual(result, target)
        self.assertEqual(ssh.files[target], b"new user font")
        config = ssh.files[_rmkit_cn.FONTCONFIG_FILE].decode("utf-8")
        private_family = "rmtool UI Font (Linux Device Family)"
        self.assertIn(f"<string>{target}</string>", config)
        self.assertIn(f"<string>{private_family}</string>", config)
        self.assertEqual(
            config.count(f"<prefer><family>{private_family}</family></prefer>"),
            2,
        )
        commands = [value for kind, value in ssh.events if kind == "exec"]
        for pattern in _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS:
            self.assertIn(
                "fc-match --format='%{file}\\n' "
                f"{shlex.quote(pattern)} | head -n 1",
                commands,
            )
        self.assertFalse(any(".rmtool-" in path for path in ssh.files))

    def test_user_font_override_binds_renamed_font_with_existing_same_family(self):
        target_dir = "/home/root/.local/share/fonts/"
        target = f"{target_dir}renamed-upload.ttf"
        existing = f"{target_dir}existing-name.ttf"
        ssh = FakeSSH(
            {existing: b"existing font with same internal family"},
            device_font_family="Duplicate Family",
        )

        result = _rmkit_cn.install_user_font_override(
            ssh,
            self.make_font(b"new font with duplicate family"),
            target_dir,
            "renamed-upload.ttf",
        )

        self.assertEqual(result, target)
        self.assertEqual(ssh.files[existing], b"existing font with same internal family")
        config = ssh.files[_rmkit_cn.FONTCONFIG_FILE].decode("utf-8")
        self.assertIn(f"<string>{target}</string>", config)
        self.assertNotIn(f"<string>{existing}</string>", config)

    def test_fontconfig_override_normalizes_and_escapes_path_and_family(self):
        config = _rmkit_cn.fontconfig_override(
            "Family & <UI>",
            "/home/root/fonts/subdir/../Renamed & <UI>.ttf",
        )

        self.assertIn(
            "<string>/home/root/fonts/Renamed &amp; &lt;UI&gt;.ttf</string>",
            config,
        )
        self.assertIn(
            "<string>rmtool UI Font (Family &amp; &lt;UI&gt;)</string>",
            config,
        )
        self.assertIn(
            "<prefer><family>rmtool UI Font (Family &amp; &lt;UI&gt;)</family></prefer>",
            config,
        )

    def test_user_font_override_restores_previous_files_when_match_fails(self):
        target_dir = "/home/root/.local/share/fonts/"
        target = f"{target_dir}zwzt.ttf"
        original = {
            target: b"old user font",
            _rmkit_cn.FONTCONFIG_FILE: b"old user fontconfig",
        }
        match_paths = {
            pattern: target for pattern in _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS
        }
        match_paths["reMarkable Sans:lang=zh-cn"] = "/usr/share/fonts/default.ttf"
        ssh = FakeSSH(
            original,
            device_font_family="Linux Device Family",
            font_match_paths=match_paths,
        )

        with self.assertRaisesRegex(RuntimeError, "字体匹配校验失败"):
            _rmkit_cn.install_user_font_override(
                ssh,
                self.make_font(b"new user font"),
                target_dir,
                "zwzt.ttf",
            )

        self.assertEqual(ssh.files, original)

    def test_user_font_inventory_lists_only_top_level_regular_fonts(self):
        font_dir = "/home/root/.local/share/fonts"
        active = f"{font_dir}/active.ttf"
        ssh = FakeSSH(
            {
                active: b"active",
                f"{font_dir}/other.otf": b"other",
                f"{font_dir}/notes.txt": b"not a font",
                f"{font_dir}/rmtool-localization/internal.otf": b"internal",
                _rmkit_cn.FONTCONFIG_FILE: _rmkit_cn.fontconfig_override(
                    "Duplicate Family", active
                ).encode("utf-8"),
            },
            device_font_family="Duplicate Family",
        )

        fonts = _rmkit_cn.list_user_fonts(ssh, font_dir)

        self.assertEqual([font.filename for font in fonts], ["active.ttf", "other.otf"])
        self.assertEqual([font.active for font in fonts], [True, False])

    def test_user_font_inventory_protects_every_divergent_ui_match(self):
        font_dir = "/home/root/.local/share/fonts"
        first = f"{font_dir}/first.ttf"
        second = f"{font_dir}/second.otf"
        matches = {
            _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS[0]: first,
            _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS[1]: second,
            _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS[2]: "/usr/share/fonts/default.ttf",
        }
        ssh = FakeSSH(
            {first: b"first", second: b"second"},
            font_match_paths=matches,
        )

        fonts = _rmkit_cn.list_user_fonts(ssh, font_dir)

        self.assertEqual([font.filename for font in fonts], ["first.ttf", "second.otf"])
        self.assertEqual([font.active for font in fonts], [True, True])
        for filename in ("first.ttf", "second.otf"):
            with self.subTest(filename=filename), self.assertRaisesRegex(
                RuntimeError, "当前系统字体不能删除"
            ):
                _rmkit_cn.delete_user_font(ssh, font_dir, filename)

    def test_upload_user_font_does_not_change_active_fontconfig_path(self):
        font_dir = "/home/root/.local/share/fonts"
        active = f"{font_dir}/active.ttf"
        config = _rmkit_cn.fontconfig_override("Same Family", active).encode("utf-8")
        ssh = FakeSSH(
            {active: b"active", _rmkit_cn.FONTCONFIG_FILE: config},
            device_font_family="Same Family",
        )

        uploaded = _rmkit_cn.upload_user_font(
            ssh, self.make_font(b"new font", "new.ttf"), font_dir, "new.ttf"
        )

        self.assertEqual(ssh.files[f"{font_dir}/new.ttf"], b"new font")
        self.assertEqual(ssh.files[_rmkit_cn.FONTCONFIG_FILE], config)
        self.assertFalse(uploaded.active)

    def test_upload_user_font_refuses_to_replace_any_active_filename(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/zwzt.ttf"
        matches = {
            _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS[0]: target,
            _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS[1]: "/usr/share/fonts/default.ttf",
            _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS[2]: target,
        }
        ssh = FakeSSH({target: b"active font"}, font_match_paths=matches)

        with self.assertRaisesRegex(RuntimeError, "上传目标正被系统字体配置使用"):
            _rmkit_cn.upload_user_font(
                ssh, self.make_font(b"replacement", "zwzt.ttf"), font_dir, "zwzt.ttf"
            )

        self.assertEqual(ssh.files[target], b"active font")
        self.assertFalse(any(kind == "transfer" for kind, _ in ssh.events))

    def test_upload_user_font_rechecks_active_path_after_transfer(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/candidate.ttf"
        ssh = FakeSSH()

        with patch.object(
            _rmkit_cn,
            "_ui_font_match_paths",
            side_effect=[("/usr/share/fonts/default.ttf",), (target,)],
        ), self.assertRaisesRegex(RuntimeError, "操作期间成为系统字体"):
            _rmkit_cn.upload_user_font(
                ssh,
                self.make_font(b"candidate", "candidate.ttf"),
                font_dir,
                "candidate.ttf",
            )

        self.assertNotIn(target, ssh.files)
        self.assertFalse(any(".rmtool-" in path for path in ssh.files))

    def test_upload_user_font_rejects_symlink_at_exact_target_path(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/link.ttf"
        ssh = FakeSSH({target: b"link target"})

        class SymlinkTargetSFTP(FakeSFTP):
            def lstat(self, path):
                if path == target:
                    return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
                return super().lstat(path)

        @contextmanager
        def symlink_session():
            yield SymlinkTargetSFTP(ssh.files)

        ssh.sftp_session = symlink_session

        with self.assertRaisesRegex(RuntimeError, "非普通字体文件"):
            _rmkit_cn.upload_user_font(
                ssh, self.make_font(b"new", "link.ttf"), font_dir, "link.ttf"
            )

        self.assertEqual(ssh.files[target], b"link target")
        self.assertFalse(any(".rmtool-" in path for path in ssh.files))

    def test_set_active_user_font_uses_exact_path_for_duplicate_family(self):
        font_dir = "/home/root/.local/share/fonts"
        old_path = f"{font_dir}/old-name.ttf"
        new_path = f"{font_dir}/new-name.ttf"
        ssh = FakeSSH(
            {old_path: b"old", new_path: b"new"},
            device_font_family="Duplicate Family",
        )

        selected = _rmkit_cn.set_active_user_font(ssh, font_dir, "new-name.ttf")

        self.assertTrue(selected.active)
        self.assertEqual(selected.remote_path, new_path)
        config = ET.fromstring(ssh.files[_rmkit_cn.FONTCONFIG_FILE].decode("utf-8"))
        self.assertEqual(
            config.find("./match[@target='scan']/test[@name='file']/string").text,
            new_path,
        )

    def test_set_active_user_font_restores_exact_fontconfig_bytes_on_failure(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/candidate.ttf"
        previous = b"\x00<fontconfig>exact prior bytes</fontconfig>\r\n"
        mismatch = {pattern: target for pattern in _rmkit_cn.FONT_OVERRIDE_MATCH_PATTERNS}
        mismatch["reMarkable Sans:lang=zh-cn"] = "/usr/share/fonts/default.ttf"
        ssh = FakeSSH(
            {target: b"candidate", _rmkit_cn.FONTCONFIG_FILE: previous},
            font_match_paths=mismatch,
        )

        with self.assertRaisesRegex(RuntimeError, "字体匹配校验失败"):
            _rmkit_cn.set_active_user_font(ssh, font_dir, "candidate.ttf")

        self.assertEqual(ssh.files[_rmkit_cn.FONTCONFIG_FILE], previous)

    def test_set_active_user_font_restores_config_after_transfer_failure(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/candidate.ttf"
        previous = b"<fontconfig>prior transfer bytes</fontconfig>\r\n"
        ssh = FakeSSH(
            {target: b"candidate", _rmkit_cn.FONTCONFIG_FILE: previous},
            fail_transfer_at=1,
        )

        with self.assertRaisesRegex(IOError, "upload failure"):
            _rmkit_cn.set_active_user_font(ssh, font_dir, "candidate.ttf")

        self.assertEqual(ssh.files[_rmkit_cn.FONTCONFIG_FILE], previous)
        self.assertNotIn(f"{_rmkit_cn.FONTCONFIG_FILE}.tmp", ssh.files)

    def test_set_active_user_font_restores_config_after_cache_failure(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/candidate.ttf"
        previous = b"<fontconfig>prior cache bytes</fontconfig>\n"
        ssh = FakeSSH(
            {target: b"candidate", _rmkit_cn.FONTCONFIG_FILE: previous},
            fail_cache_count=1,
        )

        with self.assertRaisesRegex(IOError, "cache failure"):
            _rmkit_cn.set_active_user_font(ssh, font_dir, "candidate.ttf")

        self.assertEqual(ssh.files[_rmkit_cn.FONTCONFIG_FILE], previous)
        self.assertNotIn(f"{_rmkit_cn.FONTCONFIG_FILE}.tmp", ssh.files)

    def test_upload_user_font_restores_replaced_file_when_cache_fails(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/replace.ttf"
        ssh = FakeSSH({target: b"old"}, fail_cache_count=1)

        with self.assertRaisesRegex(IOError, "cache failure"):
            _rmkit_cn.upload_user_font(
                ssh, self.make_font(b"new", "replace.ttf"), font_dir, "replace.ttf"
            )

        self.assertEqual(ssh.files[target], b"old")
        self.assertFalse(any(".rmtool-" in path for path in ssh.files))

    def test_upload_user_font_rolls_back_if_refresh_changes_ui_matches(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/replace.ttf"
        original_matches = ("/usr/share/fonts/default.ttf",)
        ssh = FakeSSH({target: b"old"})

        with patch.object(
            _rmkit_cn,
            "_ui_font_match_paths",
            side_effect=[original_matches, original_matches, (target,)],
        ), self.assertRaisesRegex(RuntimeError, "系统字体匹配发生变化"):
            _rmkit_cn.upload_user_font(
                ssh, self.make_font(b"new", "replace.ttf"), font_dir, "replace.ttf"
            )

        self.assertEqual(ssh.files[target], b"old")
        self.assertFalse(any(".rmtool-" in path for path in ssh.files))

    def test_upload_user_font_restores_replaced_file_when_backup_remove_fails(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/replace.ttf"
        ssh = FakeSSH({target: b"old"})
        failed = False

        class RemoveOnceSFTP(FakeSFTP):
            def remove(self, path):
                nonlocal failed
                if path.endswith(".bak") and not failed:
                    failed = True
                    raise IOError("simulated remove failure")
                return super().remove(path)

        @contextmanager
        def failing_session():
            yield RemoveOnceSFTP(ssh.files)

        ssh.sftp_session = failing_session

        with self.assertRaisesRegex(IOError, "remove failure"):
            _rmkit_cn.upload_user_font(
                ssh, self.make_font(b"new", "replace.ttf"), font_dir, "replace.ttf"
            )

        self.assertEqual(ssh.files[target], b"old")
        self.assertFalse(any(".rmtool-" in path for path in ssh.files))

    def test_upload_user_font_cleans_partial_transfer(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/new.ttf"
        ssh = FakeSSH(fail_transfer_at=1)

        with self.assertRaisesRegex(IOError, "upload failure"):
            _rmkit_cn.upload_user_font(
                ssh, self.make_font(b"new", "new.ttf"), font_dir, "new.ttf"
            )

        self.assertNotIn(target, ssh.files)
        self.assertFalse(any(".rmtool-" in path for path in ssh.files))

    def test_delete_user_font_blocks_active_and_deletes_only_selected_file(self):
        font_dir = "/home/root/.local/share/fonts"
        active = f"{font_dir}/active.ttf"
        inactive = f"{font_dir}/inactive.ttf"
        config = _rmkit_cn.fontconfig_override("Family", active).encode("utf-8")
        ssh = FakeSSH(
            {
                active: b"active",
                inactive: b"inactive",
                f"{font_dir}/keep.otf": b"keep",
                _rmkit_cn.FONTCONFIG_FILE: config,
            }
        )

        with self.assertRaisesRegex(RuntimeError, "当前系统字体不能删除"):
            _rmkit_cn.delete_user_font(ssh, font_dir, "active.ttf")
        _rmkit_cn.delete_user_font(ssh, font_dir, "inactive.ttf")

        self.assertIn(active, ssh.files)
        self.assertNotIn(inactive, ssh.files)
        self.assertIn(f"{font_dir}/keep.otf", ssh.files)

    def test_delete_user_font_restores_selected_file_when_cache_fails(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/inactive.ttf"
        ssh = FakeSSH({target: b"inactive"}, fail_cache_count=1)

        with self.assertRaisesRegex(IOError, "cache failure"):
            _rmkit_cn.delete_user_font(ssh, font_dir, "inactive.ttf")

        self.assertEqual(ssh.files[target], b"inactive")
        self.assertFalse(any("rmtool-delete" in path for path in ssh.files))

    def test_delete_user_font_restores_selected_file_when_backup_remove_fails(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/inactive.ttf"
        ssh = FakeSSH({target: b"inactive"})
        failed = False

        class RemoveOnceSFTP(FakeSFTP):
            def remove(self, path):
                nonlocal failed
                if "rmtool-delete" in path and not failed:
                    failed = True
                    raise IOError("simulated remove failure")
                return super().remove(path)

        @contextmanager
        def failing_session():
            yield RemoveOnceSFTP(ssh.files)

        ssh.sftp_session = failing_session

        with self.assertRaisesRegex(IOError, "remove failure"):
            _rmkit_cn.delete_user_font(ssh, font_dir, "inactive.ttf")

        self.assertEqual(ssh.files[target], b"inactive")
        self.assertFalse(any("rmtool-delete" in path for path in ssh.files))

    def test_delete_user_font_revalidates_regular_file_before_removal(self):
        font_dir = "/home/root/.local/share/fonts"
        target = f"{font_dir}/link.ttf"
        ssh = FakeSSH({target: b"link target"})

        class SymlinkSFTP(FakeSFTP):
            def lstat(self, path):
                if path == target:
                    return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
                return super().lstat(path)

        @contextmanager
        def symlink_session():
            yield SymlinkSFTP(ssh.files)

        ssh.sftp_session = symlink_session

        with self.assertRaisesRegex(RuntimeError, "不是普通文件"):
            _rmkit_cn.delete_user_font(ssh, font_dir, "link.ttf")

        self.assertEqual(ssh.files[target], b"link target")

    def test_user_font_operations_reject_traversal_system_and_internal_paths(self):
        ssh = FakeSSH()
        for directory, filename in (
            ("/home/root/.local/share/fonts", "../escape.ttf"),
            ("/usr/share/fonts", "system.ttf"),
            (_rmkit_cn.FONT_DIR, "internal.ttf"),
        ):
            with self.subTest(directory=directory, filename=filename):
                with self.assertRaises(RuntimeError):
                    _rmkit_cn.delete_user_font(ssh, directory, filename)

    def test_malformed_font_markers_are_rejected_without_writes(self):
        target = _rmkit_cn.CUSTOM_FONT_PATHS[".ttf"]
        malformed_markers = (
            b"{",
            json.dumps(
                {"path": [], "sha256": "0" * 64, "had_fontconfig": False}
            ).encode(),
            json.dumps(
                {"path": target, "sha256": 7, "had_fontconfig": False}
            ).encode(),
            json.dumps(
                {"path": target, "sha256": "0" * 64, "had_fontconfig": 1}
            ).encode(),
        )

        for marker in malformed_markers:
            with self.subTest(marker=marker):
                ssh = self.make_ssh()
                ssh.files[_rmkit_cn.FONT_MARKER_PATH] = marker
                before = dict(ssh.files)

                with self.assertRaisesRegex(RuntimeError, "字体标记无效"):
                    _rmkit_cn.get_localization_status(ssh)

                self.assertEqual(ssh.files, before)
                self.assertFalse(any(kind == "transfer" for kind, _ in ssh.events))
                self.assertFalse(
                    any(
                        kind == "exec"
                        and value.startswith(
                            ("mkdir ", "cp ", "mv ", "rm ", "touch ", "chmod ", "fc-cache ", "mount ", "systemctl ")
                        )
                        for kind, value in ssh.events
                    )
                )

    def test_restore_keeps_all_retry_metadata_when_font_removal_fails(self):
        font_data = b"managed-font"
        target = _rmkit_cn.CUSTOM_FONT_PATHS[".otf"]
        marker = json.dumps(
            {
                "path": target,
                "sha256": hashlib.sha256(font_data).hexdigest(),
                "had_fontconfig": True,
            },
            separators=(",", ":"),
        ).encode("ascii")
        remove_command = f"rm -f {target}"
        ssh = FakeSSH(
            {
                _rmkit_cn.CONFIG_PATH: b"[General]\nlanguage=fr_FR\nruntime=1\n",
                **self.managed_files(),
                target: font_data,
                _rmkit_cn.FONTCONFIG_FILE: b"managed fontconfig",
                _rmkit_cn.FONTCONFIG_BACKUP_PATH: b"original user fontconfig",
                _rmkit_cn.FONT_MARKER_PATH: marker,
            },
            cjk_available=False,
            cjk_font_data=(font_data,),
            fail_exec_commands=(remove_command,),
        )

        with self.assertRaisesRegex(IOError, "simulated command failure"):
            _rmkit_cn.restore_localization(ssh)

        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], self.STOCK_QM)
        self.assertNotIn(b"language=fr_FR", ssh.files[_rmkit_cn.CONFIG_PATH])
        for path in (
            _rmkit_cn.BACKUP_READY_PATH,
            _rmkit_cn.BACKUP_CONFIG_PATH,
            _rmkit_cn.BACKUP_QM_PATH,
            _rmkit_cn.FONT_MARKER_PATH,
            _rmkit_cn.FONTCONFIG_BACKUP_PATH,
        ):
            self.assertIn(path, ssh.files)

        ssh.fail_exec_commands.clear()
        result = _rmkit_cn.restore_localization(ssh)

        self.assertEqual(result.state, _rmkit_cn.LocalizationState.NOT_INSTALLED)
        self.assertNotIn(target, ssh.files)
        for path in (
            _rmkit_cn.BACKUP_READY_PATH,
            _rmkit_cn.BACKUP_CONFIG_PATH,
            _rmkit_cn.BACKUP_QM_PATH,
            _rmkit_cn.FONT_MARKER_PATH,
            _rmkit_cn.FONTCONFIG_BACKUP_PATH,
        ):
            self.assertNotIn(path, ssh.files)

    def test_failed_enable_keeps_retry_metadata_when_font_rollback_fails(self):
        font_data = b"new-managed-cjk-font"
        target = _rmkit_cn.CUSTOM_FONT_PATHS[".ttf"]
        remove_command = f"rm -f {target}"
        ssh = self.make_ssh(
            cjk_available=False,
            cjk_font_data=(font_data,),
            fail_transfer_at=4,
            fail_exec_commands=(remove_command,),
        )

        with self.assertRaisesRegex(IOError, "simulated upload failure"):
            _rmkit_cn.enable_localization(
                ssh,
                self.make_qm(),
                self.make_font(font_data),
                "Managed UI Font",
            )

        self.assertEqual(ssh.files[_rmkit_cn.QM_PATH], self.STOCK_QM)
        self.assertEqual(ssh.files[_rmkit_cn.CONFIG_PATH], b"[General]\n")
        for path in (
            _rmkit_cn.BACKUP_READY_PATH,
            _rmkit_cn.BACKUP_CONFIG_PATH,
            _rmkit_cn.BACKUP_QM_PATH,
            _rmkit_cn.FONT_MARKER_PATH,
        ):
            self.assertIn(path, ssh.files)

        ssh.fail_exec_commands.clear()
        result = _rmkit_cn.restore_localization(ssh)

        self.assertEqual(result.state, _rmkit_cn.LocalizationState.NOT_INSTALLED)
        self.assertNotIn(target, ssh.files)
        self.assertNotIn(_rmkit_cn.FONT_MARKER_PATH, ssh.files)
        self.assertNotIn(_rmkit_cn.BACKUP_READY_PATH, ssh.files)

    def test_restore_removes_only_hash_matching_managed_font(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                original_font = b"managed-cjk-font"
                current_font = b"user-replaced-font" if changed else original_font
                font_path = _rmkit_cn.CUSTOM_FONT_PATHS[".otf"]
                marker = json.dumps(
                    {
                        "path": font_path,
                        "sha256": hashlib.sha256(original_font).hexdigest(),
                        "had_fontconfig": False,
                    },
                    separators=(",", ":"),
                ).encode("ascii")
                ssh = FakeSSH(
                    {
                        _rmkit_cn.CONFIG_PATH: b"[General]\nlanguage=fr_FR\n",
                        **self.managed_files(),
                        font_path: current_font,
                        _rmkit_cn.FONT_MARKER_PATH: marker,
                    }
                )

                _rmkit_cn.restore_localization(ssh)

                self.assertEqual(font_path in ssh.files, changed)
                if changed:
                    self.assertEqual(ssh.files[font_path], current_font)
                self.assertNotIn(_rmkit_cn.FONT_MARKER_PATH, ssh.files)

    def test_bundled_font_and_cloud_translation_inputs_are_present(self):
        font_path = Path("assets/fonts") / _rmkit_cn.BUNDLED_FONT_NAME
        license_path = Path("assets/fonts/LICENSE")
        qm_path = Path("translations/reMarkable_zh_CN.qm")
        ferrari_qm_path = Path("translations/reMarkable_zh_CN_ferrari.qm")
        legacy_qm_path = Path(
            "translations/reMarkable_zh_CN-20260612085811-legacy.qm"
        )
        beta_qm_path = Path(
            "translations/reMarkable_zh_CN-20260629074044.qm"
        )
        manifest_path = Path("translations/manifest.json")

        self.assertEqual(font_path.stat().st_size, 16_437_364)
        self.assertEqual(
            hashlib.sha256(font_path.read_bytes()).hexdigest(),
            _rmkit_cn.BUNDLED_FONT_SHA256,
        )
        self.assertIn(
            "SIL OPEN FONT LICENSE Version 1.1",
            license_path.read_text(encoding="utf-8"),
        )
        packages = _rmkit_cn.parse_translation_manifest(manifest_path.read_bytes())
        package = packages[_rmkit_cn.SUPPORTED_FIRMWARE]
        self.assertEqual(package.size, qm_path.stat().st_size)
        self.assertEqual(
            package.localized_qm_sha256,
            hashlib.sha256(qm_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            package.stock_french_sha256,
            "8e0db0f7a2d3116469e1aae4f52657ccc38d0422b5b958ae512554bd018f285e",
        )
        self.assertEqual(package.platform, "chiappa")
        self.assertEqual(len(package.variants), 4)
        ferrari = package.variants[0]
        self.assertEqual(ferrari.platform, "ferrari")
        self.assertEqual(ferrari.size, ferrari_qm_path.stat().st_size)
        self.assertEqual(
            ferrari.localized_qm_sha256,
            hashlib.sha256(ferrari_qm_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            ferrari.stock_french_sha256,
            "9f62dc83b150e48b8d4e1688c1b16d22aa09fdd1ba09b772954394ec6c1ab4fb",
        )
        tatsu = package.variants[1]
        self.assertEqual(tatsu.release_version, package.release_version)
        self.assertEqual(tatsu.channel, package.channel)
        self.assertEqual(tatsu.platform, "tatsu")
        self.assertEqual(tatsu.asset, package.asset)
        self.assertEqual(tatsu.size, 175_519)
        self.assertEqual(tatsu.localized_qm_sha256, package.localized_qm_sha256)
        self.assertEqual(
            tatsu.stock_french_sha256,
            "2ee88b18955776e8f6f52949b6c172d50d14f60f3e59d75db7d17881377a7b3a",
        )
        legacy_bytes = legacy_qm_path.read_bytes()
        rm1, rm2 = package.variants[2:]
        self.assertEqual([rm1.platform, rm2.platform], ["rm1", "rm2"])
        self.assertEqual(rm1.release_version, package.release_version)
        self.assertEqual(rm2.release_version, package.release_version)
        self.assertEqual(rm1.channel, package.channel)
        self.assertEqual(rm2.channel, package.channel)
        self.assertEqual(rm1.asset, legacy_qm_path.name)
        self.assertEqual(rm2.asset, legacy_qm_path.name)
        self.assertEqual(rm1.size, len(legacy_bytes))
        self.assertEqual(rm2.size, len(legacy_bytes))
        self.assertEqual(rm1.size, 188_407)
        self.assertEqual(
            hashlib.sha256(legacy_bytes).hexdigest(),
            "517e70cdf4d862b8ceec57d3238ece72b3799aecdf075c0183668acfc2137c64",
        )
        self.assertEqual(
            rm1.localized_qm_sha256,
            hashlib.sha256(legacy_bytes).hexdigest(),
        )
        self.assertEqual(rm2.localized_qm_sha256, rm1.localized_qm_sha256)
        self.assertEqual(
            rm1.stock_french_sha256,
            "0767babb6d55fc960565568d6af89455ba233194a4d887d70bd1c7987c3898a4",
        )
        self.assertEqual(
            rm2.stock_french_sha256,
            "8080219cb5b3a75a1423ac0cee5bd12d3ee1c9029ff22ecf981cf075559900a7",
        )
        self.assertEqual(package.release_version, "3.27.3.0")
        self.assertEqual(package.channel, "stable")
        previous = packages["20260506100933"]
        self.assertEqual(previous.release_version, "3.27.1.0")
        self.assertEqual(previous.asset, package.asset)
        self.assertEqual(previous.localized_qm_sha256, package.localized_qm_sha256)
        self.assertEqual(previous.stock_french_sha256, package.stock_french_sha256)
        self.assertEqual(len(previous.variants), 1)
        self.assertEqual(
            [previous.platform, *(item.platform for item in previous.variants)],
            ["chiappa", "ferrari"],
        )
        previous_ferrari = previous.variants[0]
        self.assertEqual(previous_ferrari.asset, ferrari.asset)
        self.assertEqual(
            previous_ferrari.localized_qm_sha256,
            ferrari.localized_qm_sha256,
        )
        self.assertEqual(
            previous_ferrari.stock_french_sha256,
            ferrari.stock_french_sha256,
        )
        beta = packages["20260629074044"]
        beta_bytes = beta_qm_path.read_bytes()
        self.assertEqual(beta.release_version, "3.28.0.162")
        self.assertEqual(beta.channel, "beta")
        self.assertEqual(beta.platform, "chiappa")
        self.assertEqual(beta.asset, beta_qm_path.name)
        self.assertEqual(beta.size, len(beta_bytes))
        self.assertEqual(beta.size, 178_170)
        self.assertEqual(
            hashlib.sha256(beta_bytes).hexdigest(),
            "4f0fa45abdb944f42a44a356ae25d88f283ec2b193a211f59a7030be0342028e",
        )
        self.assertEqual(
            beta.localized_qm_sha256,
            hashlib.sha256(beta_bytes).hexdigest(),
        )
        self.assertEqual(
            beta.stock_french_sha256,
            "3d722f4018f33a24c738bfd14f821603c176d06c9d7e81714e2763d3d40eeb12",
        )
        self.assertEqual(len(beta.variants), 1)
        self.assertEqual(
            [beta.platform, *(item.platform for item in beta.variants)],
            ["chiappa", "ferrari"],
        )
        beta_ferrari = beta.variants[0]
        self.assertEqual(beta_ferrari.release_version, beta.release_version)
        self.assertEqual(beta_ferrari.channel, beta.channel)
        self.assertEqual(beta_ferrari.platform, "ferrari")
        self.assertEqual(beta_ferrari.asset, beta.asset)
        self.assertEqual(beta_ferrari.size, beta.size)
        self.assertEqual(
            beta_ferrari.localized_qm_sha256,
            beta.localized_qm_sha256,
        )
        self.assertEqual(
            beta_ferrari.stock_french_sha256,
            "24393f00d9edb933933b436ffe5020990dd97d31d7788172907d75ff1d42d3a5",
        )
        build_script = Path("build-portable.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("assets\\fonts\\NotoSansCJKsc-Regular.otf", build_script)
        self.assertIn("assets\\fonts\\LICENSE", build_script)
        self.assertIn("assets\\fonts');assets\\fonts", build_script)
        self.assertNotIn("translations\\reMarkable_zh_CN.qm", build_script)
        self.assertNotIn("translations\\reMarkable_zh_CN_ferrari.qm", build_script)
        self.assertNotIn(
            "translations\\reMarkable_zh_CN-20260612085811-legacy.qm",
            build_script,
        )
        self.assertIn('"--onefile"', build_script)
        self.assertIn("rmtool-windows-x64-onefile.exe", build_script)

    def test_cloud_manifest_refresh_is_cached_for_offline_use(self):
        manifest = Path("translations/manifest.json").read_bytes()
        with tempfile.TemporaryDirectory() as state_dir:
            with patch.object(
                _rmkit_cn, "_download_limited", return_value=manifest
            ) as download:
                online = _rmkit_cn.load_translation_catalog(state_dir)

            manifest_cache = (
                Path(state_dir) / "cache" / "localization" / "manifest.json"
            )
            self.assertEqual(manifest_cache.read_bytes(), manifest)
            self.assertFalse(manifest_cache.with_name("manifest.json.tmp").exists())
            download.assert_called_once_with(
                _rmkit_cn.TRANSLATION_MANIFEST_URL,
                _rmkit_cn.MAX_MANIFEST_BYTES,
            )

            with patch.object(
                _rmkit_cn, "_download_limited", side_effect=OSError("offline")
            ):
                offline = _rmkit_cn.load_translation_catalog(state_dir)

            self.assertEqual(online, offline)

    def test_cloud_manifest_rejects_unsafe_release_metadata(self):
        valid_entry = {
            "asset": "reMarkable_zh_CN-20260612085811.qm",
            "release_version": "3.27.3.0",
            "channel": "stable",
            "size": 175_519,
            "sha256": "1" * 64,
            "stock_french_sha256": "2" * 64,
        }
        for field, value in (
            ("asset", "../other.qm"),
            ("channel", "nightly"),
            ("size", _rmkit_cn.MAX_TRANSLATION_BYTES + 1),
            ("sha256", "not-a-hash"),
        ):
            with self.subTest(field=field):
                entry = dict(valid_entry)
                entry[field] = value
                data = json.dumps(
                    {
                        "schema": _rmkit_cn.TRANSLATION_MANIFEST_SCHEMA,
                        "firmwares": {_rmkit_cn.SUPPORTED_FIRMWARE: entry},
                    }
                ).encode()
                with self.assertRaises(RuntimeError):
                    _rmkit_cn.parse_translation_manifest(data)

    def test_cloud_manifest_parses_hardware_variants(self):
        common = {
            "asset": "reMarkable_zh_CN-20260612085811.qm",
            "release_version": "3.27.3.0",
            "channel": "stable",
            "size": 175_519,
            "sha256": "1" * 64,
        }
        entry = {
            **common,
            "platform": "chiappa",
            "stock_french_sha256": "2" * 64,
            "variants": [
                {
                    **common,
                    "platform": "ferrari",
                    "stock_french_sha256": "3" * 64,
                }
            ],
        }
        package = _rmkit_cn.parse_translation_manifest(
            json.dumps(
                {
                    "schema": _rmkit_cn.TRANSLATION_MANIFEST_SCHEMA,
                    "firmwares": {_rmkit_cn.SUPPORTED_FIRMWARE: entry},
                }
            ).encode()
        )[_rmkit_cn.SUPPORTED_FIRMWARE]

        self.assertEqual(package.platform, "chiappa")
        self.assertEqual(package.stock_french_sha256, "2" * 64)
        self.assertEqual(len(package.variants), 1)
        self.assertEqual(package.variants[0].platform, "ferrari")
        self.assertEqual(package.variants[0].stock_french_sha256, "3" * 64)
        self.assertEqual(
            package.localized_qm_sha256,
            package.variants[0].localized_qm_sha256,
        )

    def test_cloud_manifest_rejects_malformed_or_duplicate_variants(self):
        common = {
            "asset": "reMarkable_zh_CN-20260612085811.qm",
            "release_version": "3.27.3.0",
            "channel": "stable",
            "size": 175_519,
            "sha256": "1" * 64,
        }
        primary = {
            **common,
            "platform": "chiappa",
            "stock_french_sha256": "2" * 64,
        }
        variant = {
            **common,
            "platform": "ferrari",
            "stock_french_sha256": "3" * 64,
        }
        cases = (
            ("non-list", {**primary, "variants": {}}),
            ("missing-primary-platform", {
                **common,
                "stock_french_sha256": "2" * 64,
                "variants": [variant],
            }),
            ("non-record", {**primary, "variants": ["ferrari"]}),
            ("missing-platform", {
                **primary,
                "variants": [{**common, "stock_french_sha256": "3" * 64}],
            }),
            ("unsafe-platform", {
                **primary,
                "variants": [{**variant, "platform": "../ferrari"}],
            }),
            ("duplicate-platform", {
                **primary,
                "variants": [{**variant, "platform": "CHIAPPA"}],
            }),
            ("duplicate-stock", {
                **primary,
                "variants": [
                    {**variant, "stock_french_sha256": "2" * 64}
                ],
            }),
        )
        for name, entry in cases:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                _rmkit_cn.parse_translation_manifest(
                    json.dumps(
                        {
                            "schema": _rmkit_cn.TRANSLATION_MANIFEST_SCHEMA,
                            "firmwares": {
                                _rmkit_cn.SUPPORTED_FIRMWARE: entry
                            },
                        }
                    ).encode()
                )

    def test_cloud_translation_download_is_verified_and_reused(self):
        package = self.make_translation_package()
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            _rmkit_cn, "_download_limited", return_value=self.LOCALIZED_QM
        ) as download:
            first = _rmkit_cn.download_translation_package(package, state_dir)
            second = _rmkit_cn.download_translation_package(package, state_dir)

            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), self.LOCALIZED_QM)
            self.assertFalse(first.with_name(f"{first.name}.tmp").exists())
            download.assert_called_once_with(
                package.download_url, _rmkit_cn.MAX_TRANSLATION_BYTES
            )

    def test_invalid_cloud_translation_never_replaces_cache(self):
        package = self.make_translation_package()
        with tempfile.TemporaryDirectory() as state_dir:
            destination = (
                Path(state_dir)
                / "cache"
                / "localization"
                / package.firmware
                / package.asset
            )
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"previous-invalid-cache")

            with patch.object(
                _rmkit_cn, "_download_limited", return_value=b"truncated"
            ), self.assertRaisesRegex(RuntimeError, "大小"):
                _rmkit_cn.download_translation_package(package, state_dir)

            self.assertEqual(destination.read_bytes(), b"previous-invalid-cache")
            self.assertFalse(destination.with_name(f"{destination.name}.tmp").exists())

    def test_cloud_download_failure_happens_before_device_mutation(self):
        package = self.make_translation_package()
        ssh = self.make_ssh()
        with patch.object(
            _rmkit_cn,
            "download_translation_package",
            side_effect=RuntimeError("offline"),
        ), self.assertRaisesRegex(RuntimeError, "offline"):
            _rmkit_cn.enable_cloud_localization(ssh, package, ".rmtool")

        self.assertEqual(ssh.events, [])
        self.assertEqual(ssh.close_count, 0)

    def test_cloud_status_matches_exact_firmware_package(self):
        package = self.make_translation_package()
        ssh = self.make_ssh()
        with patch.object(
            _rmkit_cn,
            "load_translation_catalog",
            return_value={package.firmware: package},
        ):
            status = _rmkit_cn.get_cloud_localization_status(ssh, ".rmtool")

        self.assertIs(status.package, package)
        self.assertEqual(status.available_packages, (package,))
        self.assertEqual(status.state, _rmkit_cn.LocalizationState.NOT_INSTALLED)

        unsupported = self.make_ssh(firmware="20260612085812")
        with patch.object(
            _rmkit_cn,
            "load_translation_catalog",
            return_value={package.firmware: package},
        ):
            status = _rmkit_cn.get_cloud_localization_status(
                unsupported, ".rmtool"
            )

        self.assertEqual(status.state, _rmkit_cn.LocalizationState.INCOMPATIBLE)
        self.assertIsNone(status.package)
        self.assertEqual(status.available_packages, ())
        self.assertEqual(unsupported.events, [("exec", "cat /etc/version")])

    def test_cloud_status_selects_hardware_variant_by_stock_hash(self):
        package, ferrari, ferrari_stock, tatsu, tatsu_stock = (
            self.make_variant_packages()
        )
        for carrier, expected in (
            (self.STOCK_QM, package),
            (ferrari_stock, ferrari),
            (tatsu_stock, tatsu),
        ):
            with self.subTest(platform=expected.platform):
                ssh = self.make_ssh()
                ssh.files[_rmkit_cn.QM_PATH] = carrier
                with patch.object(
                    _rmkit_cn,
                    "load_translation_catalog",
                    return_value={package.firmware: package},
                ):
                    status = _rmkit_cn.get_cloud_localization_status(
                        ssh, ".rmtool"
                    )

                self.assertIs(status.package, expected)
                self.assertEqual(
                    status.state, _rmkit_cn.LocalizationState.NOT_INSTALLED
                )
                self.assertEqual(status.available_packages, (expected,))

    def test_shared_localized_qm_uses_exact_stock_backup_for_variant(self):
        package, ferrari, ferrari_stock, tatsu, tatsu_stock = (
            self.make_variant_packages()
        )
        for backup, expected in (
            (self.STOCK_QM, package),
            (ferrari_stock, ferrari),
            (tatsu_stock, tatsu),
        ):
            with self.subTest(platform=expected.platform):
                ssh = self.make_ssh(
                    config=b"[General]\nlanguage=fr_FR\n"
                )
                ssh.files.update(self.managed_files())
                ssh.files[_rmkit_cn.BACKUP_QM_PATH] = backup
                with patch.object(
                    _rmkit_cn,
                    "load_translation_catalog",
                    return_value={package.firmware: package},
                ):
                    status = _rmkit_cn.get_cloud_localization_status(
                        ssh, ".rmtool"
                    )

                self.assertIs(status.package, expected)
                self.assertEqual(status.state, _rmkit_cn.LocalizationState.ENABLED)
                self.assertNotIn(
                    ("exec", "systemctl stop xochitl"), ssh.events
                )

    def test_shared_legacy_qm_uses_exact_stock_backup_for_rm1_rm2(self):
        legacy_qm = b"legacy-localized-qm"
        common = {
            "firmware": _rmkit_cn.SUPPORTED_FIRMWARE,
            "localized_qm_sha256": hashlib.sha256(legacy_qm).hexdigest(),
            "asset": "reMarkable_zh_CN-20260612085811-legacy.qm",
            "size": len(legacy_qm),
            "release_version": "3.27.3.0",
            "channel": "stable",
        }
        rm1_stock = b"rm1-stock-carrier-qm"
        rm2_stock = b"rm2-stock-carrier-qm"
        rm1 = _rmkit_cn.TranslationPackage(
            stock_french_sha256=hashlib.sha256(rm1_stock).hexdigest(),
            platform="rm1",
            **common,
        )
        rm2 = _rmkit_cn.TranslationPackage(
            stock_french_sha256=hashlib.sha256(rm2_stock).hexdigest(),
            platform="rm2",
            **common,
        )
        root, *_existing = self.make_variant_packages()
        root = _rmkit_cn.TranslationPackage(
            firmware=root.firmware,
            stock_french_sha256=root.stock_french_sha256,
            localized_qm_sha256=root.localized_qm_sha256,
            asset=root.asset,
            size=root.size,
            release_version=root.release_version,
            channel=root.channel,
            platform=root.platform,
            variants=(*root.variants, rm1, rm2),
        )

        for stock, expected in ((rm1_stock, rm1), (rm2_stock, rm2)):
            with self.subTest(platform=expected.platform, carrier="stock"):
                ssh = self.make_ssh()
                ssh.files[_rmkit_cn.QM_PATH] = stock
                with patch.object(
                    _rmkit_cn,
                    "load_translation_catalog",
                    return_value={root.firmware: root},
                ):
                    status = _rmkit_cn.get_cloud_localization_status(
                        ssh, ".rmtool"
                    )
                self.assertIs(status.package, expected)

            with self.subTest(platform=expected.platform, carrier="localized"):
                ssh = self.make_ssh(config=b"[General]\nlanguage=fr_FR\n")
                ssh.files.update(self.managed_files(carrier=legacy_qm))
                ssh.files[_rmkit_cn.BACKUP_QM_PATH] = stock
                with patch.object(
                    _rmkit_cn,
                    "load_translation_catalog",
                    return_value={root.firmware: root},
                ):
                    status = _rmkit_cn.get_cloud_localization_status(
                        ssh, ".rmtool"
                    )
                self.assertIs(status.package, expected)
                self.assertEqual(status.state, _rmkit_cn.LocalizationState.ENABLED)

    def test_shared_localized_qm_requires_complete_backup(self):
        package, *_variants = self.make_variant_packages()
        ssh = self.make_ssh(config=b"[General]\nlanguage=fr_FR\n")
        ssh.files[_rmkit_cn.QM_PATH] = self.LOCALIZED_QM
        before = dict(ssh.files)

        with patch.object(
            _rmkit_cn,
            "load_translation_catalog",
            return_value={package.firmware: package},
        ), self.assertRaisesRegex(RuntimeError, "缺少完整备份"):
            _rmkit_cn.get_cloud_localization_status(ssh, ".rmtool")

        self.assertEqual(ssh.files, before)
        self.assertTrue(ssh.xochitl_active)
        self.assertNotIn(("exec", "systemctl stop xochitl"), ssh.events)
        self.assertFalse(any(kind == "transfer" for kind, _value in ssh.events))

    def test_variant_selection_rejects_unknown_carrier_and_stale_backup_read_only(self):
        package, *_variants = self.make_variant_packages()
        unknown = self.make_ssh()
        unknown.files[_rmkit_cn.QM_PATH] = b"unknown-carrier"
        stale = self.make_ssh(config=b"[General]\nlanguage=fr_FR\n")
        stale.files.update(self.managed_files())
        stale.files[_rmkit_cn.BACKUP_QM_PATH] = b"stale-stock-backup"

        for name, ssh in (("unknown", unknown), ("stale", stale)):
            with self.subTest(name=name):
                before = dict(ssh.files)
                with patch.object(
                    _rmkit_cn,
                    "load_translation_catalog",
                    return_value={package.firmware: package},
                ), self.assertRaises(RuntimeError):
                    _rmkit_cn.get_cloud_localization_status(ssh, ".rmtool")

                self.assertEqual(ssh.files, before)
                self.assertTrue(ssh.xochitl_active)
                self.assertNotIn(
                    ("exec", "systemctl stop xochitl"), ssh.events
                )
                self.assertFalse(
                    any(kind == "transfer" for kind, _value in ssh.events)
                )

    def test_unknown_carrier_and_wrong_local_qm_are_rejected_before_stop(self):
        for carrier, local_qm, message in (
            (b"unknown-carrier", self.LOCALIZED_QM, "均不匹配"),
            (self.STOCK_QM, b"wrong-localized-qm", "校验失败"),
        ):
            with self.subTest(message=message):
                ssh = self.make_ssh()
                ssh.files[_rmkit_cn.QM_PATH] = carrier
                before = dict(ssh.files)

                with self.assertRaisesRegex(RuntimeError, message):
                    _rmkit_cn.enable_localization(ssh, self.make_qm(local_qm))

                self.assertEqual(ssh.files, before)
                self.assertTrue(ssh.xochitl_active)
                self.assertFalse(any(kind == "transfer" for kind, _value in ssh.events))
                self.assertNotIn(
                    ("exec", "systemctl stop xochitl"),
                    ssh.events,
                )

    def test_restore_rejects_unknown_carrier_and_corrupt_backup_before_stop(self):
        for current_qm, backup_qm, missing_config, message in (
            (b"unknown-carrier", self.STOCK_QM, False, "均不匹配"),
            (self.LOCALIZED_QM, b"corrupt-backup", False, "均不匹配"),
            (self.LOCALIZED_QM, self.STOCK_QM, True, "备份不完整"),
        ):
            with self.subTest(message=message):
                ssh = self.make_ssh()
                ssh.files.update(self.managed_files(current_qm))
                ssh.files[_rmkit_cn.BACKUP_QM_PATH] = backup_qm
                if missing_config:
                    del ssh.files[_rmkit_cn.BACKUP_CONFIG_PATH]
                before = dict(ssh.files)

                with self.assertRaisesRegex(RuntimeError, message):
                    _rmkit_cn.restore_localization(ssh)

                self.assertEqual(ssh.files, before)
                self.assertTrue(ssh.xochitl_active)
                self.assertNotIn(
                    ("exec", "systemctl stop xochitl"),
                    ssh.events,
                )

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
