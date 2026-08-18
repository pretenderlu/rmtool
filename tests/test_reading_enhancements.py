import contextlib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import _fast_mono_reading as fast
import _reading_enhancements as reading
import _tap_page_turn as tap
import _xovi_standalone as shared


class ReadingEnhancementsBackendTests(unittest.TestCase):
    def setUp(self):
        self.catalog = reading.parse_manifest(
            Path("reading-enhancements/manifest.json").read_bytes(),
            require_local_match=False,
        )
        self.package = self.catalog[0]
        self.identity = tap.DeviceIdentity(
            self.package.firmware,
            self.package.platform,
            self.package.architecture,
            self.package.xochitl_sha256,
        )
        self.runtime, self.feature = reading._shared_specs(self.package)
        self.process = "12345678-1234-1234-1234-123456789abc:1:1"

    def _trusted(self):
        return {reading.FEATURE_ID: self.feature}

    def _state(self, spec, enabled=True, token=None):
        return shared.SharedFeatureState(spec, enabled, token or self.process)

    def test_manifest_is_exact_fourteen_target_matrix(self):
        self.assertEqual(
            len(self.catalog),
            14,
        )
        self.assertEqual(
            {
                (item.platform, item.firmware, item.xochitl_sha256)
                for item in self.catalog
            },
            {(key[0], key[1], key[3]) for key in reading.ALLOWED_TARGETS},
        )
        self.assertEqual(
            len({item.asset for item in self.catalog}),
            14,
        )
        self.assertEqual(
            len({(item.release_version, item.platform) for item in self.catalog}),
            14,
        )
        self.assertTrue(all(item.package_revision == 1 for item in self.catalog))
        self.assertTrue(all(item.offline_verified for item in self.catalog))
        self.assertEqual(
            {
                (item.platform, item.release_version, item.firmware)
                for item in self.catalog
                if item.device_verified
            },
            {("chiappa", "3.27.3.0", "20260612085811")},
        )
        self.assertEqual(
            set(item.path for item in self.package.files), reading._PAYLOAD_PATHS
        )
        for package in self.catalog:
            self.assertEqual(
                package.asset,
                reading._expected_asset_name(
                    package.platform, package.firmware, package.release_version
                ),
            )
            self.assertEqual(
                package.urls,
                tuple(f"{base}/{package.asset}" for base in reading.REMOTE_BASE_URLS),
            )

    def test_manifest_rejects_changed_url_order_and_extra_fields(self):
        document = json.loads(
            Path("reading-enhancements/manifest.json").read_text(encoding="utf-8")
        )
        changed_order = json.loads(json.dumps(document))
        changed_order["packages"][0]["urls"].reverse()
        extra_field = json.loads(json.dumps(document))
        extra_field["packages"][0]["unexpected"] = True
        for changed in (changed_order, extra_field):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                reading.parse_manifest(json.dumps(changed).encode(), require_local_match=False)

    def test_known_defective_predecessors_are_exact_and_release_bounded(self):
        expected = {
            "3.27.1.0": ("0cdfcff0c43e1cc87eb0e956b808c6e588d5dec0791d878bbc6bf7012ed2fa77", 27749),
            "3.27.3.0": ("0cdfcff0c43e1cc87eb0e956b808c6e588d5dec0791d878bbc6bf7012ed2fa77", 27749),
            "3.28.0.162": ("b87d847492ef5efaaf81dfcae6294ed2db61db335d79484a668f2f6fa968b361", 26891),
            "3.28.0.163": ("b87d847492ef5efaaf81dfcae6294ed2db61db335d79484a668f2f6fa968b361", 26891),
            "3.28.0.164": ("b87d847492ef5efaaf81dfcae6294ed2db61db335d79484a668f2f6fa968b361", 26891),
            "3.28.0.166": ("b87d847492ef5efaaf81dfcae6294ed2db61db335d79484a668f2f6fa968b361", 26891),
            "3.28.0.169": ("b87d847492ef5efaaf81dfcae6294ed2db61db335d79484a668f2f6fa968b361", 26891),
        }
        for package in self.catalog:
            _runtime, current = reading._shared_specs(package)
            predecessor = reading._known_defective_feature(package, current)
            self.assertEqual(
                (predecessor.sha256, predecessor.size), expected[package.release_version]
            )
            self.assertEqual(predecessor.feature_id, current.feature_id)
            self.assertEqual(predecessor.package_id, current.package_id)
            self.assertEqual(predecessor.runtime_path, current.runtime_path)

        unknown = reading._known_defective_feature(
            reading.replace(self.package, release_version="3.28.0.170"), self.feature
        )
        self.assertIsNone(unknown)

        navigation_hash = (
            "0b4ae3ac2682c452cb17fc964d108c4194a59aeb9d55f9ef2c6ddf8582679c66",
            28634,
        )
        for package in self.catalog:
            _runtime, current = reading._shared_specs(package)
            predecessor = reading._known_navigation_defective_feature(package, current)
            if package.release_version.startswith("3.27."):
                self.assertEqual(
                    (predecessor.sha256, predecessor.size), navigation_hash
                )
            else:
                self.assertIsNone(predecessor)

    def test_inspection_offers_only_the_exact_settings_defect_predecessor(self):
        defective = reading._known_defective_feature(self.package, self.feature)
        inspection = shared.SharedInspection(
            {reading.FEATURE_ID: self._state(defective)}, True, True
        )
        installed = {reading.FEATURE_ID: defective}
        selected = {reading.FEATURE_ID: "settings-component-defect"}
        with patch.object(
            reading.shared,
            "inspect_shared_revisions",
            return_value=(inspection, installed, selected),
        ) as inspect:
            result = reading._inspection_for_migration(
                Mock(), self.runtime, self._trusted(), self.package
            )
        self.assertEqual(result, (inspection, installed, selected))
        revisions = inspect.call_args.args[3]
        self.assertEqual(
            revisions,
            {
                reading.FEATURE_ID: (
                    ("settings-component-defect", defective),
                    (
                        "settings-navigation-defect",
                        reading._known_navigation_defective_feature(
                            self.package, self.feature
                        ),
                    ),
                )
            },
        )

    def test_328_does_not_trust_the_navigation_predecessor(self):
        package = next(
            item for item in self.catalog if item.release_version.startswith("3.28.")
        )
        runtime, feature = reading._shared_specs(package)
        trusted = {reading.FEATURE_ID: feature}
        inspection = shared.SharedInspection({}, False, True)
        with patch.object(
            reading.shared,
            "inspect_shared_revisions",
            return_value=(inspection, trusted, {}),
        ) as inspect:
            reading._inspection_for_migration(Mock(), runtime, trusted, package)
        revisions = inspect.call_args.args[3]
        self.assertEqual(
            revisions,
            {
                reading.FEATURE_ID: (
                    (
                        "settings-component-defect",
                        reading._known_defective_feature(package, feature),
                    ),
                )
            },
        )

    def test_download_uses_cos_then_github_and_validates_cache(self):
        payload = b"trusted archive"
        package = reading.ReadingEnhancementsPackage(
            self.package.firmware,
            self.package.release_version,
            self.package.channel,
            self.package.platform,
            self.package.architecture,
            self.package.xochitl_sha256,
            self.package.asset,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            self.package.files,
            self.package.urls,
            self.package.package_revision,
            True,
            False,
        )
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            reading.tap,
            "_download_limited",
            side_effect=(b"bad", payload),
        ) as download:
            destination = reading.download_package(package, state_dir)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(
                [call.args[0] for call in download.call_args_list],
                list(package.download_urls),
            )

    def test_clean_install_starts_new_feature_without_old_switch_state(self):
        calls = []
        ssh = Mock()
        ssh.file_exists.return_value = False
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.tap, "_preflight_device"
        ), patch.object(reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (), self.feature)), patch.object(reading.tap, "_xochitl_process_token", return_value=self.process), patch.object(
            reading.shared, "has_shared_artifacts", return_value=False
        ), patch.object(
            reading, "extract_verified_package", return_value=Path("unused")
        ), patch.object(
            reading.shared, "replace_shared_features", side_effect=lambda *args, **kwargs: calls.append((args, kwargs))
        ):
            result = reading.install(ssh, self.package, "unused.tar.gz")
        self.assertEqual(result.state, reading.ReadingEnhancementsState.NOT_INSTALLED)
        # get_status is intentionally allowed to fail on this minimal fake;
        # the important assertion is the target marker passed to the transaction.
        self.assertEqual(len(calls), 1)
        target_states = calls[0][0][5]
        self.assertEqual(set(target_states), {reading.FEATURE_ID})
        self.assertTrue(target_states[reading.FEATURE_ID].enabled)
        self.assertNotIn("xochitl.conf", " ".join(ssh.method_calls.__str__()))

    def test_only_tap_migration_drops_predecessor_and_uses_safe_defaults(self):
        tap_package = tap.select_package(tap._trusted_catalog(), self.identity)
        self.assertIsNotNone(tap_package)
        _runtime, tap_feature = tap._shared_specs(tap_package)
        trusted = {"tap-page-turn": tap_feature, reading.FEATURE_ID: self.feature}
        inspection = shared.SharedInspection(
            {"tap-page-turn": self._state(tap_feature, True)},
            True,
            True,
        )
        with patch.object(reading.tap, "_xochitl_process_token", return_value=self.process):
            states = reading._target_states(inspection, trusted, self.feature, Mock())
        self.assertEqual(set(states), {reading.FEATURE_ID})
        self.assertTrue(states[reading.FEATURE_ID].enabled)

    def test_only_fast_migration_drops_predecessor(self):
        fast_package = fast.select_package(fast._trusted_catalog(), self.identity)
        self.assertIsNotNone(fast_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        trusted = {"fast-mono-reading": fast_feature, reading.FEATURE_ID: self.feature}
        inspection = shared.SharedInspection(
            {"fast-mono-reading": self._state(fast_feature, False)},
            False,
            True,
        )
        with patch.object(reading.tap, "_xochitl_process_token", return_value=self.process):
            states = reading._target_states(inspection, trusted, self.feature, Mock())
        self.assertEqual(set(states), {reading.FEATURE_ID})
        self.assertTrue(states[reading.FEATURE_ID].enabled)

    def test_both_migration_preserves_peers_only(self):
        tap_package = tap.select_package(tap._trusted_catalog(), self.identity)
        fast_package = fast.select_package(fast._trusted_catalog(), self.identity)
        _runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        peer = shared.SharedFeatureSpec(
            "native-chinese", "native", "native.qmd", "exthome/qt-resource-rebuilder/native.qmd", "a" * 64, 1, 0o644
        )
        trusted = {
            "tap-page-turn": tap_feature,
            "fast-mono-reading": fast_feature,
            "native-chinese": peer,
            reading.FEATURE_ID: self.feature,
        }
        inspection = shared.SharedInspection(
            {
                "tap-page-turn": self._state(tap_feature, True),
                "fast-mono-reading": self._state(fast_feature, False),
                "native-chinese": self._state(peer, True),
            },
            True,
            True,
        )
        with patch.object(reading.tap, "_xochitl_process_token", return_value=self.process):
            states = reading._target_states(inspection, trusted, self.feature, Mock())
        self.assertEqual(set(states), {"native-chinese", reading.FEATURE_ID})
        self.assertTrue(states["native-chinese"].enabled)
        self.assertTrue(states[reading.FEATURE_ID].enabled)

    def test_unknown_state_refuses_before_transaction(self):
        ssh = Mock()
        ssh.file_exists.return_value = True
        transaction = Mock()
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.tap, "_preflight_device"
        ), patch.object(reading, "extract_verified_package", return_value=Path("unused")), patch.object(
            reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (), self.feature)
        ), patch.object(
            reading.shared, "replace_shared_features", transaction
        ), patch.object(
            reading.shared, "inspect_shared", side_effect=RuntimeError("unknown feature")
        ), patch.object(
            reading, "_inspection_for_migration", side_effect=RuntimeError("unknown feature")
        ):
            with self.assertRaisesRegex(RuntimeError, "unknown feature"):
                reading.install(ssh, self.package, "unused.tar.gz")
        transaction.assert_not_called()

    def _legacy_mock(self, feature_id="tap-page-turn"):
        legacy = Mock()
        legacy.feature.feature_id = feature_id
        legacy.layout.remote_base = f"/home/root/.local/share/rmtool/{feature_id}"
        legacy.marker_path = legacy.layout.remote_base + "/package.json"
        legacy.layout.dropin_path = f"/etc/systemd/system/xochitl.service.d/91-rmtool-{feature_id}.conf"
        return legacy

    def test_verified_legacy_standalone_is_migratable(self):
        legacy = self._legacy_mock()
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == legacy.layout.remote_base
        calls = []
        result = reading.ReadingEnhancementsStatus(
            reading.ReadingEnhancementsState.MIGRATION_AVAILABLE,
            self.identity,
            self.package,
        )
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.tap, "_preflight_device"
        ), patch.object(reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (legacy,), self.feature)), patch.object(
            reading.shared, "validate_legacy", return_value=True
        ), patch.object(reading.shared, "has_shared_artifacts", return_value=False), patch.object(
            reading, "extract_verified_package", return_value=Path("unused")
        ), patch.object(reading.tap, "_xochitl_process_token", return_value=self.process), patch.object(
            reading.shared, "replace_shared_features", side_effect=lambda *args, **kwargs: calls.append((args, kwargs))
        ), patch.object(reading, "get_status", return_value=result):
            reading.install(ssh, self.package, "unused.tar.gz")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["legacy_layouts"], (legacy.layout,))
        target_states = calls[0][0][5]
        self.assertEqual(set(target_states), {reading.FEATURE_ID})
        self.assertTrue(target_states[reading.FEATURE_ID].enabled)

    def test_legacy_status_reports_migration_available(self):
        legacy = self._legacy_mock()
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == legacy.layout.remote_base
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (legacy,), self.feature)
        ), patch.object(reading.shared, "validate_legacy", return_value=True), patch.object(
            reading.shared, "has_shared_artifacts", return_value=False
        ):
            status = reading.get_status(ssh, (self.package,))
        self.assertEqual(status.state, reading.ReadingEnhancementsState.MIGRATION_AVAILABLE)

    def test_both_verified_legacy_standalone_trees_are_migrated_atomically(self):
        tap_legacy = self._legacy_mock("tap-page-turn")
        fast_legacy = self._legacy_mock("fast-mono-reading")
        ssh = Mock()
        paths = {tap_legacy.layout.remote_base, fast_legacy.layout.remote_base}
        ssh.file_exists.side_effect = lambda path: path in paths
        calls = []
        result = reading.ReadingEnhancementsStatus(
            reading.ReadingEnhancementsState.MIGRATION_AVAILABLE,
            self.identity,
            self.package,
        )
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.tap, "_preflight_device"
        ), patch.object(reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (tap_legacy, fast_legacy), self.feature)), patch.object(
            reading.shared, "validate_legacy", return_value=True
        ), patch.object(reading.shared, "has_shared_artifacts", return_value=False), patch.object(
            reading, "extract_verified_package", return_value=Path("unused")
        ), patch.object(reading.tap, "_xochitl_process_token", return_value=self.process), patch.object(
            reading.shared, "replace_shared_features", side_effect=lambda *args, **kwargs: calls.append((args, kwargs))
        ), patch.object(reading, "get_status", return_value=result):
            reading.install(ssh, self.package, "unused.tar.gz")
        self.assertEqual(
            calls[0][1]["legacy_layouts"], (tap_legacy.layout, fast_legacy.layout)
        )

    def test_mixed_shared_and_legacy_layout_refuses_before_staging(self):
        legacy = self._legacy_mock()
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == legacy.layout.remote_base
        replace = Mock()
        extract = Mock()
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.tap, "_preflight_device"
        ), patch.object(reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (legacy,), self.feature)), patch.object(
            reading.shared, "validate_legacy", return_value=True
        ), patch.object(reading.shared, "has_shared_artifacts", return_value=True), patch.object(
            reading, "extract_verified_package", extract
        ), patch.object(reading.shared, "replace_shared_features", replace):
            with self.assertRaisesRegex(RuntimeError, "混合布局"):
                reading.install(ssh, self.package, "unused.tar.gz")
        extract.assert_not_called()
        replace.assert_not_called()

    def test_invalid_legacy_refuses_before_staging(self):
        legacy = self._legacy_mock()
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == legacy.layout.remote_base
        replace = Mock()
        extract = Mock()
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.tap, "_preflight_device"
        ), patch.object(reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (legacy,), self.feature)), patch.object(
            reading.shared, "validate_legacy", return_value=False
        ), patch.object(reading, "extract_verified_package", extract), patch.object(
            reading.shared, "replace_shared_features", replace
        ):
            with self.assertRaisesRegex(RuntimeError, "不完整"):
                reading.install(ssh, self.package, "unused.tar.gz")
        extract.assert_not_called()
        replace.assert_not_called()

    def test_idempotent_install_does_not_replace_existing_exact_package(self):
        ssh = Mock()
        ssh.file_exists.return_value = True
        inspection = shared.SharedInspection(
            {reading.FEATURE_ID: self._state(self.feature, True)}, True, True
        )
        replace = Mock()
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.tap, "_preflight_device"
        ), patch.object(reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (), self.feature)), patch.object(reading.tap, "_xochitl_process_token", return_value=self.process), patch.object(
            reading.shared, "has_shared_artifacts", return_value=True
        ), patch.object(reading, "extract_verified_package", return_value=Path("unused")), patch.object(
            reading, "_inspection_for_migration", return_value=(inspection, self._trusted(), {})
        ), patch.object(reading.shared, "replace_shared_features", replace), patch.object(
            reading, "get_status", return_value=Mock(state=reading.ReadingEnhancementsState.ENABLED)
        ):
            reading.install(ssh, self.package, "unused.tar.gz")
        replace.assert_not_called()

    def test_status_distinguishes_enabled_and_disable_pending_exactly(self):
        ssh = Mock()
        ssh.file_exists.return_value = True
        context = (self.runtime, self._trusted(), (), self.feature)
        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.shared, "has_shared_artifacts", return_value=True
        ), patch.object(reading, "_trusted_context", return_value=context), patch.object(
            reading.tap, "_xochitl_process_token", return_value="new-token"
        ), patch.object(
            reading, "_inspection_for_migration",
            return_value=(shared.SharedInspection({reading.FEATURE_ID: self._state(self.feature, True)}, True, True), self._trusted(), {}),
        ):
            self.assertEqual(
                reading.get_status(ssh, (self.package,)).state,
                reading.ReadingEnhancementsState.ENABLED,
            )

        with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
            reading.shared, "has_shared_artifacts", return_value=True
        ), patch.object(reading, "_trusted_context", return_value=context), patch.object(
            reading.tap, "_xochitl_process_token", return_value=self.process
        ), patch.object(
            reading, "_inspection_for_migration",
            return_value=(shared.SharedInspection({reading.FEATURE_ID: self._state(self.feature, False)}, False, True), self._trusted(), {}),
        ):
            self.assertEqual(
                reading.get_status(ssh, (self.package,)).state,
                reading.ReadingEnhancementsState.DISABLE_PENDING_REBOOT,
            )

    def test_disabled_state_uses_existing_atomic_disable_path(self):
        status = reading.ReadingEnhancementsStatus(
            reading.ReadingEnhancementsState.INSTALLED_DISABLED,
            self.identity,
            self.package,
        )
        ssh = Mock()
        with patch.object(reading, "get_status", return_value=status), patch.object(
            reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (), self.feature)
        ), patch.object(reading, "_inspection_for_migration", return_value=(Mock(states={reading.FEATURE_ID: self._state(self.feature, False)}), self._trusted(), {})), patch.object(reading.shared, "disable_shared") as disable, patch.object(
            reading, "get_status", side_effect=(status, status)
        ):
            reading.disable(ssh, (self.package,))
        disable.assert_called_once()

    def test_known_defective_package_reports_repair_and_replaces_atomically(self):
        defects = (
            (
                "settings-component-defect",
                reading._known_defective_feature(self.package, self.feature),
            ),
            (
                "settings-navigation-defect",
                reading._known_navigation_defective_feature(
                    self.package, self.feature
                ),
            ),
        )
        for reason, defective in defects:
            with self.subTest(reason=reason):
                self.assertIsNotNone(defective)
                defective_trusted = {reading.FEATURE_ID: defective}
                inspection = shared.SharedInspection(
                    {reading.FEATURE_ID: self._state(defective, True)}, True, True
                )
                ssh = Mock()
                ssh.file_exists.return_value = True
                context = (self.runtime, self._trusted(), (), self.feature)
                selected = {reading.FEATURE_ID: reason}
                with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
                    reading, "_trusted_context", return_value=context
                ), patch.object(reading.shared, "has_shared_artifacts", return_value=True), patch.object(
                    reading, "_inspection_for_migration",
                    return_value=(inspection, defective_trusted, selected),
                ):
                    status = reading.get_status(ssh, (self.package,))
                self.assertEqual(
                    status.state, reading.ReadingEnhancementsState.REPAIR_AVAILABLE
                )
                self.assertTrue(status.recovery_available)

                calls = []
                result = reading.ReadingEnhancementsStatus(
                    reading.ReadingEnhancementsState.ENABLE_PENDING_REBOOT,
                    self.identity,
                    self.package,
                )
                with patch.object(reading.tap, "get_device_identity", return_value=self.identity), patch.object(
                    reading.tap, "_preflight_device"
                ), patch.object(reading, "_trusted_context", return_value=context), patch.object(
                    reading.shared, "has_shared_artifacts", return_value=True
                ), patch.object(reading, "extract_verified_package", return_value=Path("fixed")), patch.object(
                    reading, "_inspection_for_migration",
                    return_value=(inspection, defective_trusted, selected),
                ), patch.object(reading.tap, "_xochitl_process_token", return_value=self.process), patch.object(
                    reading.shared, "replace_shared_features", side_effect=lambda *args, **kwargs: calls.append((args, kwargs))
                ), patch.object(reading, "get_status", return_value=result):
                    reading.install(ssh, self.package, "fixed.tar.gz")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0][2][reading.FEATURE_ID], defective)
                self.assertEqual(calls[0][0][4][reading.FEATURE_ID], self.feature)
                self.assertEqual(
                    calls[0][0][5][reading.FEATURE_ID].spec, self.feature
                )

    def test_known_defective_package_can_be_disabled_and_marker_normalized(self):
        defective = reading._known_defective_feature(self.package, self.feature)
        inspection = shared.SharedInspection(
            {reading.FEATURE_ID: self._state(defective, True)}, True, True
        )
        status = reading.ReadingEnhancementsStatus(
            reading.ReadingEnhancementsState.REPAIR_AVAILABLE,
            self.identity,
            self.package,
            (self.package,),
            recovery_available=True,
        )
        result = reading.ReadingEnhancementsStatus(
            reading.ReadingEnhancementsState.DISABLE_PENDING_REBOOT,
            self.identity,
            self.package,
        )
        defective_trusted = {reading.FEATURE_ID: defective}
        with patch.object(reading, "get_status", side_effect=(status, result)), patch.object(
            reading, "_trusted_context", return_value=(self.runtime, self._trusted(), (), self.feature)
        ), patch.object(
            reading, "_inspection_for_migration",
            return_value=(inspection, defective_trusted, {reading.FEATURE_ID: "settings-component-defect"}),
        ), patch.object(reading.shared, "disable_shared") as disable:
            self.assertIs(reading.disable(Mock(), (self.package,)), result)
        disable.assert_called_once_with(
            unittest.mock.ANY,
            self.runtime,
            reading.FEATURE_ID,
            defective_trusted,
            replacement_spec=self.feature,
        )

    def test_shared_transaction_replaces_same_id_defective_qmd(self):
        defective = reading._known_defective_feature(self.package, self.feature)
        current_trusted = {reading.FEATURE_ID: defective}
        target_trusted = self._trusted()
        current = shared.SharedInspection(
            {reading.FEATURE_ID: self._state(defective)}, True, True
        )
        final = shared.SharedInspection(
            {reading.FEATURE_ID: self._state(self.feature)}, True, True
        )
        target_states = {reading.FEATURE_ID: self._state(self.feature)}
        ssh = Mock()
        ssh.exec_checked.return_value = ""
        with patch.object(
            shared, "_operation_lock", lambda _ssh: contextlib.nullcontext()
        ), patch.object(shared, "_assert_managed_dropins"), patch.object(
            shared, "has_shared_artifacts", return_value=True
        ), patch.object(
            shared, "inspect_shared", side_effect=(current, final)
        ) as inspect, patch.object(shared, "_stage_shared") as stage, patch.object(
            shared, "shared_transaction_script", return_value="#!/bin/sh\n:"
        ), patch.object(shared, "_upload_bytes"):
            result = shared.replace_shared_features(
                ssh,
                self.runtime,
                current_trusted,
                self.runtime,
                target_trusted,
                target_states,
                {reading.FEATURE_ID: Path("fixed")},
            )
        self.assertIs(result, final)
        self.assertEqual(inspect.call_args_list[0].args[2], current_trusted)
        self.assertEqual(inspect.call_args_list[1].args[2], target_trusted)
        self.assertEqual(stage.call_args.args[2], target_states)
        self.assertEqual(
            stage.call_args.args[3], {reading.FEATURE_ID: Path("fixed")}
        )

    def test_transaction_template_has_fault_rollback_and_no_restart(self):
        script = shared.shared_transaction_script(
            "/data/rmtool/xovi-standalone.staging-token",
            "token",
            (),
            enable_dropin=True,
        )
        self.assertIn("ROLLBACK_OK", script)
        self.assertIn("COMMITTED=1", script)
        self.assertIn("systemctl daemon-reload", script)
        self.assertNotIn("systemctl restart", script)
        self.assertNotIn("reboot", script)

    def test_target_state_validation_happens_before_stage(self):
        target = {reading.FEATURE_ID: self.feature}
        bad_spec = shared.SharedFeatureSpec(
            reading.FEATURE_ID,
            "different-package",
            self.feature.archive_path,
            self.feature.runtime_path,
            self.feature.sha256,
            self.feature.size,
            self.feature.mode,
        )
        with patch.object(shared, "_operation_lock", lambda _ssh: contextlib.nullcontext()), patch.object(
            shared, "has_shared_artifacts", return_value=False
        ), patch.object(shared, "_stage_shared") as stage:
            with self.assertRaisesRegex(RuntimeError, "状态与信任清单"):
                shared.replace_shared_features(
                    Mock(), self.runtime, self._trusted(), self.runtime, target,
                    {reading.FEATURE_ID: self._state(bad_spec)},
                    {reading.FEATURE_ID: Path("unused")},
                )
        stage.assert_not_called()

    def test_stage_fault_cleans_staging_without_activation_script(self):
        ssh = Mock()
        ssh.exec_checked.return_value = ""
        with patch.object(shared, "_operation_lock", lambda _ssh: contextlib.nullcontext()), patch.object(
            shared, "has_shared_artifacts", return_value=False
        ), patch.object(shared, "_stage_shared", side_effect=RuntimeError("injected stage fault")), patch.object(
            shared, "_upload_bytes"
        ) as upload:
            with self.assertRaisesRegex(RuntimeError, "injected stage fault"):
                shared.replace_shared_features(
                    ssh,
                    self.runtime,
                    self._trusted(),
                    self.runtime,
                    self._trusted(),
                    {reading.FEATURE_ID: self._state(self.feature)},
                    {reading.FEATURE_ID: Path("unused")},
                )
        upload.assert_not_called()
        self.assertTrue(any("staging-" in call.args[0] for call in ssh.exec_checked.call_args_list))


if __name__ == "__main__":
    unittest.main()
