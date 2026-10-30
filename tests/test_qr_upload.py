"""Managed QR package boundaries; native device acceptance is a separate gate."""
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import _qr_upload as qr
import _tap_page_turn as tap
import _xovi_standalone as shared
from tests.test_plugin_recovery import Device


class QrUploadTests(unittest.TestCase):
    def test_native_builder_normalizes_generated_source_paths(self):
        builder = (Path(__file__).resolve().parents[1] / "qr-upload/native/build.py").read_text()
        self.assertIn("-ffile-prefix-map={work}=qr-upload-generated", builder)
        self.assertIn("-fmacro-prefix-map={work}=qr-upload-generated", builder)

    def setUp(self):
        self.catalog = qr._trusted_catalog()
        self.package = next(item for item in self.catalog
                            if item.platform == "chiappa" and item.release_version == "3.28.0.172")
        p = self.package
        self.identity = tap.DeviceIdentity(p.firmware, p.platform, p.architecture, p.xochitl_sha256)
        self.runtime, self.trusted, self.legacies, self.feature = qr._context(self.identity, p)

    def test_manifest_and_shared_peers_are_exact(self):
        self.assertEqual(len(self.catalog), 11)
        self.assertEqual(self.package.platform, "chiappa")
        self.assertEqual(self.package.release_version, "3.28.0.172")
        self.assertFalse(self.package.device_verified)
        self.assertEqual(qr.parse_manifest(qr.BUNDLED_MANIFEST.read_bytes()), self.catalog)
        self.assertIn("qr-upload", self.trusted)
        self.assertIn("reading-enhancements", self.trusted)
        self.assertIn("pinyin-input", self.trusted)
        shared.assert_feature_layout(self.runtime, self.trusted.values())
        self.assertEqual(self.feature.preload_paths, ())
        self.assertEqual(self.package.file(qr.RECEIVER_PAYLOAD_PATH).mode, 0o755)
        for identity in (replace(self.identity, platform="ferrari"),
                         replace(self.identity, firmware="20261009073202"),
                         replace(self.identity, xochitl_sha256="0"*64)):
            self.assertIsNone(qr.select_package(self.catalog, identity))

    def test_beta_153_is_exact_and_preserves_152_peers(self):
        beta = next(p for p in self.catalog if p.release_version == "3.29.0.153")
        identity = tap.DeviceIdentity(beta.firmware, beta.platform, beta.architecture, beta.xochitl_sha256)
        self.assertEqual(qr.select_package(self.catalog, identity), beta)
        self.assertFalse(beta.device_verified)
        runtime, trusted, _, _ = qr._context(identity, beta)
        self.assertTrue({"native-chinese", "pinyin-input", "reading-enhancements", "note-enhancements", "qr-upload"} <= trusted.keys())
        shared.assert_feature_layout(runtime, trusted.values())
        previous = next(p for p in tap._trusted_catalog() if p.platform == "chiappa" and p.release_version == "3.29.0.152")
        self.assertEqual(previous.firmware, beta.firmware)
        self.assertNotEqual(previous.xochitl_sha256, beta.xochitl_sha256)
        self.assertIsNone(qr.select_package(self.catalog, replace(identity, xochitl_sha256=previous.xochitl_sha256)))

    def test_beta_153_runtime_matches_each_device_and_rejects_152(self):
        beta = [p for p in self.catalog if p.release_version == "3.29.0.153"]
        self.assertEqual({p.platform for p in beta}, {"ferrari", "chiappa", "tatsu", "rm1", "rm2"})
        for package in beta:
            with self.subTest(platform=package.platform):
                identity = tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
                runtime, trusted, _, _ = qr._context(identity, package)
                self.assertEqual(qr.select_package(self.catalog, identity), package)
                shared.assert_feature_layout(runtime, trusted.values())
                self.assertTrue({"native-chinese", "pinyin-input", "qr-upload", "native-import"} <= trusted.keys())
                self.assertEqual(shared.preferred_layout(runtime).remote_base.startswith("/home/"), package.platform == "rm1")
                previous = next(p for p in tap._trusted_catalog()
                                if p.platform == package.platform and p.release_version == "3.29.0.152")
                self.assertIsNone(qr.select_package(self.catalog, replace(identity, xochitl_sha256=previous.xochitl_sha256)))

    def test_manifest_rejects_scope_file_and_url_changes(self):
        original = json.loads(qr.BUNDLED_MANIFEST.read_bytes())
        for key, value in (("platform", "chiappa"), ("offline_verified", 1),
                           ("size", True), ("urls", ["https://example.com/payload"]),
                           ("files", []), ("package_revision", True)):
            with self.subTest(key=key):
                document = json.loads(json.dumps(original))
                document["packages"][0][key] = value
                with self.assertRaises(RuntimeError):
                    qr.parse_manifest(json.dumps(document).encode(), require_local_match=False)
        document = json.loads(json.dumps(original))
        document["packages"][0]["files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "内置"):
            qr.parse_manifest(json.dumps(document).encode())

    def test_status_requires_actual_shared_loading_after_restart(self):
        ssh = Mock()
        for enabled, token, active, expected in (
            (True, "old", True, qr.QrUploadState.ENABLED),
            (True, "now", False, qr.QrUploadState.ENABLE_PENDING_REBOOT),
            (True, "old", False, qr.QrUploadState.BROKEN),
            (False, "old", False, qr.QrUploadState.INSTALLED_DISABLED),
            (False, "now", True, qr.QrUploadState.DISABLE_PENDING_REBOOT),
        ):
            state = shared.SharedFeatureState(self.feature, enabled, token)
            inspection = shared.SharedInspection({qr.FEATURE_ID: state}, active, True)
            with (patch.object(tap, "get_device_identity", return_value=self.identity),
                  patch.object(tap, "_xochitl_process_token", return_value="now"),
                  patch.object(shared, "has_shared_artifacts", return_value=True),
                  patch.object(qr, "_inspect", return_value=(inspection, self.trusted, {}))):
                status = qr.get_status(ssh, self.catalog)
            self.assertEqual(status.state, expected)
            self.assertEqual(status.available_packages, self.catalog)

    def test_unknown_device_and_active_receiver_never_install(self):
        ssh = Mock()
        with (patch.object(tap, "get_device_identity", return_value=replace(self.identity, platform="rm2")),
              patch.object(shared, "enable_shared") as deploy):
            with self.assertRaisesRegex(RuntimeError, "不精确匹配"):
                qr.install(ssh, self.package, "unused")
            deploy.assert_not_called()
        for active_path in ("/run/rmtool-qr-upload-ui", "/home/root/.rmtool-qr-upload-web-state"):
            with (patch.object(tap, "get_device_identity", return_value=self.identity),
                  patch.object(shared, "_remote_entry_exists", side_effect=lambda _ssh,p: p == active_path),
                  patch.object(shared, "enable_shared") as deploy):
                with self.assertRaisesRegex(RuntimeError, "停止接收"):
                    qr.install(ssh, self.package, "unused")
                deploy.assert_not_called()

    def test_cloud_capacity_failure_with_native_peer_is_read_only_and_precedes_download(self):
        import rmtool  # noqa: F401
        from _toolbox_reading import _install_qr_upload

        package = next(
            item for item in self.catalog
            if item.platform == "rm2" and item.release_version == "3.29.0.153"
        )
        device = Device(package=package)
        native_feature = device.trusted["native-chinese"]
        device.install({
            "native-chinese": shared.SharedFeatureState(
                native_feature, True, "12345678-1234-1234-1234-123456789abc:778:1000"
            ),
        })
        device.free_kib = 0
        execute = device.exec_checked
        def root_full(command):
            if command == shared.storage.SYSTEM_SPACE_COMMAND:
                device.events.append(command)
                return "/=0"
            return execute(command)
        device.exec_checked = root_full
        device.transfer_file = Mock()
        entries = set(device.entries)
        with (patch.object(tap, "get_device_identity", return_value=device.identity),
              patch.object(tap, "_preflight_device"),
              patch.object(qr._rmkit_cn, "has_localization_cjk_font", return_value=True),
              patch.object(qr, "download_package") as download,
              patch.object(qr, "install") as install):
            with self.assertRaises(RuntimeError) as raised:
                _install_qr_upload(device, package, ".rmtool", False)
        self.assertEqual(raised.exception.recovery_context["kind"], "shared-capacity")
        self.assertEqual(raised.exception.recovery_context["required"], 31039488)
        download.assert_not_called()
        install.assert_not_called()
        device.transfer_file.assert_not_called()
        self.assertEqual(set(device.entries), entries)
        mutations = (
            "mkdir ", "mount --bind", "umount ", "rmdir ", "rm ", "mv ",
            "chmod ", "chown ",
        )
        self.assertFalse(any(
            token in command for command in device.events for token in mutations
        ), device.events)

    def test_install_uses_verified_shared_transaction_without_restart(self):
        ssh = Mock()
        with (patch.object(tap, "get_device_identity", return_value=self.identity),
              patch.object(qr, "_assert_stopped"), patch.object(tap, "_preflight_device"),
              patch.object(qr._rmkit_cn, "has_localization_cjk_font", return_value=True),
              patch.object(tap, "_vellum_runtime_present", return_value=False),
              patch.object(shared, "validate_legacy", return_value=False),
              patch.object(shared, "has_shared_artifacts", return_value=False),
              patch.object(qr, "extract_verified_package", return_value=Path("verified")) as extract,
              patch.object(shared, "enable_shared") as deploy, patch.object(qr, "get_status")):
            qr.install(ssh, self.package, "bundle")
            extract.assert_called_once()
            deploy.assert_called_once_with(ssh, self.runtime, self.feature, Path("verified"), self.trusted, ())
            ssh.exec_checked.assert_not_called()
            deploy.reset_mock()
            extract.side_effect = RuntimeError("bad archive")
            with self.assertRaisesRegex(RuntimeError, "bad archive"):
                qr.install(ssh, self.package, "bundle")
            deploy.assert_not_called()

    def test_install_requires_chinese_font_without_changing_font_settings(self):
        for package in self.catalog:
            with self.subTest(platform=package.platform, release=package.release_version):
                identity = tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
                ssh = Mock()
                with (patch.object(tap, "get_device_identity", return_value=identity),
                      patch.object(qr, "_assert_stopped"), patch.object(tap, "_preflight_device"),
                      patch.object(tap, "_vellum_runtime_present", return_value=False),
                      patch.object(qr._rmkit_cn, "has_localization_cjk_font", return_value=False),
                      patch.object(qr._rmkit_cn, "install_bundled_fallback_font") as font_install,
                      patch.object(shared, "enable_shared") as deploy):
                    with self.assertRaisesRegex(RuntimeError, "home"):
                        qr.install(ssh, package, "unused")
                    deploy.assert_not_called()
                    font_install.assert_not_called()
                    ssh.exec_checked.assert_not_called()

    def test_managed_install_allows_inactive_external_files_but_rejects_active_loaders(self):
        for conflict in ("none", "dropin", "loaded", "modified"):
            with self.subTest(conflict=conflict):
                device = Device(package=self.package)
                old_runtime = "/home/root/xovi/xovi.so"
                device.add(old_runtime, b"old runtime")
                if conflict == "dropin":
                    device.add("/etc/systemd/system/xochitl.service.d/99-foreign.conf", b"[Service]\n")
                elif conflict == "loaded":
                    device.maps = "1-2 r-xp 0 0:0 1 " + old_runtime
                elif conflict == "modified":
                    device.break_payload()
                with (patch.object(tap, "get_device_identity", return_value=self.identity),
                      patch.object(qr, "_assert_stopped"), patch.object(tap, "_preflight_device"),
                      patch.object(qr._rmkit_cn, "has_localization_cjk_font", return_value=True),
                      patch.object(tap, "_vellum_runtime_present", return_value=True),
                      patch.object(shared, "_check_lower_dropins") as check_lower,
                      patch.object(qr, "extract_verified_package", return_value=Path("verified")),
                      patch.object(shared, "enable_shared") as deploy,
                      patch.object(qr, "get_status")):
                    if conflict == "none":
                        qr.install(device, self.package, "bundle")
                        deploy.assert_called_once()
                        check_lower.assert_not_called()
                    else:
                        with self.assertRaises(RuntimeError):
                            qr.install(device, self.package, "bundle")
                        deploy.assert_not_called()
                self.assertEqual(device.entries[old_runtime].data, b"old runtime")
                self.assertFalse(any(event.startswith(("mkdir", "rm ", "rmdir", "mount ")) for event in device.events))

    def test_disable_and_uninstall_only_target_qr_and_preserve_books(self):
        ssh = Mock()
        status = qr.QrUploadStatus(qr.QrUploadState.ENABLED, self.identity, self.package, self.catalog)
        state = shared.SharedFeatureState(self.feature, True, "old")
        inspection = shared.SharedInspection({qr.FEATURE_ID: state}, True, True)
        with (patch.object(qr, "get_status", return_value=status), patch.object(qr, "_assert_stopped"),
              patch.object(qr, "_inspect", return_value=(inspection, self.trusted, {})),
              patch.object(shared, "disable_shared") as disable,
              patch.object(shared, "remove_shared_features") as remove):
            qr.disable(ssh, self.catalog)
            disable.assert_called_once_with(ssh, self.runtime, qr.FEATURE_ID, self.trusted, replacement_spec=None)
            qr.uninstall(ssh, self.catalog)
            remove.assert_called_once_with(ssh, self.runtime, self.trusted, (qr.FEATURE_ID,))
            ssh.exec_checked.assert_not_called()
            remove.reset_mock()
            remove.side_effect = RuntimeError("transaction rolled back")
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                qr.uninstall(ssh, self.catalog)

    def test_modified_installation_cannot_be_removed(self):
        status = qr.QrUploadStatus(qr.QrUploadState.BROKEN, self.identity, self.package, detail="modified")
        with patch.object(qr, "get_status", return_value=status), patch.object(shared, "remove_shared_features") as remove:
            with self.assertRaisesRegex(RuntimeError, "modified"):
                qr.uninstall(Mock(), self.catalog)
            remove.assert_not_called()

    def test_firmware_migration_does_not_require_appload_for_qr(self):
        import _residue_migration as migration
        old = replace(self.identity, firmware="20260806095513")
        residue = shared.SharedInspection({qr.FEATURE_ID: shared.SharedFeatureState(self.feature, True, "old")}, False, True)
        for available in (True, False):
            target = {qr.FEATURE_ID: self.feature} if available else {}
            with (patch.object(tap, "get_device_identity", return_value=self.identity),
                  patch.object(shared, "has_shared_artifacts", return_value=True),
                  patch.object(shared, "read_shared_identity", return_value=(old.firmware, old.platform, old.architecture, old.xochitl_sha256)),
                  patch.object(tap, "_trusted_shared_context", side_effect=[(self.runtime, self.trusted, ()), (self.runtime, target, ())]),
                  patch.object(tap, "_trusted_alternatives_for_identity", return_value={}),
                  patch.object(shared, "inspect_shared_firmware_residue", return_value=residue),
                  patch.object(migration.appload, "app_asset", return_value=None)):
                report = migration.inspect_residue(Mock())
            self.assertTrue(report.migratable)
            self.assertEqual(report.features[0].target_available, available)

    def test_migration_to_153_uses_exact_peers_and_skips_unsupported_launcher(self):
        import _residue_migration as migration
        import _reading_enhancements as reading
        target = next(p for p in self.catalog if p.release_version == "3.29.0.153")
        identity = tap.DeviceIdentity(target.firmware, target.platform, target.architecture, target.xochitl_sha256)
        for release in ("3.28.0.172", "3.29.0.152"):
            source = next(p for p in reading._trusted_catalog() if p.platform == "chiappa" and p.release_version == release)
            old = tap.DeviceIdentity(source.firmware, source.platform, source.architecture, source.xochitl_sha256)
            _, trusted, _ = tap._trusted_shared_context(old)
            ids = {"native-chinese", "pinyin-input", "reading-enhancements", "note-enhancements"}
            if release == "3.28.0.172":
                ids.update(("qr-upload", "weread-launcher"))
            states = {name: shared.SharedFeatureState(trusted[name], True, "old") for name in ids}
            with (patch.object(tap, "get_device_identity", return_value=identity),
                  patch.object(shared, "has_shared_artifacts", return_value=True),
                  patch.object(shared, "read_shared_identity", return_value=(old.firmware, old.platform, old.architecture, old.xochitl_sha256)),
                  patch.object(shared, "inspect_shared_firmware_residue", return_value=shared.SharedInspection(states, False, True))):
                report = migration.inspect_residue(Mock())
            self.assertTrue(report.migratable, report)
            self.assertEqual({f.feature_id for f in report.features if f.target_available}, ids)

    def test_desktop_section_exposes_lifecycle_and_restart_confirmation(self):
        from rmtool import QtCore, QtWidgets
        from _toolbox_reading import QrUploadSection
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        class Connection(QtCore.QObject):
            connection_changed = QtCore.pyqtSignal(bool)
            def is_connected(self): return True
        section = QrUploadSection(Connection())
        try:
            for state in (qr.QrUploadState.NOT_INSTALLED, qr.QrUploadState.ENABLED, qr.QrUploadState.INSTALLED_DISABLED):
                section._apply_status(qr.QrUploadStatus(state, self.identity, self.package, self.catalog, recovery_available=True))
                self.assertEqual(section.install_button.isEnabled(), state is not qr.QrUploadState.ENABLED)
                self.assertEqual(section.uninstall_button.isEnabled(), state is not qr.QrUploadState.NOT_INSTALLED)
            self.assertTrue(section.cleanup_legacy_button.isHidden())
            self.assertIn("200 MB", section.detail_label.text())
        finally:
            section.deleteLater()
            app.processEvents()
