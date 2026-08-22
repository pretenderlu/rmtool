import importlib.util
import json
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import _native_chinese as native
import _pinyin_input as pinyin
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
        self.assertEqual(len(packages), 14)
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
                ("chiappa", "3.28.0.166"),
                ("chiappa", "3.28.0.169"),
                ("ferrari", "3.27.1.0"),
                ("ferrari", "3.27.3.0"),
                ("ferrari", "3.28.0.162"),
                ("ferrari", "3.28.0.163"),
                ("ferrari", "3.28.0.164"),
                ("ferrari", "3.28.0.166"),
                ("ferrari", "3.28.0.169"),
            },
        )
        self.assertTrue(all(item.offline_verified for item in packages))
        self.assertEqual(
            {
                (item.platform, item.release_version)
                for item in packages
                if item.device_verified
            },
            {("chiappa", "3.27.3.0"), ("ferrari", "3.28.0.166")},
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

    def test_ferrari_166_uses_catalog_for_keyboard_label_and_preserves_language_label(self):
        package = native.select_package(native._trusted_catalog(), self.identity())
        self.assertIsNotNone(package)
        qmd = Path("native-chinese/qmd/ferrari-3.28.0.166.qmd").read_bytes()
        spec = package.file(native.QMD_PATH)
        self.assertEqual(
            (len(qmd), native.hashlib.sha256(qmd).hexdigest()),
            (spec.size, spec.sha256),
        )
        source = qmd.decode("utf-8")
        self.assertNotIn('return "中文";', source)
        self.assertEqual(source.count('"简体中文"'), 2)
        self.assertNotIn(
            'return "中文";',
            Path("pinyin-input/qmd/pinyin-input.qmd").read_text(encoding="utf-8"),
        )

        legacy = Path(
            "native-chinese/qmd/ferrari-3.28.0.162-164.qmd"
        ).read_bytes()
        self.assertEqual(
            (len(legacy), native.hashlib.sha256(legacy).hexdigest()),
            (native.FERRARI_166_V1_QMD_SIZE, native.FERRARI_166_V1_QMD_SHA256),
        )

    def test_ferrari_166_predecessor_is_exact_and_revision_bounded(self):
        package = native.select_package(native._trusted_catalog(), self.identity())
        _runtime, current = native._shared_specs(package)
        v2, v1 = native._known_shared_predecessor_specs(package)
        self.assertEqual(
            (v2.reason, v2.archive_sha256),
            (
                "keyboard_label_qml_override",
                native.FERRARI_166_V2_ARCHIVE_SHA256,
            ),
        )
        self.assertEqual(
            (v1.reason, v1.archive_sha256),
            (
                "unconditional_keyboard_label_qml_override",
                native.FERRARI_166_V1_ARCHIVE_SHA256,
            ),
        )
        for predecessor, qmd_hash, qmd_size in (
            (v2, native.FERRARI_166_V2_QMD_SHA256, native.FERRARI_166_V2_QMD_SIZE),
            (v1, native.FERRARI_166_V1_QMD_SHA256, native.FERRARI_166_V1_QMD_SIZE),
        ):
            self.assertEqual(predecessor.feature.package_id, current.package_id)
            self.assertEqual(predecessor.feature.runtime_path, current.runtime_path)
            self.assertEqual(predecessor.feature.sha256, qmd_hash)
            self.assertEqual(predecessor.feature.size, qmd_size)
            catalog = next(
                item
                for item in predecessor.feature.extra_files
                if item.runtime_path == native.CATALOG_PATH
            )
            self.assertEqual(
                (catalog.size, catalog.sha256),
                (
                    native.FERRARI_166_LEGACY_CATALOG_SIZE,
                    native.FERRARI_166_LEGACY_CATALOG_SHA256,
                ),
            )

        for other in native._trusted_catalog():
            if other is package:
                continue
            predecessors = native._known_shared_predecessor_specs(other)
            expected = native.CATALOG_LABEL_PREDECESSORS.get(
                (other.firmware, other.platform, other.architecture, other.xochitl_sha256)
            )
            if expected is None:
                # Brand-new targets such as Chiappa 3.28.0.166 never had a
                # published predecessor revision to recognize or repair.
                self.assertEqual(len(predecessors), 0)
                continue
            self.assertEqual(len(predecessors), 1)
            self.assertEqual(predecessors[0].reason, "keyboard_label_catalog_missing")
            self.assertEqual(predecessors[0].archive_sha256, expected[0])
            catalog = next(
                item for item in predecessors[0].feature.extra_files
                if item.runtime_path == native.CATALOG_PATH
            )
            self.assertEqual((catalog.sha256, catalog.size), expected[1:])

    def test_native_and_pinyin_old_revisions_are_jointly_strictly_recognized(self):
        native_package = native.select_package(native._trusted_catalog(), self.identity())
        pinyin_package = pinyin.select_package(pinyin._trusted_catalog(), self.identity())
        runtime, native_current = native._shared_specs(native_package)
        _runtime, pinyin_current = pinyin._shared_specs(pinyin_package)
        native_old = native._known_shared_predecessor_specs(native_package)[0]
        pinyin_old = pinyin._known_shared_predecessor_specs(pinyin_package)[0]
        trusted = {
            native.FEATURE_ID: native_current,
            pinyin.FEATURE_ID: pinyin_current,
        }
        expected = shared.SharedInspection({}, False, False)

        def inspect(_ssh, _runtime, candidate, **_kwargs):
            if (
                candidate[native.FEATURE_ID] == native_old.feature
                and candidate[pinyin.FEATURE_ID] == pinyin_old.feature
            ):
                return expected
            raise RuntimeError("not this exact pair")

        with patch.object(shared, "inspect_shared", side_effect=inspect):
            inspection, installed, revisions = native._inspect_shared_revision(
                Mock(), runtime, trusted, native_package
            )
        self.assertIs(inspection, expected)
        self.assertEqual(installed[native.FEATURE_ID], native_old.feature)
        self.assertEqual(installed[pinyin.FEATURE_ID], pinyin_old.feature)
        self.assertEqual(
            revisions,
            {
                native.FEATURE_ID: native_old.reason,
                pinyin.FEATURE_ID: pinyin_old.reason,
            },
        )

        sequential_pairs = (
            (native_old.feature, pinyin_current, native._inspect_shared_revision),
            (native_current, pinyin_old.feature, pinyin._inspect_shared_revision),
        )
        for native_spec, pinyin_spec, probe in sequential_pairs:
            def inspect_sequential(_ssh, _runtime, candidate, **_kwargs):
                if (
                    candidate[native.FEATURE_ID] == native_spec
                    and candidate[pinyin.FEATURE_ID] == pinyin_spec
                ):
                    return expected
                raise RuntimeError("not this sequential state")

            with self.subTest(probe=probe.__module__), patch.object(
                shared, "inspect_shared", side_effect=inspect_sequential
            ):
                if probe is native._inspect_shared_revision:
                    _inspection, installed, selected = probe(
                        Mock(), runtime, trusted, native_package
                    )
                    self.assertEqual(selected, {native.FEATURE_ID: native_old.reason})
                else:
                    _inspection, installed, own_reason = probe(
                        Mock(), runtime, trusted, pinyin_package
                    )
                    self.assertEqual(own_reason, pinyin_old.reason)
                self.assertEqual(installed[native.FEATURE_ID], native_spec)
                self.assertEqual(installed[pinyin.FEATURE_ID], pinyin_spec)

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
                {
                    native.FEATURE_ID,
                    pinyin.FEATURE_ID,
                    "tap-page-turn",
                    "fast-mono-reading",
                },
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
                changed["packages"][0]["urls"][0] = "https://example.invalid/payload"
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

    def test_french_slot_selects_163_164_and_169_on_both_platforms(self):
        expected_release = {
            "3.28.0.163": "3.28.0.163",
            "3.28.0.164": "3.28.0.164",
            "3.28.0.169": "3.28.0.166",
        }
        for raw_identity, policy in native.ALLOWED_TARGETS.items():
            release = policy[0]
            if release not in expected_release:
                continue
            identity = tap.DeviceIdentity(*raw_identity)
            with self.subTest(release=release, platform=identity.platform):
                package = native._bundled_french_slot_package(identity)
                self.assertEqual(package.platform, identity.platform)
                self.assertEqual(package.release_version, expected_release[release])

    def test_french_slot_prefers_exact_xochitl_over_hashless_record(self):
        identity = self.identity()
        exact = native._bundled_french_slot_package(identity)
        hashless = replace(exact, xochitl_sha256="")
        with patch.object(
            _rmkit_cn,
            "parse_translation_manifest",
            return_value={identity.firmware: replace(exact, variants=(hashless,))},
        ):
            selected = native._bundled_french_slot_package(identity)
        self.assertEqual(selected.xochitl_sha256, identity.xochitl_sha256)
        self.assertEqual(selected.asset, exact.asset)

    def test_french_slot_check_fails_closed_without_unique_candidate(self):
        identity = self.identity()
        package = native._bundled_french_slot_package(identity)
        chiappa_163 = next(
            tap.DeviceIdentity(*raw_identity)
            for raw_identity, policy in native.ALLOWED_TARGETS.items()
            if policy[0] == "3.28.0.163" and raw_identity[1] == "chiappa"
        )
        hashless = native._bundled_french_slot_package(chiappa_163)
        ferrari_169 = tap.DeviceIdentity(*native.FERRARI_169_IDENTITY)
        fallback = native._bundled_french_slot_package(ferrari_169)
        cases = (
            (identity, {}),
            (identity, {identity.firmware: replace(package, variants=(package,))}),
            (
                chiappa_163,
                {chiappa_163.firmware: replace(hashless, variants=(hashless,))},
            ),
            (
                ferrari_169,
                {
                    ferrari_169.firmware: replace(
                        fallback,
                        variants=(replace(fallback, xochitl_sha256="f" * 64),),
                    )
                },
            ),
        )
        for current_identity, catalog in cases:
            with self.subTest(catalog=catalog), patch.object(
                _rmkit_cn, "parse_translation_manifest", return_value=catalog
            ), patch.object(_rmkit_cn, "get_localization_status") as get_status:
                with self.assertRaisesRegex(RuntimeError, "无法唯一验证"):
                    native._reject_active_french_slot(Mock(), current_identity)
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

    def test_repair_preserves_exact_old_pinyin_peer_revision(self):
        package = native.select_package(native._trusted_catalog(), self.identity())
        pinyin_package = pinyin.select_package(pinyin._trusted_catalog(), self.identity())
        runtime, current = native._shared_specs(package)
        _runtime, pinyin_current = pinyin._shared_specs(pinyin_package)
        native_old = native._known_shared_predecessor_specs(package)[0].feature
        pinyin_old = pinyin._known_shared_predecessor_specs(pinyin_package)[0].feature
        installed_trusted = {
            native.FEATURE_ID: native_old,
            pinyin.FEATURE_ID: pinyin_old,
        }
        inspection = shared.SharedInspection(
            {
                feature_id: shared.SharedFeatureState(
                    feature,
                    True,
                    "12345678-1234-1234-1234-123456789abc:1:1",
                )
                for feature_id, feature in installed_trusted.items()
            },
            True,
            True,
        )
        expected = native.NativeChineseStatus(
            native.NativeChineseState.ENABLE_PENDING_REBOOT,
            self.identity(),
            package,
            installed=True,
        )
        with patch.object(
            tap, "get_device_identity", return_value=self.identity()
        ), patch.object(
            _rmkit_cn, "has_cjk_font", return_value=True
        ), patch.object(
            tap, "_preflight_device"
        ), patch.object(
            native, "_reject_active_french_slot"
        ), patch.object(
            native,
            "_trusted_shared_context",
            return_value=(
                runtime,
                {
                    native.FEATURE_ID: current,
                    pinyin.FEATURE_ID: pinyin_current,
                },
                (),
            ),
        ), patch.object(
            tap, "extract_verified_package", return_value=Path("/tmp/extracted")
        ), patch.object(
            shared, "_operation_lock", return_value=nullcontext()
        ), patch.object(
            shared, "has_shared_artifacts", return_value=True
        ), patch.object(
            native,
            "_inspect_shared_revision",
            return_value=(
                inspection,
                installed_trusted,
                {
                    native.FEATURE_ID: "keyboard_label_qml_override",
                    pinyin.FEATURE_ID: "keyboard_label_owned_by_pinyin",
                },
            ),
        ), patch.object(
            shared, "_enable_shared_locked"
        ) as activate, patch.object(
            native, "get_status", return_value=expected
        ):
            result = native.enable(Mock(), package, "package.tar.gz", ".rmtool")

        self.assertIs(result, expected)
        self.assertIs(activate.call_args.args[4], installed_trusted)

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

    def test_status_marks_exact_ferrari_166_predecessor_as_repairable(self):
        package = native.select_package(native._trusted_catalog(), self.identity())
        runtime, current = native._shared_specs(package)
        predecessor = native._known_shared_predecessor_specs(package)[0]
        inspection = shared.SharedInspection(
            {
                native.FEATURE_ID: shared.SharedFeatureState(
                    predecessor.feature,
                    True,
                    "12345678-1234-1234-1234-123456789abc:1:1",
                )
            },
            True,
            True,
        )
        with patch.object(
            tap, "get_device_identity", return_value=self.identity()
        ), patch.object(
            shared, "recovery_sentinel_present", return_value=False
        ), patch.object(
            shared, "has_shared_artifacts", return_value=True
        ), patch.object(
            shared, "read_shared_identity", return_value=native.FERRARI_166_IDENTITY
        ), patch.object(
            native,
            "_trusted_shared_context",
            return_value=(runtime, {native.FEATURE_ID: current}, ()),
        ), patch.object(
            native,
            "_inspect_shared_revision",
            return_value=(
                inspection,
                {native.FEATURE_ID: predecessor.feature},
                {native.FEATURE_ID: predecessor.reason},
            ),
        ):
            status = native.get_status(Mock(), (package,))

        self.assertEqual(status.state, native.NativeChineseState.OUTDATED)
        self.assertTrue(status.installed)
        self.assertIn("可直接修复更新", status.detail)

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
            side_effect=lambda *_args, **_kwargs: events.append("remove"),
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
        self.assertTrue(
            remove.call_args.kwargs["tolerate_legacy_templates"]
        )

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
