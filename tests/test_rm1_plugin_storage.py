import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import _native_chinese as native
import _plugin_recovery as recovery
import _tap_page_turn as tap
import _xovi_standalone as shared
from tests import test_xovi_standalone as fixtures
from tests import test_plugin_recovery as recovery_fixtures


class RM1PluginStorageTests(unittest.TestCase):
    def setUp(self):
        self.package = next(p for p in native._trusted_catalog()
                            if p.platform == "rm1" and p.release_version == "3.28.0.172")
        self.runtime, self.feature = native._shared_specs(self.package)
        self.states = {self.feature.feature_id: shared.SharedFeatureState(
            self.feature, True, fixtures.SharedXoviTests.TOKEN)}
        self.home = shared.LEGACY_SHARED_LAYOUT

    def device(self, **kwargs):
        return fixtures.SharedXoviTests().shared_residue_ssh(
            self.package, None, feature_ids=(self.feature.feature_id,),
            visible_dropins=(shared.SHARED_LAYOUT.dropin_path,), **kwargs)

    def test_only_rm1_uses_home_and_retains_startup_safety(self):
        self.assertEqual(shared.preferred_layout(replace(self.runtime, firmware="20260612085811")), shared.SHARED_LAYOUT)
        for platform in ("rm1", "rm2", "ferrari", "chiappa", "tatsu"):
            runtime = replace(self.runtime, platform=platform)
            layout = shared.preferred_layout(runtime)
            self.assertEqual(layout, self.home if platform == "rm1" else shared.SHARED_LAYOUT)
            launcher = shared.shared_launcher(runtime, (self.feature,))
            dropin = shared.shared_dropin(runtime, (self.feature,))
            self.assertIn(layout.remote_base, launcher)
            self.assertIn(layout.remote_base + "/startup.pending", launcher)
            self.assertIn(shared.SHARED_RECOVERY_SENTINEL, launcher)
            self.assertIn(shared.LEGACY_RECOVERY_SENTINEL, launcher)
            self.assertIn("else exec /usr/bin/xochitl --system", dropin)
            self.assertNotIn("ConditionPathExists", dropin)

    def test_existing_data_and_new_home_are_both_strictly_verifiable(self):
        for layout in (shared.SHARED_LAYOUT, self.home):
            with self.subTest(layout=layout.remote_base):
                ssh, _, runtime, trusted, _ = self.device(layout=layout)
                result = shared.inspect_shared(ssh, runtime, trusted)
                self.assertEqual(result.layout, layout)
                marker = json.loads(
                    shared._remote_text(ssh, layout.remote_base + "/package.json"))
                self.assertEqual(recovery._recognized_marker(marker, runtime, trusted, {}, layout), result.states)
                ssh.transfer_file.assert_not_called()

    def test_arm_translator_payload_and_published_predecessor(self):
        binary = (Path(native.__file__).parent / "native-chinese/native-chinese-translator-armv7.so").read_bytes()
        self.assertEqual((len(binary), hashlib.sha256(binary).hexdigest()), native.ARM_TRANSLATOR)
        self.assertEqual(binary[:6], b"\x7fELF\x01\x01")
        self.assertEqual(int.from_bytes(binary[18:20], "little"), 40)
        for base in (self.home.remote_base, shared.SHARED_LAYOUT.remote_base):
            self.assertIn((base + "/" + native.CATALOG_PATH).encode("utf-16le"), binary)
        self.assertIn(b"XOVI_ROOT", binary)
        self.assertNotIn("/home/root/.local/share/rmtool/native-chinese/".encode("utf-16le"), binary)
        old = native._known_shared_predecessor_specs(self.package)[0]
        contexts = recovery._trusted_cleanup_contexts(native._package_identity(self.package))
        self.assertTrue(any(runtime == self.runtime and trusted.get(native.FEATURE_ID) == old.feature
                            for runtime, trusted in contexts))
        for schema in (1, 2):
            ssh, _, runtime, trusted, _ = self.device(
                feature_overrides={native.FEATURE_ID: old.feature}, marker_schema=schema)
            result, _, revisions = native._inspect_shared_revision(ssh, runtime, trusted, self.package)
            self.assertEqual(revisions[native.FEATURE_ID], "catalog_runtime_path")
            with self.assertRaisesRegex(RuntimeError, "同步更新"):
                shared._stage_shared(ssh, runtime, result.states, {}, {}, "/unused")
            ssh.transfer_file.assert_not_called()
            shared._assert_home_catalog(runtime, {
                fid: replace(state, enabled=False) for fid, state in result.states.items()})

    def test_home_payload_tampering_and_mixed_layouts_still_refuse(self):
        ssh, _, runtime, trusted, _ = self.device(layout=self.home, modified_path="xovi.so")
        with self.assertRaises(RuntimeError):
            shared.inspect_shared(ssh, runtime, trusted)
        ssh, present, runtime, trusted, _ = self.device(layout=self.home)
        present.add(shared.SHARED_LAYOUT.remote_base)
        with self.assertRaisesRegex(RuntimeError, "两套"):
            shared.inspect_shared(ssh, runtime, trusted)
        ssh.transfer_file.assert_not_called()

    def test_rm1_capacity_uses_home_but_requires_small_root_headroom(self):
        for home_kib, root_kib, error in ((100000, 2048, None), (0, 2048, "/home"),
                                          (100000, 0, "/ 空间不足"), ("?", 2048, "无法确认")):
            ssh = Mock()
            ssh.exec_checked.side_effect = lambda command: (
                "" if command == "mountpoint -q /home" else
                str(home_kib if command.startswith("df -Pk /home") else root_kib))
            if error:
                with self.assertRaisesRegex(RuntimeError, error):
                    shared._stage_shared(ssh, self.runtime, self.states, {}, {}, "/home/stage")
            else:
                shared.check_shared_capacity(ssh, self.runtime, self.states)
            self.assertFalse(any("df -Pk /data" in call.args[0] for call in ssh.exec_checked.call_args_list))
            ssh.transfer_file.assert_not_called()
        ssh = Mock()
        ssh.exec_checked.side_effect = RuntimeError("home is not mounted")
        with self.assertRaisesRegex(RuntimeError, "not mounted"):
            shared._stage_shared(ssh, self.runtime, self.states, {}, {}, "/home/stage")
        ssh.transfer_file.assert_not_called()

    def test_rm1_enable_and_remove_stage_and_commit_in_home(self):
        ssh = Mock()
        ssh.file_exists.return_value = False
        ssh.exec_checked.return_value = ""
        trusted = {self.feature.feature_id: self.feature}
        old = shared.SharedInspection(self.states, False, True, layout=shared.SHARED_LAYOUT)
        for operation in ("install", "remove"):
            scripts = []
            with self.subTest(operation=operation), patch.object(shared, "_assert_managed_dropins"), patch.object(
                shared, "_process_token", return_value=fixtures.SharedXoviTests.TOKEN
            ), patch.object(shared, "_operation_lock"), patch.object(
                shared, "inspect_shared", return_value=old
            ), patch.object(shared, "_stage_shared") as stage, patch.object(
                shared, "_upload_bytes", side_effect=lambda client, data, path, mode: scripts.append(data.decode())
            ), patch.object(shared, "has_shared_artifacts", return_value=operation == "remove"):
                if operation == "install":
                    shared._enable_shared_locked(ssh, self.runtime, self.feature, Path("unused"), trusted, ())
                else:
                    shared.remove_shared_features(ssh, self.runtime, trusted, trusted)
                self.assertTrue(stage.call_args.args[-1].startswith(self.home.remote_base + ".staging-"))
                self.assertIn("BASE=" + self.home.remote_base, scripts[-1])
                self.assertIn("BACKUP_DIR=" + str(Path(self.home.remote_base).parent).replace("\\", "/"), scripts[-1])
                if operation == "remove":
                    self.assertIn(shared.SHARED_LAYOUT.remote_base + ".backup-", scripts[-1])

    def test_cross_filesystem_migration_renames_backups_on_source_filesystem(self):
        script = shared.shared_transaction_script(
            self.home.remote_base + ".staging-test", "test", (shared.SHARED_LAYOUT,),
            enable_dropin=True, retain_backup=True, layout=self.home)
        self.assertIn("mv /data/rmtool/xovi-standalone /data/rmtool/xovi-standalone.backup-test", script)
        self.assertNotIn("mv /data/rmtool/xovi-standalone /home/", script)

    def test_recovery_accepts_home_and_can_clean_only_verified_incomplete_files(self):
        with patch.object(recovery_fixtures, "BASE", self.home.remote_base):
            device = recovery_fixtures.Device()
            device.identity = tap.DeviceIdentity(self.runtime.firmware, self.runtime.platform,
                                                 self.runtime.architecture, self.runtime.xochitl_sha256)
            device.runtime = self.runtime
            device.layout = self.home
            device.entries.clear()
            device.mountinfo = "1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 8:7 / /home rw - ext4 /dev/mmcblk1p7 rw\n"
            device.install(self.states)
            with patch.object(tap, "get_device_identity", return_value=device.identity), patch.object(
                recovery, "_lower_dropin_snapshot", return_value=None
            ), patch.object(recovery, "_trusted_cleanup_contexts", return_value=((self.runtime, {self.feature.feature_id: self.feature}),)):
                report = recovery.inspect_recovery(device)
                self.assertEqual(report.state, recovery.RecoveryState.NOT_NEEDED, report.detail)
                report = recovery.inspect_incomplete(device)
                self.assertEqual(report.state, recovery.RecoveryState.NOT_NEEDED, report.detail)
                device.entries.pop(self.home.remote_base + "/package.json")
                report, plan = recovery._inspect_incomplete(device)
                self.assertEqual(report.state, recovery.RecoveryState.CLEANUP_AVAILABLE, report.detail)
                self.assertEqual(plan.layout, self.home)
                self.assertIn("BASE=" + self.home.remote_base, recovery._incomplete_cleanup_script(plan, "test"))
                device.add(self.home.remote_base + "/unknown.so", b"unknown")
                report = recovery.inspect_incomplete(device)
                self.assertEqual(report.state, recovery.RecoveryState.BLOCKED)
                self.assertIn("未知文件", report.detail)


if __name__ == "__main__":
    unittest.main()
