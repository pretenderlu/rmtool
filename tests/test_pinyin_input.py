import hashlib
import json
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import _pinyin_input as pinyin
import _fast_mono_reading as fast
import _native_chinese as native
import _tap_page_turn as tap
import _xovi_standalone as shared


class PinyinInputTests(unittest.TestCase):
    def package(self):
        return pinyin._trusted_catalog()[0]

    def identity(self):
        package = self.package()
        return tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )

    def test_manifest_and_ported_qmd_are_exact(self):
        packages = pinyin._trusted_catalog()
        package = self.package()
        identities = {
            (item.firmware, item.platform, item.architecture, item.xochitl_sha256)
            for item in packages
        }
        self.assertEqual(len(packages), 14)
        self.assertEqual(
            {(item.platform, item.release_version) for item in packages},
            {
                (platform, release)
                for platform in ("ferrari", "chiappa")
                for release in ("3.27.1.0", "3.27.3.0", "3.28.0.162", "3.28.0.163", "3.28.0.164")
            }
            | {("ferrari", "3.28.0.166"), ("chiappa", "3.28.0.166"), ("ferrari", "3.28.0.169"), ("chiappa", "3.28.0.169")},
        )
        self.assertEqual(
            identities,
            {
                (item.firmware, item.platform, item.architecture, item.xochitl_sha256)
                for item in native._trusted_catalog()
            },
        )
        self.assertEqual(
            identities,
            {
                (item.firmware, item.platform, item.architecture, item.xochitl_sha256)
                for item in fast._trusted_catalog()
            },
        )
        self.assertLessEqual(
            identities,
            {
                (item.firmware, item.platform, item.architecture, item.xochitl_sha256)
                for item in tap._trusted_catalog()
            },
        )
        qmd = Path("pinyin-input/qmd/pinyin-input.qmd").read_bytes()
        spec = package.file(pinyin.QMD_PATH)
        self.assertEqual((len(qmd), hashlib.sha256(qmd).hexdigest()), (spec.size, spec.sha256))
        self.assertEqual(
            spec.sha256,
            pinyin.V3_QMD_SHA256,
        )
        source = qmd.decode("utf-8")
        self.assertNotIn('return "中文";', source)
        self.assertNotIn("LanguageAndKeyboard.qml", source)
        self.assertTrue(all(item.offline_verified for item in packages))
        self.assertEqual(
            {(item.platform, item.release_version) for item in packages if item.device_verified},
            {("ferrari", "3.28.0.166")},
        )
        rcc = Path("pinyin-input/zh_CN.rcc").read_bytes()
        rcc_spec = package.file(pinyin.RCC_PATH)
        self.assertEqual((len(rcc), hashlib.sha256(rcc).hexdigest()), (333, rcc_spec.sha256))
        self.assertEqual(
            rcc_spec.sha256,
            "5c51487f74f68b3afe2e4b0b11d0994b8f2577304d26551dda9cbcd5d26aa726",
        )

    def test_builder_checks_current_qmd_against_exact_firmware_hashtab(self):
        source = Path("pinyin-input/build_assets.py").read_text(encoding="utf-8")
        self.assertIn("sha256(xochitl.read_bytes()) != package.xochitl_sha256", source)
        self.assertIn('"-hashtabs",', source)
        self.assertIn('"-qmd",', source)
        self.assertIn("tuple(reversed(all_qmds))", source)
        self.assertIn('("pinyin", pinyin_qmd), (name, data)', source)
        self.assertIn('(name, data), ("pinyin", pinyin_qmd)', source)
        self.assertNotIn(
            '"check", str(REPO_ROOT / "pinyin-input/qmd/pinyin-input.qmd")',
            source,
        )

    def test_manifest_rejects_unknown_target_path_and_url(self):
        document = json.loads(pinyin.BUNDLED_MANIFEST.read_text(encoding="utf-8"))
        mutations = (
            lambda entry: entry.update(platform="chiappa"),
            lambda entry: entry["files"][0].update(path="outside"),
            lambda entry: entry["urls"].__setitem__(
                0, "https://example.invalid/payload"
            ),
        )
        for mutate in mutations:
            changed = json.loads(json.dumps(document))
            mutate(changed["packages"][0])
            with self.subTest(mutate=mutate), self.assertRaises(RuntimeError):
                pinyin.parse_manifest(json.dumps(changed).encode())

    def test_manifest_requires_every_exact_target(self):
        document = json.loads(pinyin.BUNDLED_MANIFEST.read_text(encoding="utf-8"))
        for mutation in (
            lambda value: value["packages"].pop(),
            lambda value: value["packages"].append(dict(value["packages"][0])),
        ):
            changed = json.loads(json.dumps(document))
            mutation(changed)
            with self.assertRaises(RuntimeError):
                pinyin.parse_manifest(json.dumps(changed).encode())

    def test_shared_spec_adds_trusted_hook_layout_resource_and_home_service(self):
        runtime, feature = pinyin._shared_specs(self.package())
        self.assertEqual(feature.preload_paths, (pinyin.HOOK_PATH,))
        self.assertEqual(
            feature.strict_metadata_paths,
            (pinyin.HOOK_PATH, pinyin.RCC_PATH),
        )
        self.assertEqual(feature.legacy_resource_path, "")
        self.assertEqual(
            pinyin.RCC_PATH,
            "exthome/qt-resource-rebuilder/zh_CN.rcc",
        )
        self.assertEqual(feature.sidecars[0].remote_path, pinyin.REMOTE_SERVER)
        self.assertEqual(feature.sidecars[0].size, 18481336)
        self.assertEqual(feature.sidecars[0].unit_name, pinyin.UNIT_NAME)
        self.assertEqual(feature.sidecars[0].unit_runtime_path, pinyin.UNIT_PATH)
        launcher = shared.shared_launcher(runtime, (feature,))
        self.assertIn(
            f'$BASE/xovi.so:$BASE/{pinyin.HOOK_PATH}',
            launcher,
        )
        self.assertIn(f"$BASE/{pinyin.UNIT_PATH}", launcher)
        self.assertIn(f"systemctl start --no-block {pinyin.UNIT_NAME}", launcher)
        self.assertNotIn("QT_RESOURCE_REBUILDER_PATH", launcher)
        self.assertIn("|| stock", launcher)
        self.assertIn(
            f"$BASE/{pinyin.HOOK_PATH}", launcher
        )
        self.assertIn("'644:0:0:72232' ] || stock", launcher)
        self.assertIn("'644:0:0:333' ] || stock", launcher)
        dropin = shared.shared_dropin(runtime, (feature,))
        self.assertIn("After=data.mount", dropin)
        self.assertNotIn("home.mount", dropin)
        unit = (pinyin.BUNDLED_MANIFEST.parent.parent / pinyin.UNIT_PATH).read_text(
            encoding="utf-8"
        )
        self.assertIn("After=home.mount", unit)
        self.assertIn("PartOf=xochitl.service", unit)
        self.assertIn("stat -c %%a:%%u:%%g:%%s", unit)
        self.assertNotIn("stat -c %a:%u:%g:%s", unit)
        self.assertIn(pinyin.REMOTE_SERVER, unit)
        self.assertNotIn("systemctl restart", launcher)
        self.assertNotIn("reboot", launcher)

    def test_current_rcc_must_be_an_immediate_qrr_file(self):
        runtime, feature = pinyin._shared_specs(self.package())
        rcc = next(
            item for item in feature.extra_files if item.runtime_path == pinyin.RCC_PATH
        )
        misplaced = replace(
            feature,
            extra_files=tuple(
                replace(item, runtime_path="pinyin-input/zh_CN.rcc")
                if item == rcc else item
                for item in feature.extra_files
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "RCC 必须直接位于"):
            shared.assert_feature_layout(runtime, (misplaced,))

        unowned = replace(feature, strict_metadata_paths=("outside",))
        with self.assertRaisesRegex(RuntimeError, "严格元数据校验"):
            shared.assert_feature_layout(runtime, (unowned,))

    def test_known_predecessors_are_exact_and_revision_bounded(self):
        _runtime, current = pinyin._shared_specs(self.package())
        v5, v4, v3, v2, v1 = pinyin._known_shared_predecessor_specs(self.package())
        self.assertEqual(
            (v5.reason, v5.archive_sha256),
            ("inline_home_sidecar", pinyin.V5_ARCHIVE_SHA256),
        )
        self.assertEqual(
            (v4.reason, v4.archive_sha256),
            ("keyboard_label_owned_by_pinyin", pinyin.V4_ARCHIVE_SHA256),
        )
        self.assertEqual(
            (v3.reason, v3.archive_sha256),
            ("preload_metadata_unchecked", pinyin.V3_ARCHIVE_SHA256),
        )
        self.assertEqual(
            (v2.reason, v2.archive_sha256),
            ("rcc_subdirectory", pinyin.V2_ARCHIVE_SHA256),
        )
        self.assertEqual(
            (v1.reason, v1.archive_sha256),
            ("missing_rcc", pinyin.V1_ARCHIVE_SHA256),
        )
        self.assertEqual(v5.feature.package_id, current.package_id)
        self.assertEqual(v4.feature.package_id, current.package_id)
        self.assertEqual(v3.feature.package_id, current.package_id)
        self.assertEqual(v2.feature.package_id, current.package_id)
        self.assertEqual(v1.feature.package_id, current.package_id)
        self.assertEqual(v4.feature.sha256, pinyin.V4_QMD_SHA256)
        self.assertEqual(v4.feature.size, pinyin.V4_QMD_SIZE)
        self.assertNotIn(
            pinyin.UNIT_PATH,
            {item.runtime_path for item in v5.feature.extra_files},
        )
        self.assertEqual(v4.feature.extra_files, v5.feature.extra_files)
        self.assertEqual(v5.feature.sidecars[0].unit_name, "")
        self.assertEqual(v5.feature.sidecars[0].unit_runtime_path, "")
        self.assertEqual(v3.feature.sha256, current.sha256)
        self.assertEqual(v3.feature.size, current.size)
        self.assertEqual(v3.feature.strict_metadata_paths, ())
        self.assertEqual(v4.feature.strict_metadata_paths, ())
        self.assertNotEqual(v4.feature.sha256, current.sha256)
        self.assertEqual(v2.feature.sha256, pinyin.V3_QMD_SHA256)
        self.assertEqual(v1.feature.sha256, pinyin.V3_QMD_SHA256)
        self.assertEqual(v2.feature.preload_paths, current.preload_paths)
        self.assertEqual(v1.feature.sidecars, v5.feature.sidecars)
        self.assertEqual(v2.feature.legacy_resource_path, pinyin.V2_RCC_PATH)
        self.assertEqual(v1.feature.legacy_resource_path, "")
        self.assertEqual(
            {item.runtime_path for item in v2.feature.files}
            - {item.runtime_path for item in current.files},
            {pinyin.V2_RCC_PATH},
        )
        self.assertEqual(
            {item.runtime_path for item in current.files}
            - {item.runtime_path for item in v1.feature.files},
            {pinyin.RCC_PATH, pinyin.UNIT_PATH},
        )
        self.assertNotIn(
            pinyin.RCC_PATH,
            {item.runtime_path for item in v2.feature.files},
        )
        other = next(
            item for item in pinyin._trusted_catalog()
            if item.release_version != "3.28.0.166"
        )
        self.assertEqual(pinyin._known_shared_predecessor_specs(other), ())

    def test_peer_context_contains_all_four_features(self):
        runtime, trusted, legacies = pinyin._trusted_shared_context(self.identity())
        self.assertEqual(
            set(trusted),
            {"tap-page-turn", "fast-mono-reading", "native-chinese", pinyin.FEATURE_ID},
        )
        self.assertEqual(
            {item.feature.feature_id for item in legacies},
            {"tap-page-turn", "fast-mono-reading"},
        )
        shared.assert_feature_layout(runtime, trusted.values())

    def test_status_is_not_installed_without_artifacts(self):
        ssh = Mock()
        ssh.file_exists.return_value = False
        with patch.object(tap, "get_device_identity", return_value=self.identity()), patch.object(
            shared, "recovery_sentinel_present", return_value=False
        ), patch.object(shared, "has_shared_artifacts", return_value=False):
            status = pinyin.get_status(ssh)
        self.assertEqual(status.state, pinyin.PinyinInputState.NOT_INSTALLED)
        self.assertFalse(status.installed)

    def test_unsupported_firmware_with_verified_peer_is_not_broken(self):
        peer = next(
            item for item in tap._trusted_catalog()
            if item.platform == "rm1"
        )
        identity = tap.DeviceIdentity(
            peer.firmware, peer.platform, peer.architecture, peer.xochitl_sha256
        )
        runtime, trusted, _legacies = tap._trusted_shared_context(identity)
        inspection = shared.SharedInspection({}, True, True)
        ssh = Mock()
        with patch.object(
            tap, "get_device_identity", return_value=identity
        ), patch.object(
            shared, "recovery_sentinel_present", return_value=False
        ), patch.object(
            shared, "has_shared_artifacts", return_value=True
        ), patch.object(
            pinyin, "_has_external_payload", return_value=False
        ), patch.object(
            shared, "read_shared_identity", return_value=(
                identity.firmware,
                identity.platform,
                identity.architecture,
                identity.xochitl_sha256,
            )
        ), patch.object(
            pinyin, "_trusted_shared_context", return_value=(runtime, trusted, ())
        ), patch.object(
            shared, "inspect_shared", return_value=inspection
        ) as inspect:
            status = pinyin.get_status(ssh)

        self.assertEqual(status.state, pinyin.PinyinInputState.INCOMPATIBLE)
        self.assertFalse(status.installed)
        inspect.assert_called_once_with(ssh, runtime, trusted)

    def test_status_marks_all_exact_predecessors_as_repairable(self):
        package = self.package()
        runtime, current = pinyin._shared_specs(package)
        for predecessor in pinyin._known_shared_predecessor_specs(package):
            inspection = shared.SharedInspection(
                {
                    pinyin.FEATURE_ID: shared.SharedFeatureState(
                        predecessor.feature,
                        True,
                        "12345678-1234-1234-1234-123456789abc:1:1",
                    )
                },
                True,
                True,
            )
            ssh = Mock()
            with self.subTest(reason=predecessor.reason), patch.object(
                tap, "get_device_identity", return_value=self.identity()
            ), patch.object(
                shared, "recovery_sentinel_present", return_value=False
            ), patch.object(shared, "has_shared_artifacts", return_value=True), patch.object(
                pinyin, "_has_external_payload", return_value=True
            ), patch.object(shared, "read_shared_identity", return_value=(
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )), patch.object(
                pinyin, "_trusted_shared_context", return_value=(runtime, {pinyin.FEATURE_ID: current}, ())
            ), patch.object(
                pinyin,
                "_inspect_shared_revision",
                return_value=(
                    inspection,
                    {pinyin.FEATURE_ID: predecessor.feature},
                    predecessor.reason,
                ),
            ), patch.object(pinyin, "_validate_external_payload") as validate:
                status = pinyin.get_status(ssh, (package,))
            self.assertEqual(status.state, pinyin.PinyinInputState.OUTDATED)
            self.assertTrue(status.installed)
            self.assertIn("可直接修复更新", status.detail)
            if predecessor.reason == "rcc_subdirectory":
                self.assertIn("无效子目录", status.detail)
                self.assertIn("QRR 不会扫描", status.detail)
            elif predecessor.reason == "keyboard_label_owned_by_pinyin":
                self.assertIn("原生中文补丁", status.detail)
            elif predecessor.reason == "preload_metadata_unchecked":
                self.assertIn("所有权", status.detail)
            validate.assert_called_once_with(ssh, package)

    def test_revision_probe_accepts_only_current_or_known_predecessors(self):
        package = self.package()
        runtime, current = pinyin._shared_specs(package)
        predecessors = pinyin._known_shared_predecessor_specs(package)
        trusted = {pinyin.FEATURE_ID: current}
        expected = shared.SharedInspection({}, False, False)

        for failures, accepted in enumerate(predecessors, start=1):
            calls = 0

            def inspect(_ssh, _runtime, candidate, **_kwargs):
                nonlocal calls
                calls += 1
                feature = candidate[pinyin.FEATURE_ID]
                if feature == accepted.feature:
                    return expected
                raise RuntimeError("not this exact revision")

            with self.subTest(reason=accepted.reason), patch.object(
                shared, "inspect_shared", side_effect=inspect
            ):
                inspection, installed, reason = pinyin._inspect_shared_revision(
                    Mock(), runtime, trusted, package
                )
            self.assertIs(inspection, expected)
            self.assertEqual(installed[pinyin.FEATURE_ID], accepted.feature)
            self.assertEqual(reason, accepted.reason)
            self.assertEqual(calls, failures + 1)

        with patch.object(
            shared, "inspect_shared", side_effect=RuntimeError("modified tree")
        ) as inspect:
            with self.assertRaisesRegex(RuntimeError, "modified tree"):
                pinyin._inspect_shared_revision(Mock(), runtime, trusted, package)
        self.assertEqual(inspect.call_count, len(predecessors) + 1)

    def test_rmkit_owned_ime_is_rejected_before_writes(self):
        ssh = Mock()
        ssh.exec_checked.return_value = pinyin.RMKIT_IME_PATHS[0]
        with self.assertRaisesRegex(RuntimeError, "rmkit 管理"):
            pinyin._assert_no_rmkit_ime(ssh)

    def test_loaded_state_requires_exact_dictionary_server_process(self):
        package = self.package()
        _runtime, feature = pinyin._shared_specs(package)
        inspection = shared.SharedInspection(
            {
                pinyin.FEATURE_ID: shared.SharedFeatureState(
                    feature, True, "12345678-1234-1234-1234-123456789abc:1:1"
                )
            },
            True,
            True,
        )
        ssh = Mock()
        with patch.object(
            tap,
            "_xochitl_process_token",
            return_value="12345678-1234-1234-1234-123456789abc:2:2",
        ), patch.object(pinyin, "_server_running", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "词库服务未在"):
                pinyin._state_from_inspection(ssh, inspection, False)

        with patch.object(
            tap,
            "_xochitl_process_token",
            return_value="12345678-1234-1234-1234-123456789abc:2:2",
        ), patch.object(pinyin, "_server_running", return_value=True):
            state, detail, installed = pinyin._state_from_inspection(
                ssh, inspection, False
            )
        self.assertEqual(state, pinyin.PinyinInputState.ENABLED)
        self.assertEqual(detail, "")
        self.assertTrue(installed)

    def test_enable_rolls_back_home_payload_when_shared_activation_fails(self):
        package = self.package()
        ssh = Mock()
        ssh.exec_checked.return_value = ""
        runtime, feature = pinyin._shared_specs(package)
        with patch.object(tap, "get_device_identity", return_value=self.identity()), patch.object(
            tap, "_preflight_device"
        ), patch.object(pinyin, "_assert_no_rmkit_ime"), patch.object(
            pinyin, "_trusted_shared_context", return_value=(runtime, {pinyin.FEATURE_ID: feature}, ())
        ), patch.object(tap, "extract_verified_package", return_value=Path("/tmp/extracted")), patch.object(
            shared, "_operation_lock", return_value=nullcontext()
        ), patch.object(shared, "has_shared_artifacts", return_value=False
        ), patch.object(pinyin, "_has_external_payload", return_value=False), patch.object(
            pinyin, "_stage_external"
        ), patch.object(shared, "_enable_shared_locked", side_effect=RuntimeError("activate failed")):
            with self.assertRaisesRegex(RuntimeError, "activate failed"):
                pinyin.enable(ssh, package, "package.tar.gz", ".rmtool")
        commands = "\n".join(call.args[0] for call in ssh.exec_checked.call_args_list)
        self.assertIn(f"rm -rf {pinyin.REMOTE_BASE}", commands)
        self.assertNotIn("systemctl restart", commands)
        self.assertNotIn("reboot", commands)

    def test_enable_restores_previous_payload_when_staged_swap_fails(self):
        package = self.package()
        ssh = Mock()

        def execute(command):
            if command.startswith("mv ") and ".staging-" in command:
                raise RuntimeError("swap failed")
            return ""

        ssh.exec_checked.side_effect = execute
        runtime, feature = pinyin._shared_specs(package)
        with patch.object(tap, "get_device_identity", return_value=self.identity()), patch.object(
            tap, "_preflight_device"
        ), patch.object(pinyin, "_assert_no_rmkit_ime"), patch.object(
            pinyin, "_trusted_shared_context", return_value=(runtime, {pinyin.FEATURE_ID: feature}, ())
        ), patch.object(tap, "extract_verified_package", return_value=Path("/tmp/extracted")), patch.object(
            shared, "_operation_lock", return_value=nullcontext()
        ), patch.object(shared, "has_shared_artifacts", return_value=False), patch.object(
            pinyin, "_has_external_payload", return_value=True
        ), patch.object(pinyin, "_validate_external_payload"), patch.object(
            pinyin, "_stage_external"
        ), patch.object(shared, "_enable_shared_locked") as activate:
            with self.assertRaisesRegex(RuntimeError, "swap failed"):
                pinyin.enable(ssh, package, "package.tar.gz", ".rmtool")

        commands = [call.args[0] for call in ssh.exec_checked.call_args_list]
        old_move = next(command for command in commands if command.startswith(
            f"mv {pinyin.REMOTE_BASE} {pinyin.REMOTE_BASE}.backup-"
        ))
        backup = old_move.split()[-1]
        self.assertIn(f"mv {backup} {pinyin.REMOTE_BASE}", commands)
        activate.assert_not_called()

    def test_enable_is_idempotent_after_exact_validation(self):
        package = self.package()
        runtime, feature = pinyin._shared_specs(package)
        inspection = shared.SharedInspection(
            {
                pinyin.FEATURE_ID: shared.SharedFeatureState(
                    feature, True, "12345678-1234-1234-1234-123456789abc:1:1"
                )
            },
            True,
            True,
        )
        expected = pinyin.PinyinInputStatus(
            pinyin.PinyinInputState.ENABLED, self.identity(), package, installed=True
        )
        ssh = Mock()
        with patch.object(tap, "get_device_identity", return_value=self.identity()), patch.object(
            tap, "_preflight_device"
        ), patch.object(pinyin, "_assert_no_rmkit_ime"), patch.object(
            pinyin, "_trusted_shared_context", return_value=(runtime, {pinyin.FEATURE_ID: feature}, ())
        ), patch.object(tap, "extract_verified_package", return_value=Path("/tmp/extracted")), patch.object(
            shared, "_operation_lock", return_value=nullcontext()
        ), patch.object(shared, "has_shared_artifacts", return_value=True), patch.object(
            pinyin, "_inspect_shared_revision", return_value=(inspection, {pinyin.FEATURE_ID: feature}, None)
        ), patch.object(pinyin, "_has_external_payload", return_value=True), patch.object(
            pinyin, "_validate_external_payload"
        ) as validate, patch.object(pinyin, "get_status", return_value=expected), patch.object(
            pinyin, "_stage_external"
        ) as stage, patch.object(shared, "_enable_shared_locked") as activate:
            result = pinyin.enable(ssh, package, "package.tar.gz", ".rmtool")

        self.assertIs(result, expected)
        validate.assert_called_once_with(ssh, package)
        stage.assert_not_called()
        activate.assert_not_called()
        self.assertFalse(any(call.args[0].startswith("mv ") for call in ssh.exec_checked.call_args_list))

    def test_repair_uses_exact_predecessor_and_restores_home_payload_on_failure(self):
        package = self.package()
        runtime, current = pinyin._shared_specs(package)
        native_package = native.select_package(native._trusted_catalog(), self.identity())
        _runtime, native_current = native._shared_specs(native_package)
        native_old = native._known_shared_predecessor_specs(native_package)[0].feature
        for predecessor in pinyin._known_shared_predecessor_specs(package):
            installed_trusted = {
                pinyin.FEATURE_ID: predecessor.feature,
                native.FEATURE_ID: native_old,
            }
            inspection = shared.SharedInspection(
                {
                    pinyin.FEATURE_ID: shared.SharedFeatureState(
                        predecessor.feature,
                        True,
                        "12345678-1234-1234-1234-123456789abc:1:1",
                    ),
                    native.FEATURE_ID: shared.SharedFeatureState(
                        native_old,
                        True,
                        "12345678-1234-1234-1234-123456789abc:1:1",
                    ),
                },
                True,
                True,
            )
            ssh = Mock()
            ssh.exec_checked.return_value = ""
            with self.subTest(reason=predecessor.reason), patch.object(
                tap, "get_device_identity", return_value=self.identity()
            ), patch.object(tap, "_preflight_device"), patch.object(
                pinyin, "_assert_no_rmkit_ime"
            ), patch.object(
                pinyin,
                "_trusted_shared_context",
                return_value=(
                    runtime,
                    {
                        pinyin.FEATURE_ID: current,
                        native.FEATURE_ID: native_current,
                    },
                    (),
                ),
            ), patch.object(
                tap, "extract_verified_package", return_value=Path("/tmp/extracted")
            ), patch.object(
                shared, "_operation_lock", return_value=nullcontext()
            ), patch.object(shared, "has_shared_artifacts", return_value=True), patch.object(
                pinyin,
                "_inspect_shared_revision",
                return_value=(inspection, installed_trusted, predecessor.reason),
            ), patch.object(pinyin, "_has_external_payload", return_value=True), patch.object(
                pinyin, "_validate_external_payload"
            ), patch.object(pinyin, "_stage_external"), patch.object(
                shared, "_enable_shared_locked", side_effect=RuntimeError("repair failed")
            ) as activate:
                with self.assertRaisesRegex(RuntimeError, "repair failed"):
                    pinyin.enable(ssh, package, "package.tar.gz", ".rmtool")
            self.assertIs(activate.call_args.args[4], installed_trusted)
            commands = "\n".join(call.args[0] for call in ssh.exec_checked.call_args_list)
            self.assertIn(f"mv {pinyin.REMOTE_BASE}", commands)
            self.assertIn(f" {pinyin.REMOTE_BASE}", commands)
            self.assertNotIn("systemctl restart", commands)
            self.assertNotIn("reboot", commands)

    def test_disable_restores_home_payload_when_shared_disable_fails(self):
        package = self.package()
        runtime, feature = pinyin._shared_specs(package)
        inspection = shared.SharedInspection(
            {
                pinyin.FEATURE_ID: shared.SharedFeatureState(
                    feature, True, "12345678-1234-1234-1234-123456789abc:1:1"
                )
            },
            True,
            True,
        )
        ssh = Mock()
        ssh.exec_checked.return_value = ""
        with patch.object(tap, "get_device_identity", return_value=self.identity()), patch.object(
            pinyin, "_trusted_shared_context", return_value=(runtime, {pinyin.FEATURE_ID: feature}, ())
        ), patch.object(shared, "_operation_lock", return_value=nullcontext()), patch.object(
            shared, "inspect_shared", return_value=inspection
        ), patch.object(pinyin, "_has_external_payload", return_value=True), patch.object(
            pinyin, "_validate_external_payload"
        ), patch.object(shared, "_disable_shared_locked", side_effect=RuntimeError("disable failed")):
            with self.assertRaisesRegex(RuntimeError, "disable failed"):
                pinyin.disable(ssh, (package,))
        commands = "\n".join(call.args[0] for call in ssh.exec_checked.call_args_list)
        self.assertIn(f"mv {pinyin.REMOTE_BASE}", commands)
        self.assertIn(f" {pinyin.REMOTE_BASE}", commands)
        self.assertNotIn("systemctl restart", commands)
        self.assertNotIn("reboot", commands)

    def test_device_mutation_source_never_restarts_or_reboots(self):
        source = Path(pinyin.__file__).read_text(encoding="utf-8")
        self.assertNotIn("systemctl restart", source)
        self.assertNotIn("restart xochitl", source)
        self.assertNotIn(" reboot", source)


if __name__ == "__main__":
    unittest.main()
