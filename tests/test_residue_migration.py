import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import rmtool  # noqa: F401  (must load before tab modules)
import _fast_mono_reading as fast
import _native_chinese as native
import _note_enhancements as note
import _pinyin_input as pinyin
import _reading_enhancements as reading
import _residue_migration
import _tap_page_turn as tap
import _weread_app as weread_app
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
            "note-enhancements": note,
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
            mock.patch.object(
                note,
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
            "note-enhancements": note,
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
            mock.patch.object(
                note,
                "extract_verified_package",
                side_effect=lambda _a, _p, dest: Path("extracted") / Path(dest).name,
            ).start()
            self.addCleanup(mock.patch.stopall)
            _residue_migration.migrate(ssh, "state-dir")

        self.assertTrue(migrate_shared.call_args.kwargs["tolerate_legacy_templates"])

    def test_migrate_prepares_appload_and_koreader_roots(self):
        ssh = mock.Mock()
        old_identity = _identity_for("3.27.1.0")
        new_identity = _identity_for("3.27.3.0")
        old_context = tap._trusted_shared_context(old_identity)
        new_context = tap._trusted_shared_context(new_identity)
        feature_ids = ("appload", "koreader")
        report = _residue_migration.ResidueReport(
            old_identity,
            new_identity,
            tuple(
                _residue_migration.ResidueFeatureReport(fid, fid, True, True)
                for fid in feature_ids
            ),
            True,
            (),
            "detail",
        )
        app_root = Path("prepared-appload")
        koreader_root = Path("prepared-koreader")
        with (
            mock.patch.object(_residue_migration, "inspect_residue", return_value=report),
            mock.patch.object(
                tap,
                "_trusted_shared_context",
                side_effect=[old_context, new_context],
            ),
            mock.patch.object(
                _residue_migration,
                "_prepare_appload_root",
                return_value=app_root,
            ) as prepare_app,
            mock.patch.object(
                _residue_migration,
                "_prepare_koreader_root",
                return_value=koreader_root,
            ) as prepare_koreader,
            mock.patch.object(
                _residue_migration.appload, "ensure_shim_links"
            ) as ensure_links,
            mock.patch.object(shared, "migrate_shared") as migrate_shared,
        ):
            _residue_migration.migrate(ssh, "state-dir")

        prepare_app.assert_called_once()
        prepare_koreader.assert_called_once()
        ensure_links.assert_called_once_with(ssh, new_identity)
        self.assertEqual(
            migrate_shared.call_args.args[5],
            {"appload": app_root, "koreader": koreader_root},
        )

    def test_migrate_skips_incompatible_appload_and_keeps_other_features(self):
        ssh = mock.Mock()
        old_identity = _identity_for("3.27.3.0")
        new_identity = _identity_for("3.28.0.172")
        old_context = tap._trusted_shared_context(old_identity)
        new_context = tap._trusted_shared_context(new_identity)
        report = _residue_migration.ResidueReport(
            old_identity,
            new_identity,
            (
                _residue_migration.ResidueFeatureReport(
                    "native-chinese", "独立简体中文", True, True
                ),
                _residue_migration.ResidueFeatureReport(
                    "appload", "AppLoad", True, False
                ),
                _residue_migration.ResidueFeatureReport(
                    "koreader", "KOReader", True, False
                ),
            ),
            True,
            (),
            "detail",
        )
        with (
            mock.patch.object(_residue_migration, "inspect_residue", return_value=report),
            mock.patch.object(
                tap,
                "_trusted_shared_context",
                side_effect=[old_context, new_context],
            ),
            mock.patch.object(
                native, "download_package", return_value=Path("archive.tar.gz")
            ),
            mock.patch.object(
                tap,
                "extract_verified_package",
                return_value=Path("extracted/native-chinese"),
            ),
            mock.patch.object(
                _residue_migration, "_prepare_appload_root"
            ) as prepare_app,
            mock.patch.object(
                _residue_migration, "_prepare_koreader_root"
            ) as prepare_koreader,
            mock.patch.object(shared, "migrate_shared") as migrate_shared,
            mock.patch.object(
                _residue_migration.appload, "remove_shim_links"
            ) as remove_links,
        ):
            _residue_migration.migrate(ssh, "state-dir")

        prepare_app.assert_not_called()
        prepare_koreader.assert_not_called()
        self.assertEqual(set(migrate_shared.call_args.args[4]), {"native-chinese"})
        self.assertEqual(set(migrate_shared.call_args.args[5]), {"native-chinese"})
        remove_links.assert_called_once_with(ssh, new_identity)

    def test_koreader_migration_rejects_unverified_icon(self):
        ssh = mock.Mock()
        ssh.download_file.side_effect = lambda _remote, local: Path(local).write_bytes(
            b"invalid"
        )
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            RuntimeError, "图标校验失败"
        ):
            _residue_migration._prepare_koreader_root(
                ssh,
                _identity_for("3.28.0.172"),
                tap._trusted_shared_context(_identity_for("3.27.3.0"))[1],
                "state-dir",
                Path(temporary) / "koreader",
            )

    def test_skipping_weread_or_qr_does_not_touch_apps_or_shim_links(self):
        for skipped in ("weread-launcher", "qr-upload"):
            ssh = mock.Mock()
            report = _residue_migration.ResidueReport(
                _identity_for("3.28.0.172"), _identity_for("3.29.0.152"),
                (_residue_migration.ResidueFeatureReport("native-chinese", "Chinese", True, True),
                 _residue_migration.ResidueFeatureReport(skipped, skipped, True, False)),
                True, (), "skipped entry, application data preserved",
            )
            with (
                mock.patch.object(_residue_migration, "inspect_residue", return_value=report),
                mock.patch.object(native, "download_package", return_value=Path("archive.tar.gz")),
                mock.patch.object(tap, "extract_verified_package", return_value=Path("extracted")),
                mock.patch.object(shared, "migrate_shared") as migrate_shared,
                mock.patch.object(_residue_migration.weread, "download_package") as launcher_download,
                mock.patch.object(_residue_migration.appload, "remove_shim_links") as remove_links,
            ):
                _residue_migration.migrate(ssh, "state-dir")
            self.assertEqual(set(migrate_shared.call_args.args[4]), {"native-chinese"})
            self.assertEqual(set(migrate_shared.call_args.args[5]), {"native-chinese"})
            remove_links.assert_not_called()
            launcher_download.assert_not_called()
            ssh.exec_checked.assert_not_called()

    def test_optional_skip_runs_through_detection_and_shared_transaction(self):
        cases = (
            ("3.27.3.0", "3.28.0.172", {"appload", "koreader"}, {"native-chinese"}),
            ("3.28.0.172", "3.29.0.152", {"qr-upload"}, {"native-chinese"}),
            ("3.28.0.172", "3.29.0.153", {"weread-launcher"}, {
                "native-chinese", "pinyin-input", "reading-enhancements",
                "note-enhancements", "qr-upload",
            }),
            ("3.28.0.172", "3.29.0.153", {"weread-launcher"}, set()),
        )
        for old_release, new_release, skipped, kept in cases:
            for failure in (None, "verify", "stage", "transaction"):
                with self.subTest(target=new_release, failure=failure), ExitStack() as stack:
                    old = _identity_for(old_release)
                    new = _identity_for(new_release)
                    old_runtime, old_trusted, _ = tap._trusted_shared_context(old)
                    new_runtime, new_trusted, _ = tap._trusted_shared_context(new)
                    residue = shared.SharedInspection({
                        fid: shared.SharedFeatureState(old_trusted[fid], True, "old-token")
                        for fid in kept | skipped
                    }, False, False)
                    ssh = mock.Mock()
                    ssh.exec_checked.return_value = ""
                    if failure == "transaction":
                        def execute(command):
                            if command.startswith("/bin/sh /tmp/rmtool-xovi-migrate-"):
                                raise RuntimeError("transaction failed")
                            return ""
                        ssh.exec_checked.side_effect = execute
                    stack.enter_context(mock.patch.object(tap, "get_device_identity", return_value=new))
                    stack.enter_context(mock.patch.object(shared, "has_shared_artifacts", return_value=True))
                    stack.enter_context(mock.patch.object(shared, "read_shared_identity", return_value=(
                        old.firmware, old.platform, old.architecture, old.xochitl_sha256,
                    )))
                    verify = stack.enter_context(mock.patch.object(
                        shared, "inspect_shared_firmware_residue", return_value=residue,
                    ))
                    if failure == "verify":
                        verify.side_effect = [residue, RuntimeError("residue invalid")]
                    for module in _residue_migration._providers().values():
                        stack.enter_context(mock.patch.object(
                            module, "download_package", return_value=Path("archive.tar.gz"),
                        ))
                        if hasattr(module, "extract_verified_package"):
                            stack.enter_context(mock.patch.object(
                                module, "extract_verified_package",
                                side_effect=lambda _archive, _package, destination: destination,
                            ))
                    stack.enter_context(mock.patch.object(shared, "_process_token", return_value=self.TOKEN))
                    stage = stack.enter_context(mock.patch.object(shared, "_stage_shared"))
                    if failure == "stage":
                        stage.side_effect = RuntimeError("staging failed")
                    upload = stack.enter_context(mock.patch.object(shared, "_upload_bytes"))
                    transaction = stack.enter_context(mock.patch.object(
                        shared, "shared_transaction_script", wraps=shared.shared_transaction_script,
                    ))
                    post_inspect = stack.enter_context(mock.patch.object(shared, "inspect_shared"))
                    remove_links = stack.enter_context(mock.patch.object(
                        _residue_migration.appload, "remove_shim_links",
                    ))
                    if failure:
                        message = {"verify": "residue invalid", "stage": "staging failed",
                                   "transaction": "transaction failed"}[failure]
                        with self.assertRaisesRegex(RuntimeError, message):
                            _residue_migration.migrate(ssh, "state-dir")
                        remove_links.assert_not_called()
                        post_inspect.assert_not_called()
                        if failure == "verify":
                            stage.assert_not_called()
                        if failure in ("verify", "stage"):
                            transaction.assert_not_called()
                            upload.assert_not_called()
                        if failure in ("stage", "transaction"):
                            commands = [call.args[0] for call in ssh.exec_checked.call_args_list]
                            self.assertTrue(any(command.startswith("rm -f /tmp/rmtool-xovi-migrate-")
                                                for command in commands))
                        continue
                    report = _residue_migration.migrate(ssh, "state-dir")
                    self.assertTrue(report.migratable)
                    self.assertEqual({item.feature_id for item in report.features
                                      if not item.target_available}, skipped)
                    self.assertEqual(verify.call_count, 2)
                    self.assertEqual(verify.call_args.args[1:3], (old_runtime, old_trusted))
                    self.assertEqual(stage.call_args.args[1], new_runtime)
                    states = stage.call_args.args[2]
                    self.assertEqual(set(states), kept)
                    self.assertEqual(set(stage.call_args.args[3]), kept)
                    for fid, state in states.items():
                        self.assertEqual(state.spec, new_trusted[fid])
                        self.assertTrue(state.enabled)
                    self.assertEqual(transaction.call_args.kwargs["enable_dropin"], bool(kept))
                    script = upload.call_args.args[1].decode()
                    self.assertIn("trap rollback EXIT INT TERM", script)
                    for external in (
                        weread_app.INSTALL_ROOT,
                        _residue_migration.koreader.OFFICIAL_INSTALL_DIR,
                        _residue_migration.koreader.APPLOAD_INSTALL_DIR,
                        _residue_migration.koreader.XOCHITL_ROOT,
                    ):
                        self.assertNotIn(external, script)
                    post_inspect.assert_called_once()
                    if "appload" in skipped:
                        remove_links.assert_called_once_with(ssh, new)
                    else:
                        remove_links.assert_not_called()

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

        remove.assert_called_once()
        call = remove.call_args
        self.assertEqual(call.args[:4], (
            ssh,
            self.old_context[0],
            self.old_context[1],
            (
                self.new_identity.firmware,
                self.new_identity.platform,
                self.new_identity.architecture,
                self.new_identity.xochitl_sha256,
            ),
        ))
        self.assertTrue(call.kwargs["tolerate_legacy_templates"])
        self.assertIn("trusted_alternatives", call.kwargs)
        self.assertTrue(call.kwargs["trusted_alternatives"])

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

    def _run(self, residue, new_features, roots, *, skipped_feature_ids=()):
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
                skipped_feature_ids=skipped_feature_ids,
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

    def test_migrate_skips_only_explicit_optional_entries(self):
        for feature_id, old_release, new_release in (
            ("appload", "3.27.3.0", "3.28.0.172"),
            ("koreader", "3.27.3.0", "3.28.0.172"),
            ("weread-launcher", "3.28.0.172", "3.29.0.153"),
            ("qr-upload", "3.28.0.172", "3.29.0.152"),
        ):
            with self.subTest(feature=feature_id):
                self.old_runtime, self.old_trusted, _ = tap._trusted_shared_context(
                    _identity_for(old_release)
                )
                self.new_runtime, self.new_trusted, _ = tap._trusted_shared_context(
                    _identity_for(new_release)
                )
                residue = self._residue({"native-chinese": True, feature_id: True})
                target = {"native-chinese": self.new_trusted["native-chinese"]}
                roots = {"native-chinese": Path("roots/native-chinese")}
                with self.assertRaisesRegex(RuntimeError, feature_id):
                    self._run(residue, target, roots)
                _result, stage = self._run(
                    residue, target, roots, skipped_feature_ids=(feature_id,),
                )
                self.assertEqual(set(stage.call_args.args[2]), {"native-chinese"})
                self.assertEqual(stage.call_args.args[3], roots)
                with self.assertRaisesRegex(RuntimeError, "解包目录不一致"):
                    self._run(
                        residue, target, {**roots, feature_id: Path("unexpected-root")},
                        skipped_feature_ids=(feature_id,),
                    )

    def test_migrate_cannot_skip_core_unknown_or_targeted_features(self):
        enabled = {fid: True for fid in self.old_trusted}
        for feature_id in ("pinyin-input", "unknown-plugin", "appload"):
            with self.subTest(feature=feature_id):
                target = dict(self.new_trusted)
                if feature_id == "pinyin-input":
                    del target[feature_id]
                elif feature_id == "appload":
                    target[feature_id] = tap._trusted_shared_context(
                        _identity_for("3.27.3.0")
                    )[1][feature_id]
                with self.assertRaisesRegex(RuntimeError, "跳过"):
                    self._run(
                        self._residue(enabled), target,
                        {fid: Path("roots") / fid for fid in target},
                        skipped_feature_ids=(feature_id,),
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
