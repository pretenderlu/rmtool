import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import rmtool  # noqa: F401  (must load before tab modules)
import _fast_mono_reading as fast
import _native_chinese as native
import _pinyin_input as pinyin
import _reading_enhancements as reading
import _residue_migration
import _tap_page_turn as tap
import _xovi_standalone as shared
from tests.test_rmtool_behaviors import FakeConnectionClient  # noqa: F401


def _identity_for(release: str, platform: str = "chiappa") -> tap.DeviceIdentity:
    package = next(
        item
        for item in tap._trusted_catalog()
        if item.release_version == release and item.platform == platform
    )
    return tap.DeviceIdentity(
        package.firmware, package.platform, package.architecture, package.xochitl_sha256
    )


class ResidueMigrationTests(unittest.TestCase):
    TOKEN = "t" * 32

    def setUp(self):
        self.old_identity = _identity_for("3.28.0.166")
        self.new_identity = _identity_for("3.28.0.169")
        self.old_context = tap._trusted_shared_context(self.old_identity)
        self.new_context = tap._trusted_shared_context(self.new_identity)

    def _residue(self, enabled: dict) -> shared.SharedInspection:
        states = {
            feature_id: shared.SharedFeatureState(spec, value, "old-token")
            for feature_id, value in enabled.items()
            for spec in [self.old_context[1][feature_id]]
        }
        return shared.SharedInspection(states, False, False)

    def test_inspect_returns_none_without_shared_artifacts(self):
        ssh = mock.Mock()
        with (
            mock.patch.object(tap, "get_device_identity", return_value=self.new_identity),
            mock.patch.object(shared, "has_shared_artifacts", return_value=False),
        ):
            self.assertIsNone(_residue_migration.inspect_residue(ssh))

    def test_inspect_reports_migratable_residue(self):
        ssh = mock.Mock()
        enabled = {fid: True for fid in self.old_context[1]}
        with (
            mock.patch.object(tap, "get_device_identity", return_value=self.new_identity),
            mock.patch.object(shared, "has_shared_artifacts", return_value=True),
            mock.patch.object(
                shared,
                "read_shared_identity",
                return_value=(
                    self.old_identity.firmware,
                    self.old_identity.platform,
                    self.old_identity.architecture,
                    self.old_identity.xochitl_sha256,
                ),
            ),
            mock.patch.object(
                tap,
                "_trusted_shared_context",
                side_effect=[self.old_context, self.new_context],
            ),
            mock.patch.object(
                shared,
                "inspect_shared_firmware_residue",
                return_value=self._residue(enabled),
            ) as residue_check,
        ):
            report = _residue_migration.inspect_residue(ssh)

        self.assertTrue(report.migratable)
        self.assertEqual(report.old_identity, self.old_identity)
        self.assertEqual(report.new_identity, self.new_identity)
        self.assertEqual(len(report.features), len(enabled))
        self.assertTrue(all(item.target_available for item in report.features))
        self.assertEqual(
            residue_check.call_args.args[3],
            (
                self.new_identity.firmware,
                self.new_identity.platform,
                self.new_identity.architecture,
                self.new_identity.xochitl_sha256,
            ),
        )

    def test_inspect_blocks_when_new_firmware_has_no_packages(self):
        ssh = mock.Mock()
        enabled = {fid: True for fid in self.old_context[1]}
        with (
            mock.patch.object(tap, "get_device_identity", return_value=self.new_identity),
            mock.patch.object(shared, "has_shared_artifacts", return_value=True),
            mock.patch.object(
                shared,
                "read_shared_identity",
                return_value=(
                    self.old_identity.firmware,
                    self.old_identity.platform,
                    self.old_identity.architecture,
                    self.old_identity.xochitl_sha256,
                ),
            ),
            mock.patch.object(
                tap,
                "_trusted_shared_context",
                side_effect=[
                    self.old_context,
                    RuntimeError("内置点击翻页清单没有当前设备的精确包。"),
                ],
            ),
            mock.patch.object(
                shared,
                "inspect_shared_firmware_residue",
                return_value=self._residue(enabled),
            ),
        ):
            report = _residue_migration.inspect_residue(ssh)

        self.assertFalse(report.migratable)
        self.assertTrue(report.blockers)
        self.assertIn("没有当前设备的精确包", report.blockers[0])

    def test_inspect_falls_back_to_tolerant_for_dev_launchers(self):
        ssh = mock.Mock()
        enabled = {fid: True for fid in self.old_context[1]}
        legacy_residue = shared.SharedInspection(
            self._residue(enabled).states, False, False, False,
            shared.SHARED_LAYOUT, True,
        )
        with (
            mock.patch.object(tap, "get_device_identity", return_value=self.new_identity),
            mock.patch.object(shared, "has_shared_artifacts", return_value=True),
            mock.patch.object(
                shared,
                "read_shared_identity",
                return_value=(
                    self.old_identity.firmware,
                    self.old_identity.platform,
                    self.old_identity.architecture,
                    self.old_identity.xochitl_sha256,
                ),
            ),
            mock.patch.object(
                tap,
                "_trusted_shared_context",
                side_effect=[self.old_context, self.new_context],
            ),
            mock.patch.object(
                shared,
                "inspect_shared_firmware_residue",
                side_effect=[
                    RuntimeError("共享 Xovi 标记与内置信任清单不匹配。"),
                    legacy_residue,
                ],
            ) as residue_check,
        ):
            report = _residue_migration.inspect_residue(ssh)

        self.assertTrue(report.migratable)
        self.assertTrue(report.legacy_templates)
        self.assertIn("开发期", report.detail)
        calls = residue_check.call_args_list
        self.assertFalse(calls[0].kwargs.get("tolerate_legacy_templates", False))
        self.assertTrue(calls[1].kwargs.get("tolerate_legacy_templates"))

    def test_inspect_reports_blocked_when_even_tolerant_fails(self):
        ssh = mock.Mock()
        with (
            mock.patch.object(tap, "get_device_identity", return_value=self.new_identity),
            mock.patch.object(shared, "has_shared_artifacts", return_value=True),
            mock.patch.object(
                shared,
                "read_shared_identity",
                return_value=(
                    self.old_identity.firmware,
                    self.old_identity.platform,
                    self.old_identity.architecture,
                    self.old_identity.xochitl_sha256,
                ),
            ),
            mock.patch.object(tap, "_trusted_shared_context", return_value=self.old_context),
            mock.patch.object(
                shared,
                "inspect_shared_firmware_residue",
                side_effect=RuntimeError("共享 Xovi 标记与内置信任清单不匹配。"),
            ),
        ):
            report = _residue_migration.inspect_residue(ssh)

        self.assertFalse(report.migratable)
        self.assertFalse(report.legacy_templates)
        self.assertIn("不能自动迁移", report.detail)
        self.assertIn("不能自动清理", report.detail)

    def test_migrate_refuses_when_not_migratable(self):
        ssh = mock.Mock()
        report = _residue_migration.ResidueReport(
            self.old_identity,
            self.new_identity,
            (),
            False,
            ("阻断：测试",),
            "残留已验证，但存在阻断项，暂不能一键迁移。",
        )
        with mock.patch.object(
            _residue_migration, "inspect_residue", return_value=report
        ):
            with self.assertRaisesRegex(RuntimeError, "阻断：测试"):
                _residue_migration.migrate(ssh, "state-dir")

    def test_migrate_fetches_every_enabled_feature_and_delegates(self):
        ssh = mock.Mock()
        enabled = {fid: True for fid in self.old_context[1]}
        report = _residue_migration.ResidueReport(
            self.old_identity,
            self.new_identity,
            tuple(
                _residue_migration.ResidueFeatureReport(fid, fid, True, True)
                for fid in self.old_context[1]
            ),
            True,
            (),
            "detail",
        )
        providers = {
            "tap-page-turn": tap,
            "fast-mono-reading": fast,
            "native-chinese": native,
            "pinyin-input": pinyin,
            "reading-enhancements": reading,
        }
        with (
            mock.patch.object(_residue_migration, "inspect_residue", return_value=report),
            mock.patch.object(
                tap,
                "_trusted_shared_context",
                side_effect=[self.old_context, self.new_context],
            ),
            mock.patch.object(shared, "migrate_shared") as migrate_shared,
        ):
            for feature_id, module in providers.items():
                mock.patch.object(
                    module, "download_package", return_value=Path("archive.tar.gz")
                ).start()
            # tap/fast/reading own extractors; native/pinyin fall back to tap's.
            mock.patch.object(
                tap,
                "extract_verified_package",
                side_effect=lambda _a, _p, dest: Path("extracted") / Path(dest).name,
            ).start()
            mock.patch.object(
                fast,
                "extract_verified_package",
                side_effect=lambda _a, _p, dest: Path("extracted") / Path(dest).name,
            ).start()
            mock.patch.object(
                reading,
                "extract_verified_package",
                side_effect=lambda _a, _p, dest: Path("extracted") / Path(dest).name,
            ).start()
            self.addCleanup(mock.patch.stopall)
            _residue_migration.migrate(ssh, "state-dir")

        call = migrate_shared.call_args
        self.assertEqual(
            set(call.args[4]),
            set(self.old_context[1]),
        )
        for feature_id, spec in call.args[4].items():
            self.assertEqual(spec, self.new_context[1][feature_id])
        self.assertEqual(set(call.args[5]), set(self.old_context[1]))
        self.assertEqual(call.args[1], self.old_context[0])
        self.assertEqual(call.args[3], self.new_context[0])
        self.assertFalse(call.kwargs.get("tolerate_legacy_templates", False))

    def test_migrate_delegates_template_tolerance(self):
        ssh = mock.Mock()
        report = _residue_migration.ResidueReport(
            self.old_identity,
            self.new_identity,
            tuple(
                _residue_migration.ResidueFeatureReport(fid, fid, True, True)
                for fid in self.old_context[1]
            ),
            True,
            (),
            "detail",
            True,
        )
        providers = {
            "tap-page-turn": tap,
            "fast-mono-reading": fast,
            "native-chinese": native,
            "pinyin-input": pinyin,
            "reading-enhancements": reading,
        }
        with (
            mock.patch.object(_residue_migration, "inspect_residue", return_value=report),
            mock.patch.object(
                tap,
                "_trusted_shared_context",
                side_effect=[self.old_context, self.new_context],
            ),
            mock.patch.object(shared, "migrate_shared") as migrate_shared,
        ):
            for feature_id, module in providers.items():
                mock.patch.object(
                    module, "download_package", return_value=Path("archive.tar.gz")
                ).start()
            mock.patch.object(
                tap,
                "extract_verified_package",
                side_effect=lambda _a, _p, dest: Path("extracted") / Path(dest).name,
            ).start()
            mock.patch.object(
                fast,
                "extract_verified_package",
                side_effect=lambda _a, _p, dest: Path("extracted") / Path(dest).name,
            ).start()
            mock.patch.object(
                reading,
                "extract_verified_package",
                side_effect=lambda _a, _p, dest: Path("extracted") / Path(dest).name,
            ).start()
            self.addCleanup(mock.patch.stopall)
            _residue_migration.migrate(ssh, "state-dir")

        self.assertTrue(migrate_shared.call_args.kwargs["tolerate_legacy_templates"])

    def test_cleanup_removes_verified_residue_and_preserves_template_tolerance(self):
        ssh = mock.Mock()
        report = _residue_migration.ResidueReport(
            self.old_identity,
            self.new_identity,
            (
                _residue_migration.ResidueFeatureReport(
                    "tap-page-turn", "点击翻页", True, False
                ),
            ),
            False,
            ("当前固件没有精确包",),
            "残留已验证，但不能迁移。",
            True,
        )
        with (
            mock.patch.object(
                _residue_migration, "inspect_residue", return_value=report
            ),
            mock.patch.object(
                tap, "_trusted_shared_context", return_value=self.old_context
            ),
            mock.patch.object(
                shared, "remove_shared_firmware_residue"
            ) as remove,
        ):
            self.assertIs(_residue_migration.cleanup(ssh), report)

        remove.assert_called_once_with(
            ssh,
            self.old_context[0],
            self.old_context[1],
            (
                self.new_identity.firmware,
                self.new_identity.platform,
                self.new_identity.architecture,
                self.new_identity.xochitl_sha256,
            ),
            tolerate_legacy_templates=True,
        )

    def test_cleanup_rejects_unverified_residue_before_mutation(self):
        report = _residue_migration.ResidueReport(
            self.old_identity,
            self.new_identity,
            (),
            False,
            ("无法验证",),
            "残留无法验证，不能自动清理。",
        )
        remove = mock.Mock()
        with (
            mock.patch.object(
                _residue_migration, "inspect_residue", return_value=report
            ),
            mock.patch.object(shared, "remove_shared_firmware_residue", remove),
            self.assertRaisesRegex(RuntimeError, "不能自动清理"),
        ):
            _residue_migration.cleanup(mock.Mock())
        remove.assert_not_called()


class MigrateSharedTests(unittest.TestCase):
    TOKEN = "n" * 32

    def setUp(self):
        old_identity = _identity_for("3.28.0.166")
        new_identity = _identity_for("3.28.0.169")
        self.old_runtime, self.old_trusted, _legacy = tap._trusted_shared_context(
            old_identity
        )
        self.new_runtime, self.new_trusted, _legacy = tap._trusted_shared_context(
            new_identity
        )

    def _residue(self, enabled: dict) -> shared.SharedInspection:
        states = {
            feature_id: shared.SharedFeatureState(spec, value, "old-token")
            for feature_id, value in enabled.items()
            for spec in [self.old_trusted[feature_id]]
        }
        return shared.SharedInspection(states, False, False)

    def _run(self, residue, new_features, roots):
        ssh = mock.Mock()
        ssh.exec_checked.return_value = ""
        final = shared.SharedInspection({}, False, True)
        stage = mock.Mock(return_value=("1" * 64, "2" * 64))
        with (
            mock.patch.object(shared, "validate_legacy", return_value=False),
            mock.patch.object(shared, "_process_token", return_value=self.TOKEN),
            mock.patch.object(
                shared, "inspect_shared_firmware_residue", return_value=residue
            ),
            mock.patch.object(shared, "_stage_shared", stage),
            mock.patch.object(
                shared, "shared_transaction_script", return_value="#!/bin/sh\n:"
            ),
            mock.patch.object(shared, "_upload_bytes"),
            mock.patch.object(shared, "inspect_shared", return_value=final),
        ):
            result = shared.migrate_shared(
                ssh,
                self.old_runtime,
                self.old_trusted,
                self.new_runtime,
                new_features,
                roots,
            )
        return result, stage

    def test_migrate_rebuilds_every_enabled_feature(self):
        enabled = {fid: True for fid in self.old_trusted}
        roots = {fid: Path("roots") / fid for fid in enabled}
        result, stage = self._run(
            self._residue(enabled),
            {fid: self.new_trusted[fid] for fid in enabled},
            roots,
        )
        self.assertTrue(result.dropin_present)
        states = stage.call_args.args[2]
        self.assertEqual(set(states), set(enabled))
        for feature_id, state in states.items():
            self.assertTrue(state.enabled)
            self.assertEqual(state.spec, self.new_trusted[feature_id])
            self.assertEqual(state.process_token, self.TOKEN)
        self.assertEqual(stage.call_args.args[3], roots)

    def test_migrate_preserves_disabled_states(self):
        enabled = {fid: fid != "pinyin-input" for fid in self.old_trusted}
        roots = {fid: Path("roots") / fid for fid in enabled if enabled[fid]}
        _result, stage = self._run(
            self._residue(enabled),
            {fid: self.new_trusted[fid] for fid in enabled},
            roots,
        )
        states = stage.call_args.args[2]
        self.assertFalse(states["pinyin-input"].enabled)
        self.assertEqual(states["pinyin-input"].process_token, "old-token")
        self.assertTrue(states["tap-page-turn"].enabled)

    def test_migrate_refuses_enabled_feature_without_target(self):
        enabled = {fid: True for fid in self.old_trusted}
        roots = {
            fid: Path("roots") / fid
            for fid in enabled
            if fid != "pinyin-input"
        }
        with self.assertRaisesRegex(RuntimeError, "pinyin-input"):
            self._run(
                self._residue(enabled),
                {fid: self.new_trusted[fid] for fid in roots},
                roots,
            )

    def test_migrate_requires_roots_for_every_enabled_feature(self):
        enabled = {fid: True for fid in self.old_trusted}
        roots = {fid: Path("roots") / fid for fid in enabled if fid != "pinyin-input"}
        with self.assertRaisesRegex(RuntimeError, "解包目录不一致"):
            self._run(
                self._residue(enabled),
                {fid: self.new_trusted[fid] for fid in enabled},
                roots,
            )

    def test_migrate_passes_template_tolerance_to_residue_inspection(self):
        enabled = {fid: True for fid in self.old_trusted}
        roots = {fid: Path("roots") / fid for fid in enabled}
        ssh = mock.Mock()
        ssh.exec_checked.return_value = ""
        stage = mock.Mock(return_value=("1" * 64, "2" * 64))
        with (
            mock.patch.object(shared, "validate_legacy", return_value=False),
            mock.patch.object(shared, "_process_token", return_value=self.TOKEN),
            mock.patch.object(
                shared, "inspect_shared_firmware_residue",
                return_value=self._residue(enabled),
            ) as inspect,
            mock.patch.object(shared, "_stage_shared", stage),
            mock.patch.object(
                shared, "shared_transaction_script", return_value="#!/bin/sh\n:"
            ),
            mock.patch.object(shared, "_upload_bytes"),
            mock.patch.object(
                shared, "inspect_shared", return_value=shared.SharedInspection({}, False, True)
            ),
        ):
            shared.migrate_shared(
                ssh,
                self.old_runtime,
                self.old_trusted,
                self.new_runtime,
                {fid: self.new_trusted[fid] for fid in enabled},
                roots,
                tolerate_legacy_templates=True,
            )
        self.assertTrue(
            inspect.call_args.kwargs["tolerate_legacy_templates"]
        )


if __name__ == "__main__":
    unittest.main()
