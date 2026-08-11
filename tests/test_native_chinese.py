import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import _native_chinese as native
import _fast_mono_reading as fast
import _rmkit_cn
import _tap_page_turn as tap
import _xovi_standalone as shared


class NativeChineseTests(unittest.TestCase):
    @staticmethod
    def identity():
        return tap.DeviceIdentity(
            native.SUPPORTED_IDENTITY[0],
            native.SUPPORTED_IDENTITY[1],
            native.SUPPORTED_IDENTITY[2],
            native.SUPPORTED_IDENTITY[3],
        )

    def test_manifest_is_complete_dual_source_three_file_feature(self):
        packages = native.parse_manifest(native.BUNDLED_MANIFEST.read_bytes())
        self.assertEqual(len(packages), 11)
        self.assertEqual(
            {
                (item.platform, item.release_version)
                for item in packages
            },
            {
                ("chiappa", "3.27.1.0"),
                ("chiappa", "3.27.3.0"),
                ("chiappa", "3.28.0.162"),
                ("chiappa", "3.28.0.163"),
                ("chiappa", "3.28.0.164"),
                ("ferrari", "3.27.1.0"),
                ("ferrari", "3.27.3.0"),
                ("ferrari", "3.28.0.162"),
                ("ferrari", "3.28.0.163"),
                ("ferrari", "3.28.0.164"),
                ("ferrari", "3.28.0.166"),
            },
        )
        self.assertTrue(all(item.offline_verified for item in packages))
        self.assertEqual(
            {
                (item.platform, item.release_version)
                for item in packages
                if item.device_verified
            },
            {("ferrari", "3.28.0.166"), ("chiappa", "3.27.3.0")},
        )
        for package in packages:
            with self.subTest(platform=package.platform):
                self.assertEqual(
                    package.urls,
                    (
                        f"{native.COS_URL}/{package.asset}",
                        f"{native.GITHUB_URL}/{package.asset}",
                    ),
                )
                self.assertEqual(
                    {item.path for item in package.files}, native.PAYLOAD_PATHS
                )
                runtime, feature = native._shared_specs(package)
                self.assertEqual(
                    {item.runtime_path for item in feature.files},
                    {
                        f"{shared.SHARED_QRR_HOME}/rmtool-native-chinese.qmd",
                        native.EXTENSION_PATH,
                        native.CATALOG_PATH,
                    },
                )
                shared.assert_feature_layout(runtime, (feature,))

    def test_manifest_requires_every_exact_target_and_verification_metadata(self):
        document = json.loads(native.BUNDLED_MANIFEST.read_text(encoding="utf-8"))
        missing = json.loads(json.dumps(document))
        missing["packages"].pop()
        with self.assertRaisesRegex(RuntimeError, "完整且唯一"):
            native.parse_manifest(json.dumps(missing).encode())

        forged = json.loads(json.dumps(document))
        forged["packages"][1]["device_verified"] = False
        with self.assertRaisesRegex(RuntimeError, "精确身份白名单"):
            native.parse_manifest(json.dumps(forged).encode())

    def test_chiappa_package_requires_exact_four_part_identity(self):
        packages = native._trusted_catalog()
        identity = tap.DeviceIdentity(*native.CHIAPPA_3273_IDENTITY)
        selected = native.select_package(packages, identity)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.release_version, "3.27.3.0")
        self.assertTrue(selected.device_verified)
        changed = replace(identity, xochitl_sha256="f" * 64)
        self.assertIsNone(native.select_package(packages, changed))

    def test_build_metadata_and_reviewable_qmd_match_manifest(self):
        path = Path("native-chinese/build_assets.py").resolve()
        spec = importlib.util.spec_from_file_location("native_chinese_builder", path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        packages = native._trusted_catalog()
        bases = builder._target_base_packages()
        records = builder._translation_records()
        self.assertEqual(len(bases), len(packages))
        for base, package in zip(bases, packages):
            with self.subTest(platform=package.platform, release=package.release_version):
                self.assertEqual((
                    base.firmware,
                    base.platform,
                    base.architecture,
                    base.xochitl_sha256,
                ), (
                    package.firmware,
                    package.platform,
                    package.architecture,
                    package.xochitl_sha256,
                ))
                qmd = builder._qmd_bytes()[builder._variant(base.release_version)]
                qmd_expected = package.file(native.QMD_PATH)
                self.assertEqual(
                    (len(qmd), builder.sha256(qmd)),
                    (qmd_expected.size, qmd_expected.sha256),
                )
                catalog = builder._catalog_bytes(base, records)
                catalog_expected = package.file(native.CATALOG_PATH)
                self.assertEqual(
                    (len(catalog), builder.sha256(catalog)),
                    (catalog_expected.size, catalog_expected.sha256),
                )

    def test_translator_reads_catalog_from_early_data_mount(self):
        extension = (
            Path(native.__file__).with_name("native-chinese")
            / "native-chinese-translator.so"
        ).read_bytes()
        self.assertIn(
            "/data/rmtool/xovi-standalone/native-chinese/".encode("utf-16le"),
            extension,
        )
        self.assertNotIn(
            "/home/root/.local/share/rmtool/xovi-standalone/".encode("utf-16le"),
            extension,
        )

    def test_existing_feature_contexts_recognize_native_only_shared_identity(self):
        for context in (
            tap._trusted_shared_context(self.identity()),
            fast._trusted_shared_context(self.identity()),
        ):
            runtime, trusted, legacies = context
            self.assertEqual(
                set(trusted),
                {native.FEATURE_ID, "tap-page-turn", "fast-mono-reading"},
            )
            self.assertEqual(
                {legacy.feature.feature_id for legacy in legacies},
                {"tap-page-turn", "fast-mono-reading"},
            )
            shared.assert_feature_layout(runtime, trusted.values())

    def test_manifest_rejects_unknown_path_and_changed_url_order(self):
        document = json.loads(native.BUNDLED_MANIFEST.read_text(encoding="utf-8"))
        for mutation in ("path", "urls"):
            changed = json.loads(json.dumps(document))
            if mutation == "path":
                changed["packages"][0]["files"][0]["path"] = "unknown.bin"
            else:
                changed["packages"][0]["urls"].reverse()
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                native.parse_manifest(json.dumps(changed).encode())

    def test_manifest_rejects_duplicate_and_untrusted_metadata(self):
        document = json.loads(native.BUNDLED_MANIFEST.read_text(encoding="utf-8"))
        mutations = []

        duplicate = json.loads(json.dumps(document))
        duplicate["packages"].append(duplicate["packages"][0])
        mutations.append(duplicate)

        bad_channel = json.loads(json.dumps(document))
        bad_channel["packages"][1]["channel"] = "beta"
        mutations.append(bad_channel)

        bad_verification = json.loads(json.dumps(document))
        bad_verification["packages"][1]["offline_verified"] = False
        mutations.append(bad_verification)

        extra_url = json.loads(json.dumps(document))
        extra_url["packages"][0]["urls"].append("https://example.invalid/payload")
        mutations.append(extra_url)

        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                native.parse_manifest(json.dumps(changed).encode())

    def test_download_uses_verified_cache_without_network(self):
        data = b"verified local archive"
        package = replace(
            native._trusted_catalog()[0],
            size=len(data),
            sha256=native.hashlib.sha256(data).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            cached = (
                Path(temporary)
                / "cache/native-chinese"
                / package.firmware
                / package.asset
            )
            cached.parent.mkdir(parents=True)
            cached.write_bytes(data)
            with patch.object(tap, "_download_limited") as download:
                self.assertEqual(native.download_package(package, temporary), cached)
            download.assert_not_called()

    def test_enable_rejects_every_managed_french_slot_state(self):
        identity = self.identity()
        bundled = native._bundled_french_slot_package(identity)
        ssh = Mock()
        for state in (
            _rmkit_cn.LocalizationState.ENABLED,
            _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED,
            _rmkit_cn.LocalizationState.INCOMPATIBLE,
        ):
            status = _rmkit_cn.LocalizationStatus(
                state,
                native.SUPPORTED_IDENTITY[0],
            )
            with self.subTest(state=state), patch.object(
                _rmkit_cn, "get_localization_status", return_value=status
            ) as get_status:
                with self.assertRaisesRegex(RuntimeError, "还原法语槽位"):
                    native._reject_active_french_slot(ssh, identity)
            get_status.assert_called_once_with(ssh, bundled)

    def test_enable_allows_only_unmanaged_french_slot(self):
        identity = self.identity()
        status = _rmkit_cn.LocalizationStatus(
            _rmkit_cn.LocalizationState.NOT_INSTALLED,
            native.SUPPORTED_IDENTITY[0],
        )
        with patch.object(
            _rmkit_cn, "get_localization_status", return_value=status
        ) as get_status:
            native._reject_active_french_slot(Mock(), identity)
        self.assertEqual(
            get_status.call_args.args[1],
            native._bundled_french_slot_package(identity),
        )

    def test_french_slot_check_ignores_valid_remote_catalog_without_166(self):
        identity = self.identity()
        status = _rmkit_cn.LocalizationStatus(
            _rmkit_cn.LocalizationState.NOT_INSTALLED,
            identity.firmware,
        )
        with patch.object(
            _rmkit_cn,
            "load_translation_catalog",
            return_value={"20260702125656": Mock()},
        ) as remote, patch.object(
            _rmkit_cn, "get_localization_status", return_value=status
        ) as get_status:
            native._reject_active_french_slot(Mock(), identity)

        remote.assert_not_called()
        package = get_status.call_args.args[1]
        self.assertEqual(package.firmware, identity.firmware)
        self.assertEqual(package.platform, identity.platform)
        self.assertEqual(package.xochitl_sha256, identity.xochitl_sha256)

    def test_chiappa_french_slot_allows_missing_historical_xochitl_hash(self):
        identity = tap.DeviceIdentity(*native.CHIAPPA_3273_IDENTITY)
        package = native._bundled_french_slot_package(identity)

        self.assertEqual(package.firmware, identity.firmware)
        self.assertEqual(package.platform, identity.platform)
        self.assertEqual(package.xochitl_sha256, "")

    def test_french_slot_rejects_wrong_recorded_xochitl_hash(self):
        identity = tap.DeviceIdentity(*native.CHIAPPA_3273_IDENTITY)
        package = native._bundled_french_slot_package(identity)
        changed = replace(package, xochitl_sha256="f" * 64)
        with patch.object(
            _rmkit_cn,
            "parse_translation_manifest",
            return_value={identity.firmware: changed},
        ), self.assertRaisesRegex(RuntimeError, "无法唯一验证"):
            native._bundled_french_slot_package(identity)

    def test_french_slot_check_fails_closed_without_unique_bundled_166(self):
        identity = self.identity()
        package = native._bundled_french_slot_package(identity)
        ambiguous = replace(package, variants=(package,))
        for catalog in ({}, {identity.firmware: ambiguous}):
            with self.subTest(catalog=catalog), patch.object(
                _rmkit_cn, "parse_translation_manifest", return_value=catalog
            ), patch.object(_rmkit_cn, "get_localization_status") as get_status:
                with self.assertRaisesRegex(RuntimeError, "无法唯一验证"):
                    native._reject_active_french_slot(Mock(), identity)
            get_status.assert_not_called()

    def test_enable_checks_cjk_before_preflight_and_french_slot_inspection(self):
        package = native._trusted_catalog()[0]
        events = []

        def reject(*_args):
            events.append("french")
            raise RuntimeError("stop")

        with patch.object(
            tap, "get_device_identity", return_value=self.identity()
        ), patch.object(
            tap, "_preflight_device", side_effect=lambda _ssh: events.append("preflight")
        ), patch.object(
            _rmkit_cn, "has_cjk_font", side_effect=lambda _ssh: events.append("font") or True
        ), patch.object(
            native,
            "_reject_active_french_slot",
            side_effect=reject,
        ), patch.object(shared, "enable_shared") as deploy:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                native.enable(Mock(), package, "unused.tar.gz", ".rmtool")

        self.assertEqual(events, ["font", "preflight", "french"])
        deploy.assert_not_called()

    def test_enable_rejects_missing_cjk_before_french_or_deployment(self):
        package = native._trusted_catalog()[1]
        identity = tap.DeviceIdentity(*native.CHIAPPA_3273_IDENTITY)
        with patch.object(
            tap, "get_device_identity", return_value=identity
        ), patch.object(tap, "_preflight_device"), patch.object(
            _rmkit_cn, "has_cjk_font", return_value=False
        ), patch.object(native, "_reject_active_french_slot") as french, patch.object(
            shared, "enable_shared"
        ) as deploy:
            with self.assertRaisesRegex(RuntimeError, "字体管理.*系统字体"):
                native.enable(Mock(), package, "unused.tar.gz", ".rmtool")
        french.assert_not_called()
        deploy.assert_not_called()

    def test_disable_switches_selected_chinese_to_english_before_removal(self):
        package = native._trusted_catalog()[0]
        runtime, feature = native._shared_specs(package)
        inspection = shared.SharedInspection(
            {
                native.FEATURE_ID: shared.SharedFeatureState(
                    feature,
                    True,
                    "12345678-1234-1234-1234-123456789abc:1:1",
                )
            },
            True,
            True,
        )
        events = []
        with patch.object(tap, "get_device_identity", return_value=self.identity()), patch.object(
            shared, "read_shared_identity", return_value=native.SUPPORTED_IDENTITY
        ), patch.object(
            native,
            "_trusted_shared_context",
            return_value=(runtime, {native.FEATURE_ID: feature}, ()),
        ), patch.object(shared, "inspect_shared", return_value=inspection), patch.object(
            _rmkit_cn,
            "_read_bytes",
            return_value=b"[General]\nlanguage=zh_CN\n[Other]\nvalue=1\n",
        ), patch.object(
            _rmkit_cn,
            "_write_remote_bytes",
            side_effect=lambda _ssh, _path, data: events.append(("config", data)),
        ), patch.object(
            _rmkit_cn,
            "_flush_remote_writes",
            side_effect=lambda _ssh: events.append(("sync", b"")),
        ), patch.object(
            shared,
            "_disable_shared_locked",
            side_effect=lambda *_args: events.append(("disable", b"")),
        ), patch.object(native, "get_status") as get_status:
            native.disable(Mock(), (package,))

        self.assertEqual([name for name, _data in events], ["config", "sync", "disable"])
        self.assertIn(b"language=en", events[0][1])
        self.assertNotIn(b"language=zh_CN", events[0][1])
        get_status.assert_called_once()

    def test_status_reports_and_clear_uses_generic_emergency_sentinel(self):
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == shared.SHARED_RECOVERY_SENTINEL
        with patch.object(tap, "get_device_identity", return_value=self.identity()):
            status = native.get_status(ssh)
        self.assertEqual(status.state, native.NativeChineseState.NOT_INSTALLED)
        self.assertTrue(status.emergency_disabled)

        with patch.object(shared, "clear_recovery_sentinel") as clear, patch.object(
            native, "get_status", return_value=status
        ):
            native.clear_emergency_disable(ssh)
        clear.assert_called_once_with(ssh)

    def test_unsupported_device_with_peer_shared_feature_is_not_broken(self):
        identity = tap.DeviceIdentity(
            "20260612085811", "chiappa", "aarch64", "f" * 64
        )
        runtime = Mock()
        peer = Mock(feature_id="tap-page-turn")
        inspection = shared.SharedInspection(
            {
                "tap-page-turn": shared.SharedFeatureState(
                    peer,
                    True,
                    "12345678-1234-1234-1234-123456789abc:1:1",
                )
            },
            True,
            True,
        )
        ssh = Mock()
        with patch.object(
            tap, "get_device_identity", return_value=identity
        ), patch.object(
            shared, "recovery_sentinel_present", return_value=False
        ), patch.object(
            shared, "has_shared_artifacts", return_value=True
        ), patch.object(
            shared,
            "read_shared_identity",
            return_value=(
                identity.firmware,
                identity.platform,
                identity.architecture,
                identity.xochitl_sha256,
            ),
        ), patch.object(
            native,
            "_trusted_shared_context",
            return_value=(runtime, {"tap-page-turn": peer}, ()),
        ), patch.object(
            shared, "inspect_shared", return_value=inspection
        ):
            status = native.get_status(ssh)

        self.assertEqual(status.state, native.NativeChineseState.INCOMPATIBLE)
        self.assertFalse(status.installed)
        self.assertIn("其他共享功能不受影响", status.detail)

    def test_status_recognizes_own_shared_firmware_residue(self):
        package = native._trusted_catalog()[0]
        runtime, feature = native._shared_specs(package)
        current = tap.DeviceIdentity(
            "20990101000000", "ferrari", "aarch64", "f" * 64
        )
        inspection = shared.SharedInspection(
            {
                native.FEATURE_ID: shared.SharedFeatureState(
                    feature,
                    True,
                    "12345678-1234-1234-1234-123456789abc:1:1",
                )
            },
            False,
            False,
        )
        ssh = Mock()
        with patch.object(tap, "get_device_identity", return_value=current), patch.object(
            shared, "recovery_sentinel_present", return_value=False
        ), patch.object(shared, "has_shared_artifacts", return_value=True), patch.object(
            shared, "read_shared_identity", return_value=native.SUPPORTED_IDENTITY
        ), patch.object(
            native,
            "_trusted_shared_context",
            return_value=(runtime, {native.FEATURE_ID: feature}, ()),
        ), patch.object(
            shared, "inspect_shared_firmware_residue", return_value=inspection
        ) as inspect:
            status = native.get_status(ssh)

        self.assertEqual(status.state, native.NativeChineseState.FIRMWARE_RESIDUE)
        self.assertTrue(status.installed)
        self.assertIsNone(status.package)
        inspect.assert_called_once_with(
            ssh,
            runtime,
            {native.FEATURE_ID: feature},
            (current.firmware, current.platform, current.architecture, current.xochitl_sha256),
        )

    def test_disable_removes_verified_shared_firmware_residue(self):
        package = native._trusted_catalog()[0]
        runtime, feature = native._shared_specs(package)
        current = tap.DeviceIdentity(
            "20990101000000", "ferrari", "aarch64", "f" * 64
        )
        inspection = shared.SharedInspection(
            {
                native.FEATURE_ID: shared.SharedFeatureState(
                    feature,
                    True,
                    "12345678-1234-1234-1234-123456789abc:1:1",
                )
            },
            False,
            False,
        )
        events = []
        with patch.object(tap, "get_device_identity", return_value=current), patch.object(
            shared, "read_shared_identity", return_value=native.SUPPORTED_IDENTITY
        ), patch.object(
            native,
            "_trusted_shared_context",
            return_value=(runtime, {native.FEATURE_ID: feature}, ()),
        ), patch.object(
            shared, "inspect_shared_firmware_residue", return_value=inspection
        ), patch.object(
            native,
            "_switch_selected_chinese_to_english",
            side_effect=lambda _ssh: events.append("language"),
        ), patch.object(
            shared,
            "remove_shared_firmware_residue",
            side_effect=lambda *_args: events.append("remove"),
        ) as remove, patch.object(shared, "_disable_shared_locked") as disable_current, patch.object(
            native, "get_status"
        ):
            native.disable(Mock(), (package,))

        self.assertEqual(events, ["language", "remove"])
        disable_current.assert_not_called()
        self.assertEqual(remove.call_args.args[-1], (
            current.firmware,
            current.platform,
            current.architecture,
            current.xochitl_sha256,
        ))

    def test_emergency_set_and_clear_use_shared_marker_helpers(self):
        ssh = Mock()
        status = native.NativeChineseStatus(
            native.NativeChineseState.EMERGENCY_DISABLED,
            self.identity(),
            native._trusted_catalog()[0],
            installed=True,
            emergency_disabled=True,
        )
        with patch.object(shared, "set_recovery_sentinel") as set_marker, patch.object(
            native, "get_status", return_value=status
        ):
            self.assertIs(native.set_emergency_disable(ssh), status)
        set_marker.assert_called_once_with(ssh)


if __name__ == "__main__":
    unittest.main()
