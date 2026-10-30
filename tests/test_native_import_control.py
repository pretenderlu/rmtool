"""Exact native-import packages and shared-control lifecycle boundaries."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import _native_import_control as control
import _qr_upload as qr
import _tap_page_turn as tap
import _xovi_standalone as shared


class NativeControlTests(unittest.TestCase):
    def setUp(self):
        self.catalog = control._trusted_catalog()
        self.package = next(item for item in self.catalog
                            if item.platform == "ferrari" and item.release_version == "3.28.0.172")
        p = self.package
        self.identity = tap.DeviceIdentity(p.firmware, p.platform, p.architecture, p.xochitl_sha256)
        self.runtime, self.trusted, self.legacies, self.feature = control._context(self.identity, p)

    def test_exact_five_stable_and_five_beta_targets_share_verified_runtime(self):
        self.assertEqual(len(self.catalog), 15)
        self.assertEqual({p.platform for p in self.catalog if p.channel == "stable"},
                         {"rm1", "rm2", "ferrari", "chiappa", "tatsu"})
        self.assertEqual({(p.platform, p.release_version) for p in self.catalog if p.channel == "beta"},
                         {(platform, "3.29.0.153") for platform in ("rm1", "rm2", "ferrari", "chiappa", "tatsu")})
        for p in self.catalog:
            identity = tap.DeviceIdentity(p.firmware, p.platform, p.architecture, p.xochitl_sha256)
            runtime, trusted, _, feature = control._context(identity, p)
            shared.assert_feature_layout(runtime, trusted.values())
            self.assertTrue(p.offline_verified)
            self.assertFalse(p.device_verified)
            self.assertEqual(feature.strict_metadata_paths, (control.BRIDGE_PAYLOAD_PATH, control.HELPER_PAYLOAD_PATH))
            self.assertIsNone(control.select_package(self.catalog, replace(identity, xochitl_sha256="0" * 64)))
            self.assertEqual({f.path for f in p.files}, control.REQUIRED_PATHS)

    def test_beta_controller_shares_exact_peers_with_device_specific_gates(self):
        import _native_chinese as chinese
        import _pinyin_input as pinyin
        import _reading_enhancements as reading
        import _note_enhancements as note
        for p in self.catalog:
            if p.release_version != "3.29.0.153":
                continue
            identity = tap.DeviceIdentity(p.firmware, p.platform, p.architecture, p.xochitl_sha256)
            previous = next(item for item in tap._trusted_catalog()
                            if item.platform == p.platform and item.release_version == "3.29.0.152")
            self.assertEqual(identity.firmware, previous.firmware)
            self.assertIsNone(control.select_package(self.catalog, replace(identity, xochitl_sha256=previous.xochitl_sha256)))
            with self.subTest(platform=p.platform):
                runtime, trusted, legacies, _ = control._context(identity, p)
                expected = {
                    control.FEATURE_ID, "qr-upload", "native-chinese", "pinyin-input",
                    "tap-page-turn", "appload", "koreader",
                }
                if p.platform in ("ferrari", "chiappa"):
                    expected.update(("reading-enhancements", "note-enhancements", "weread-launcher"))
                self.assertEqual(set(trusted), expected)
                self.assertEqual([legacy.feature.feature_id for legacy in legacies], ["tap-page-turn"])
                self.assertTrue(all(legacy.runtime == runtime for legacy in legacies))
                self.assertEqual(shared.preferred_layout(runtime), shared.LEGACY_SHARED_LAYOUT
                                 if p.platform == "rm1" else shared.SHARED_LAYOUT)
                for module in (tap, chinese, pinyin, reading, note, qr):
                    supported = module not in (reading, note) or p.platform in ("ferrari", "chiappa")
                    self.assertEqual(module.select_package(module._trusted_catalog(), identity) is not None, supported)
                with (patch.object(tap, "get_device_identity", return_value=identity),
                      patch.object(shared, "has_shared_artifacts", return_value=False)):
                    self.assertEqual(control.get_status(Mock(), self.catalog).state, control.NativeImportState.NOT_INSTALLED)

    def test_manifest_refuses_missing_targets_changed_payload_and_unsafe_metadata(self):
        original = json.loads(control.BUNDLED_MANIFEST.read_bytes())
        for field, value in (("urls", ["https://example.com/a"]), ("size", True),
                             ("files", []), ("device_verified", 1), ("package_revision", True)):
            document = json.loads(json.dumps(original))
            document["packages"][0][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                control.parse_manifest(json.dumps(document).encode(), require_local_match=False)
        document = json.loads(json.dumps(original))
        document["packages"].pop()
        with self.assertRaisesRegex(RuntimeError, "缺少"):
            control.parse_manifest(json.dumps(document).encode(), require_local_match=False)
        document = json.loads(json.dumps(original))
        document["packages"][0]["files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "内置"):
            control.parse_manifest(json.dumps(document).encode())
        document["packages"][0]["files"][0]["mode"] = 0o777
        with self.assertRaisesRegex(RuntimeError, "权限"):
            control.parse_manifest(json.dumps(document).encode(), require_local_match=False)

    def test_cached_packages_verify_all_bytes_when_available(self):
        import _plugin_packages as packages
        state = Path(__file__).resolve().parents[1] / ".rmtool"
        archives = [(p, packages.package_cache_path(str(state), control.FEATURE_ID, p)) for p in self.catalog]
        if not all(path.is_file() for _, path in archives):
            self.skipTest("native-import build cache is not configured")
        for package, archive in archives:
            with self.subTest(platform=package.platform), tempfile.TemporaryDirectory() as temporary:
                result = control.extract_verified_package(archive, package, temporary)
                self.assertTrue((result / control.HELPER_PAYLOAD_PATH).is_file())
                self.assertTrue((result / control.BRIDGE_PAYLOAD_PATH).is_file())

    def test_status_distinguishes_restart_loading_disabled_and_tampered_state(self):
        for enabled, token, active, expected in (
            (True, "old", True, control.NativeImportState.ENABLED),
            (True, "now", False, control.NativeImportState.ENABLE_PENDING_REBOOT),
            (True, "old", False, control.NativeImportState.BROKEN),
            (False, "old", False, control.NativeImportState.INSTALLED_DISABLED),
            (False, "now", True, control.NativeImportState.DISABLE_PENDING_REBOOT),
        ):
            inspection = shared.SharedInspection({control.FEATURE_ID: shared.SharedFeatureState(self.feature, enabled, token)}, active, True)
            with (self.subTest(expected=expected), patch.object(tap, "get_device_identity", return_value=self.identity),
                  patch.object(tap, "_xochitl_process_token", return_value="now"),
                  patch.object(shared, "has_shared_artifacts", return_value=True),
                  patch.object(control, "_inspect", return_value=(inspection, {}, {}))):
                self.assertEqual(control.get_status(Mock(), self.catalog).state, expected)
        with (patch.object(tap, "get_device_identity", return_value=self.identity),
              patch.object(shared, "has_shared_artifacts", return_value=True),
              patch.object(control, "_inspect", side_effect=RuntimeError("modified"))):
            self.assertEqual(control.get_status(Mock(), self.catalog).detail, "modified")

    def test_loaded_helper_uses_actual_data_or_home_layout_and_requires_new_process(self):
        for layout in (shared.SHARED_LAYOUT, shared.LEGACY_SHARED_LAYOUT):
            for enabled, token, active in ((True, "old", True), (False, "old", True),
                                           (True, "now", True), (True, "old", False)):
                inspection = shared.SharedInspection({control.FEATURE_ID: shared.SharedFeatureState(self.feature, enabled, token)}, active, True, layout=layout)
                with (self.subTest(layout=layout, enabled=enabled, token=token, active=active),
                      patch.object(tap, "get_device_identity", return_value=self.identity),
                      patch.object(tap, "_xochitl_process_token", return_value="now"),
                      patch.object(shared, "has_shared_artifacts", return_value=True),
                      patch.object(control, "_inspect", return_value=(inspection, {}, {}))):
                    expected = f"{layout.remote_base}/{control.HELPER_PAYLOAD_PATH}" if enabled and token != "now" and active else None
                    self.assertEqual(control.loaded_helper(Mock()), expected)

    def test_authenticated_qr_controller_remains_usable_without_standalone_install(self):
        package = qr._trusted_catalog()[0]
        identity = tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
        feature = qr._shared_specs(package)[1]
        inspection = shared.SharedInspection({qr.FEATURE_ID: shared.SharedFeatureState(feature, True, "old")}, True, True)
        with (patch.object(tap, "get_device_identity", return_value=identity),
              patch.object(tap, "_xochitl_process_token", return_value="now"),
              patch.object(shared, "has_shared_artifacts", return_value=True),
              patch.object(control, "_inspect", return_value=(inspection, {}, {}))):
            self.assertEqual(control.loaded_helper(Mock()), f"{inspection.layout.remote_base}/{qr.RECEIVER_PAYLOAD_PATH}")
            with patch.object(control, "_inspect", side_effect=RuntimeError("untrusted")):
                with self.assertRaisesRegex(RuntimeError, "untrusted"):
                    control.loaded_helper(Mock())
            with patch.object(shared, "assert_startup_guard_not_latched", side_effect=RuntimeError("latched")):
                with self.assertRaisesRegex(RuntimeError, "latched"):
                    control.loaded_helper(Mock())

    def test_unsupported_firmware_cannot_load_or_install_automatic_control(self):
        ssh = Mock()
        identity = replace(self.identity, xochitl_sha256="0" * 64)
        with (patch.object(tap, "get_device_identity", return_value=identity),
              patch.object(shared, "enable_shared") as enable):
            self.assertIsNone(control.loaded_helper(ssh))
            with self.assertRaisesRegex(RuntimeError, "不精确匹配"):
                control.install(ssh, self.package, "unused")
            enable.assert_not_called()
            ssh.exec_checked.assert_not_called()

    def test_active_transfer_refuses_mutation_before_any_deployment(self):
        for path in ("/run/rmtool-qr-upload-ui", "/home/root/.rmtool-qr-upload-web-state",
                     "/run/rmtool-native-import-control/active", "/home/root/.rmtool-native-import-web-state"):
            with (self.subTest(path=path), patch.object(tap, "get_device_identity", return_value=self.identity),
                  patch.object(shared, "_remote_entry_exists", side_effect=lambda _ssh, p: p == path),
                  patch.object(shared, "enable_shared") as enable):
                with self.assertRaisesRegex(RuntimeError, "停止扫码"):
                    control.install(Mock(), self.package, "unused")
                enable.assert_not_called()

    def test_install_is_verified_shared_transaction_and_never_restarts(self):
        ssh = Mock()
        pending = control.NativeImportStatus(control.NativeImportState.ENABLE_PENDING_REBOOT, self.identity, self.package)
        with (patch.object(tap, "get_device_identity", return_value=self.identity),
              patch.object(control, "_assert_stopped"), patch.object(tap, "_preflight_device"),
              patch.object(tap, "_vellum_runtime_present", return_value=False),
              patch.object(shared, "validate_legacy", return_value=False),
              patch.object(shared, "has_shared_artifacts", return_value=False),
              patch.object(control, "extract_verified_package", return_value=Path("verified")) as extract,
              patch.object(shared, "enable_shared") as enable,
              patch.object(control, "get_status", return_value=pending) as status):
            self.assertEqual(control.install(ssh, self.package, "bundle"), pending)
            enable.assert_called_once_with(ssh, self.runtime, self.feature, Path("verified"), self.trusted, ())
            ssh.exec_checked.assert_not_called()
            enable.reset_mock()
            extract.side_effect = RuntimeError("bad archive")
            with self.assertRaisesRegex(RuntimeError, "bad archive"):
                control.install(ssh, self.package, "bundle")
            enable.assert_not_called()
            extract.side_effect = None
            status.return_value = replace(pending, state=control.NativeImportState.BROKEN, detail="postcondition failed")
            with self.assertRaisesRegex(RuntimeError, "postcondition failed"):
                control.install(ssh, self.package, "bundle")

    def test_old_qr_without_control_cannot_silently_shadow_native_component(self):
        ssh = Mock()
        inspection = shared.SharedInspection({qr.FEATURE_ID: shared.SharedFeatureState(self.feature, True, "old")}, True, True)
        with (patch.object(tap, "get_device_identity", return_value=self.identity),
              patch.object(control, "_assert_stopped"), patch.object(tap, "_preflight_device"),
              patch.object(tap, "_vellum_runtime_present", return_value=False),
              patch.object(shared, "validate_legacy", return_value=False),
              patch.object(shared, "has_shared_artifacts", return_value=True),
              patch.object(control, "_inspect", return_value=(inspection, {}, {})),
              patch.object(shared, "enable_shared") as enable):
            ssh.exec_checked.side_effect = RuntimeError("symbol absent")
            with self.assertRaisesRegex(RuntimeError, "先更新扫码传输"):
                control.install(ssh, self.package, "unused")
            enable.assert_not_called()

    def test_initialize_is_idempotent_and_refuses_broken_state(self):
        status = control.NativeImportStatus(control.NativeImportState.ENABLED, self.identity, self.package)
        with patch.object(control, "get_status", return_value=status) as get, patch.object(control, "download_package") as download:
            self.assertEqual(control.initialize(Mock(), "state"), status)
            download.assert_not_called()
            get.return_value = replace(status, state=control.NativeImportState.BROKEN, detail="unsafe")
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                control.initialize(Mock(), "state")
            download.assert_not_called()

    def test_shared_inspection_and_recovery_recognize_native_control_with_peers(self):
        import _plugin_recovery as recovery
        from tests.test_plugin_recovery import Device, TOKEN
        device = Device(package=self.package)
        states = {name: shared.SharedFeatureState(self.trusted[name], True, TOKEN)
                  for name in (control.FEATURE_ID, "note-enhancements", "reading-enhancements")}
        device.install(states)
        identity_patch = patch.object(tap, "get_device_identity", return_value=self.identity)
        identity_patch.start()
        self.addCleanup(identity_patch.stop)
        inspection, _, _ = control._inspect(device, self.identity, self.runtime, self.trusted)
        self.assertEqual(set(inspection.states), set(states))
        self.assertEqual(recovery.inspect_recovery(device).state, recovery.RecoveryState.NOT_NEEDED)
        device.entries[f"{device.layout.remote_base}/{control.HELPER_PAYLOAD_PATH}"].digest = "0" * 64
        with self.assertRaises(RuntimeError):
            control._inspect(device, self.identity, self.runtime, self.trusted)
        report = recovery.inspect_recovery(device)
        self.assertTrue(report.can_repair, report.detail)
        self.assertIn(control.FEATURE_ID, report.features)

    def test_migration_preserves_control_or_skips_only_unsupported_optional_component(self):
        import _residue_migration as migration
        old = next(p for p in self.catalog if p.platform == "chiappa" and p.release_version == "3.28.0.172")
        old_identity = tap.DeviceIdentity(old.firmware, old.platform, old.architecture, old.xochitl_sha256)
        _, trusted, _ = tap._trusted_shared_context(old_identity)
        states = {name: shared.SharedFeatureState(trusted[name], True, "old")
                  for name in (control.FEATURE_ID, "native-chinese")}
        for release, supported in (("3.29.0.153", True), ("3.29.0.152", False)):
            package = next(p for p in tap._trusted_catalog() if p.platform == "chiappa" and p.release_version == release)
            identity = tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
            with (self.subTest(release=release), patch.object(tap, "get_device_identity", return_value=identity),
                  patch.object(shared, "has_shared_artifacts", return_value=True),
                  patch.object(shared, "read_shared_identity", return_value=(old_identity.firmware, old_identity.platform,
                               old_identity.architecture, old_identity.xochitl_sha256)),
                  patch.object(shared, "inspect_shared_firmware_residue", return_value=shared.SharedInspection(states, False, True))):
                report = migration.inspect_residue(Mock())
            self.assertTrue(report.migratable, report)
            self.assertEqual(next(f.target_available for f in report.features if f.feature_id == control.FEATURE_ID), supported)
            self.assertTrue(next(f.target_available for f in report.features if f.feature_id == "native-chinese"))
        self.assertIs(migration._providers()[control.FEATURE_ID], control)

    def test_application_packaging_and_resource_verification_include_native_manifest(self):
        root = Path(__file__).resolve().parents[1]
        windows = (root / "build-portable.ps1").read_text()
        macos = (root / ".github/workflows/release.yml").read_text()
        assets = (root / ".github/workflows/sync-feature-assets.yml").read_text()
        self.assertIn("native-import\\manifest.json", windows)
        self.assertIn("--add-data", windows)
        self.assertIn('--add-data "$root/native-import/manifest.json:native-import"', macos)
        self.assertIn("native-import:native-import-assets", assets)
        self.assertIn("--resource native-import", assets)
        self.assertNotIn("rmtool-native-import.so", windows + macos)

    def test_uninstall_only_removes_control_and_verifies_postcondition(self):
        ssh = Mock()
        enabled = control.NativeImportStatus(control.NativeImportState.ENABLED, self.identity, self.package)
        absent = replace(enabled, state=control.NativeImportState.NOT_INSTALLED)
        with (patch.object(control, "get_status", side_effect=[enabled, absent]),
              patch.object(control, "_assert_stopped"), patch.object(control, "_inspect"),
              patch.object(shared, "remove_shared_features") as remove):
            self.assertEqual(control.uninstall(ssh, self.catalog), absent)
            remove.assert_called_once_with(ssh, self.runtime, self.trusted, (control.FEATURE_ID,))
            ssh.exec_checked.assert_not_called()
        for state in (control.NativeImportState.BROKEN, control.NativeImportState.NOT_INSTALLED):
            with (patch.object(control, "get_status", return_value=replace(enabled, state=state, detail="unsafe")),
                  patch.object(shared, "remove_shared_features") as remove):
                if state is control.NativeImportState.BROKEN:
                    with self.assertRaisesRegex(RuntimeError, "unsafe"):
                        control.uninstall(ssh, self.catalog)
                else:
                    self.assertTrue(control.uninstall(ssh, self.catalog).detail)
                remove.assert_not_called()
