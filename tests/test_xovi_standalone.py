import hashlib
import io
import json
import posixpath
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import _fast_mono_reading as fast
import _appload as appload
import _native_chinese as native
import _pinyin_input as pinyin
import _tap_page_turn as tap
import _xovi_standalone as shared


class SharedXoviTests(unittest.TestCase):
    TOKEN = "12345678-1234-1234-1234-123456789abc:778:1000"

    @staticmethod
    def contexts():
        tap_packages = tap._trusted_catalog()
        fast_packages = fast._trusted_catalog()
        for fast_package in fast_packages:
            tap_package = next(
                item
                for item in tap_packages
                if item.platform == fast_package.platform
                and item.firmware == fast_package.firmware
                and item.architecture == fast_package.architecture
                and item.xochitl_sha256 == fast_package.xochitl_sha256
            )
            yield tap_package, fast_package

    @staticmethod
    def multifile_feature():
        return shared.SharedFeatureSpec(
            "native-chinese",
            "ferrari-166",
            "native.qmd",
            f"{shared.SHARED_QRR_HOME}/rmtool-native-chinese.qmd",
            "a" * 64,
            10,
            0o644,
            (
                shared.SharedFeatureFileSpec(
                    "extensions.d/native-chinese-translator.so",
                    "extensions.d/native-chinese-translator.so",
                    "b" * 64,
                    11,
                    0o644,
                ),
                shared.SharedFeatureFileSpec(
                    "native-chinese/reMarkable_zh_CN.qm",
                    "native-chinese/reMarkable_zh_CN.qm",
                    "c" * 64,
                    12,
                    0o644,
                ),
            ),
        )

    def shared_residue_ssh(
        self,
        old_tap,
        old_fast,
        *,
        marker_bytes=None,
        modified_path=None,
        extra_path=None,
        active=False,
        visible_dropins=(),
        lower_dropins=(),
        feature_ids=None,
        disabled_feature_ids=(),
        feature_overrides=None,
        legacy_launcher=False,
        layout=None,
        startup_pending=False,
        pending_mode=0o600,
        pending_size=0,
    ):
        layout = layout or shared.SHARED_LAYOUT
        identity = tap.DeviceIdentity(
            old_tap.firmware,
            old_tap.platform,
            old_tap.architecture,
            old_tap.xochitl_sha256,
        )
        runtime, trusted, _legacies = tap._trusted_shared_context(identity)
        installed = dict(trusted)
        installed.update(feature_overrides or {})
        states = {
            feature_id: shared.SharedFeatureState(
                spec,
                feature_id not in disabled_feature_ids,
                self.TOKEN,
            )
            for feature_id, spec in installed.items()
            if feature_ids is None or feature_id in feature_ids
        }
        enabled = tuple(state.spec for state in states.values() if state.enabled)
        launcher = shared.shared_launcher(
            runtime,
            enabled,
            recovery_sentinel=not legacy_launcher,
            startup_guard=(not legacy_launcher and layout == shared.SHARED_LAYOUT),
            layout=layout,
        ).encode()
        dropin = shared.shared_dropin(runtime, enabled, layout=layout).encode()
        marker = shared.shared_marker(
            runtime,
            states,
            hashlib.sha256(launcher).hexdigest(),
            hashlib.sha256(dropin).hexdigest(),
        )
        if marker_bytes is not None:
            marker = marker_bytes
        expected = {item.path: item for item in runtime.files}
        expected.update({
            item.runtime_path: shared.SharedFileSpec(
                item.runtime_path,
                item.sha256,
                item.size,
                item.mode,
            )
            for state in states.values()
            if state.enabled
            for item in state.spec.files
        })
        expected.update(
            {
                "launcher.sh": shared.SharedFileSpec(
                    "launcher.sh", hashlib.sha256(launcher).hexdigest(), len(launcher), 0o755
                ),
                f"systemd/{shared.SHARED_LAYOUT.dropin_name}": shared.SharedFileSpec(
                    f"systemd/{shared.SHARED_LAYOUT.dropin_name}",
                    hashlib.sha256(dropin).hexdigest(),
                    len(dropin),
                    0o644,
                ),
                "package.json": shared.SharedFileSpec(
                    "package.json", hashlib.sha256(marker).hexdigest(), len(marker), 0o644
                ),
            }
        )
        dirs = set()
        for path in expected:
            parent = posixpath.dirname(path)
            while parent:
                dirs.add(parent)
                parent = posixpath.dirname(parent)
        dirs.update(shared._parent_directories(
            item.runtime_path
            for state in states.values()
            if not state.enabled
            for item in state.spec.files
        ))
        if startup_pending:
            expected["startup.pending"] = shared.SharedFileSpec(
                "startup.pending",
                hashlib.sha256(b"").hexdigest(),
                0,
                0o600,
            )
        base = layout.remote_base
        records = [f"41ed|0|0|0|{base}"]
        records.extend(f"41ed|0|0|0|{base}/{path}" for path in sorted(dirs))
        for path, item in sorted(expected.items()):
            mode = pending_mode if path == "startup.pending" else item.mode
            size = pending_size if path == "startup.pending" else item.size
            records.append(f"{0o100000 | mode:x}|0|0|{size}|{base}/{path}")
        if extra_path:
            records.append(f"81a4|0|0|1|{base}/{extra_path}")
        hashes = {
            f"{base}/{path}": item.sha256 for path, item in expected.items()
        }
        if shared.SHARED_LAYOUT.dropin_path in visible_dropins:
            hashes[shared.SHARED_LAYOUT.dropin_path] = hashlib.sha256(
                dropin
            ).hexdigest()
        if modified_path:
            hashes[f"{base}/{modified_path}"] = "0" * 64
        present = {base, f"{base}/package.json", *visible_dropins}
        if startup_pending:
            present.add(f"{base}/startup.pending")
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path in present
        def open_remote(*_args, **_kwargs):
            remote = MagicMock()
            remote.__enter__.return_value = io.BytesIO(marker)
            return remote

        ssh.open_remote.side_effect = open_remote
        def execute_raw(command):
            if command.startswith("[ -e "):
                exists = any(f"[ -e {path} ]" in command for path in present)
                return "", "", 0 if exists else 1
            return "", "", 0 if active else 1

        ssh.exec_command.side_effect = execute_raw

        def execute(command):
            if command.startswith("for file in /etc/systemd/system"):
                return "\n".join(visible_dropins)
            if command.startswith("stat -c '%f|%u|%g"):
                return "\n".join(records)
            if command.startswith("stat -c '%f %u %g'"):
                return "81a4 0 0"
            if command.startswith("sha256sum"):
                path = command.split("sha256sum ", 1)[1]
                return f"{hashes[path]}  {path}\n"
            if "rmtool-xovi-residue-check" in command:
                return "\n".join(lower_dropins)
            if "rmtool-xovi-check" in command:
                if shared.SHARED_LAYOUT.dropin_path in lower_dropins:
                    raise RuntimeError("底层 shared drop-in 仍存在")
                return ""
            raise AssertionError(command)

        ssh.exec_checked.side_effect = execute
        return ssh, present, runtime, trusted, identity

    def test_all_targets_have_identical_common_runtime(self):
        pairs = tuple(self.contexts())
        self.assertEqual(len(pairs), 12)
        for tap_package, fast_package in pairs:
            with self.subTest(
                platform=tap_package.platform, firmware=tap_package.firmware
            ):
                tap_runtime, _tap_feature = tap._shared_specs(tap_package)
                fast_runtime, _fast_feature = fast._shared_specs(fast_package)
                self.assertEqual(tap_runtime, fast_runtime)
                identity = tap.DeviceIdentity(
                    fast_package.firmware,
                    fast_package.platform,
                    fast_package.architecture,
                    fast_package.xochitl_sha256,
                )
                for context in (
                    tap._trusted_shared_context(identity),
                    fast._trusted_shared_context(identity),
                ):
                    context_runtime, trusted, legacies = context
                    self.assertEqual(context_runtime, tap_runtime)
                    expected_features = {"tap-page-turn", "fast-mono-reading"}
                    if native.select_package(native._trusted_catalog(), identity):
                        expected_features.add("native-chinese")
                    if pinyin.select_package(pinyin._trusted_catalog(), identity):
                        expected_features.add("pinyin-input")
                    if appload.app_asset(identity):
                        expected_features.update(("appload", "koreader"))
                    self.assertEqual(
                        set(trusted), expected_features
                    )
                    self.assertEqual(len(legacies), 2)

    def test_combined_launcher_and_check_are_order_independent(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        first = shared.shared_launcher(runtime, (tap_feature, fast_feature))
        second = shared.shared_launcher(runtime, (fast_feature, tap_feature))
        self.assertEqual(first, second)
        self.assertIn(tap_feature.runtime_path, first)
        self.assertIn(fast_feature.runtime_path, first)
        command = shared._qmd_check_command("/stage", (fast_feature, tap_feature))
        self.assertIn("tap-page-turn.qmd", command)
        self.assertIn("fast-mono-reading.qmd", command)
        self.assertLess(command.index("cp /stage"), command.index("qmd-tool check"))
        self.assertIn('set -- "$BASE"/extensions.d/*.so', first)
        self.assertIn('*) stock ;;', first)
        self.assertIn("qmd_count", first)

    def test_non_resource_launchers_do_not_change_environment_contract(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        for enabled in ((tap_feature,), (tap_feature, fast_feature)):
            launcher = shared.shared_launcher(runtime, enabled)
            self.assertNotIn("QT_RESOURCE_REBUILDER_PATH", launcher)

    def test_multifile_launcher_has_generic_recovery_and_strict_extensions(self):
        tap_package, _fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        native_feature = self.multifile_feature()

        launcher = shared.shared_launcher(runtime, (tap_feature, native_feature))
        self.assertIn(
            f"[ ! -e {shared.SHARED_RECOVERY_SENTINEL} ] || stock",
            launcher,
        )
        self.assertTrue(
            shared.SHARED_RECOVERY_SENTINEL.startswith(
                "/data/rmtool/"
            )
        )
        self.assertIn('case "${extension##*/}" in', launcher)
        self.assertIn(
            "native-chinese-translator.so|qt-resource-rebuilder.so", launcher
        )
        self.assertIn('[ "$extension_count" -eq 2 ]', launcher)
        self.assertIn("native-chinese/reMarkable_zh_CN.qm", launcher)

        tap_only = shared.shared_launcher(runtime, (tap_feature,))
        self.assertIn(shared.SHARED_RECOVERY_SENTINEL, tap_only)
        legacy = shared.shared_launcher(
            runtime,
            (tap_feature,),
            recovery_sentinel=False,
            startup_guard=False,
        )
        self.assertNotIn(shared.SHARED_RECOVERY_SENTINEL, legacy)

    def test_shared_launcher_uses_data_and_latches_failed_startup(self):
        tap_package, _fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        launcher = shared.shared_launcher(runtime, (tap_feature,))

        self.assertEqual(
            shared.SHARED_LAYOUT.remote_base,
            "/data/rmtool/xovi-standalone",
        )
        self.assertIn("PENDING=/data/rmtool/xovi-standalone/startup.pending", launcher)
        self.assertIn("ln \"$PENDING_TMP\" \"$PENDING\"", launcher)
        self.assertIn("chmod 0600 \"$PENDING_TMP\"", launcher)
        self.assertIn("chown root:root \"$PENDING_TMP\"", launcher)
        self.assertIn("600:0:0:0", launcher)
        self.assertIn(f"sleep {shared.SHARED_STARTUP_STABLE_SECONDS}", launcher)
        self.assertIn('readlink "/proc/$main_pid/exe"', launcher)
        self.assertLess(
            launcher.index("arm_startup_guard || stock"),
            launcher.index("export LD_PRELOAD"),
        )
        self.assertLess(
            launcher.index("clear_guard_when_stable $$ &"),
            launcher.index("exec /usr/bin/xochitl --system", launcher.index("export LD_PRELOAD")),
        )
        self.assertNotIn("systemd-run", launcher)
        self.assertNotIn("nohup", launcher)

    def test_shared_dropin_keeps_early_data_ordering(self):
        tap_package, _fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        text = shared.shared_dropin(runtime, (tap_feature,))

        self.assertIn("After=data.mount", text)
        self.assertNotIn("home.mount", text)
        self.assertNotIn("ConditionPathExists=", text)
        self.assertIn("ExecStart=/bin/sh -c", text)
        self.assertIn("then exec /bin/sh /data/rmtool/xovi-standalone/launcher.sh", text)
        self.assertIn("else exec /usr/bin/xochitl --system", text)
        self.assertIn("KillMode=control-group", text)

        legacy = shared.shared_dropin(
            runtime, (tap_feature,), layout=shared.LEGACY_SHARED_LAYOUT
        )
        self.assertIn("After=home.mount", legacy)
        self.assertIn(
            f"ConditionPathExists={shared.LEGACY_SHARED_LAYOUT.launcher_path}",
            legacy,
        )
        self.assertNotIn("/bin/sh -c", legacy)

    def test_every_new_shared_target_uses_required_mount_ordering(self):
        packages = (*tap._trusted_catalog(), *native._trusted_catalog())
        identities = {
            (
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )
            for package in packages
        }
        for firmware, platform, architecture, xochitl_sha256 in identities:
            identity = tap.DeviceIdentity(
                firmware, platform, architecture, xochitl_sha256
            )
            runtime, trusted, _legacies = tap._trusted_shared_context(identity)
            text = shared.shared_dropin(runtime, trusted.values())
            with self.subTest(platform=platform, firmware=firmware):
                self.assertIn("After=data.mount", text)
                self.assertNotIn("home.mount", text)
                self.assertNotIn("ConditionPathExists=", text)
                self.assertIn("else exec /usr/bin/xochitl --system", text)

    def test_startup_pending_is_strictly_validated_and_reported(self):
        tap_package, fast_package = next(iter(self.contexts()))
        ssh, _present, runtime, trusted, _identity = self.shared_residue_ssh(
            tap_package,
            fast_package,
            visible_dropins=(shared.SHARED_LAYOUT.dropin_path,),
            startup_pending=True,
        )
        inspection = shared.inspect_shared(ssh, runtime, trusted)
        self.assertTrue(inspection.startup_pending)
        self.assertFalse(inspection.active)
        with self.assertRaisesRegex(RuntimeError, "自动启动保护已触发"):
            shared.assert_startup_guard_not_latched(inspection)

        ssh, _present, runtime, trusted, _identity = self.shared_residue_ssh(
            tap_package,
            fast_package,
            visible_dropins=(shared.SHARED_LAYOUT.dropin_path,),
            startup_pending=True,
            pending_mode=0o644,
        )
        with self.assertRaisesRegex(RuntimeError, "权限或大小已变化"):
            shared.inspect_shared(ssh, runtime, trusted)

    def test_exact_home_shared_layout_is_recognized_for_migration(self):
        tap_package, fast_package = next(iter(self.contexts()))
        ssh, _present, runtime, trusted, _identity = self.shared_residue_ssh(
            tap_package,
            fast_package,
            visible_dropins=(shared.SHARED_LAYOUT.dropin_path,),
            layout=shared.LEGACY_SHARED_LAYOUT,
        )
        inspection = shared.inspect_shared(ssh, runtime, trusted)
        self.assertEqual(inspection.layout, shared.LEGACY_SHARED_LAYOUT)

        script = shared.shared_transaction_script(
            "/data/rmtool/xovi-standalone.staging-test",
            "a" * 32,
            (shared.LEGACY_SHARED_LAYOUT,),
            enable_dropin=True,
        )
        self.assertIn(shared.LEGACY_SHARED_LAYOUT.remote_base, script)
        self.assertIn(shared.SHARED_LAYOUT.remote_base, script)
        self.assertNotIn("upper-1", script)

    def test_revision_inspection_accepts_only_explicit_same_identity_specs(self):
        feature = self.multifile_feature()
        predecessor = replace(feature, sha256="d" * 64, size=9)
        runtime = Mock()
        expected = shared.SharedInspection({}, False, False)

        with patch.object(
            shared,
            "inspect_shared",
            side_effect=(RuntimeError("current mismatch"), expected),
        ) as inspect:
            inspection, installed, revisions = shared.inspect_shared_revisions(
                Mock(),
                runtime,
                {feature.feature_id: feature},
                {feature.feature_id: (("v1", predecessor),)},
            )
        self.assertIs(inspection, expected)
        self.assertEqual(installed[feature.feature_id], predecessor)
        self.assertEqual(revisions, {feature.feature_id: "v1"})
        self.assertEqual(inspect.call_count, 2)

        forged = replace(predecessor, package_id="other-package")
        with self.assertRaisesRegex(RuntimeError, "前代功能规格无效"):
            shared.inspect_shared_revisions(
                Mock(),
                runtime,
                {feature.feature_id: feature},
                {feature.feature_id: (("forged", forged),)},
            )

    def test_emergency_sentinel_clear_requires_root_owned_regular_file(self):
        ssh = Mock()
        ssh.exec_checked.return_value = ""

        with (
            patch.object(
                shared, "recovery_sentinel_present", side_effect=(True, False)
            ),
            patch.object(
                shared,
                "_remote_entry_exists",
                side_effect=lambda _ssh, path: path == shared.SHARED_RECOVERY_SENTINEL,
            ),
        ):
            shared.clear_recovery_sentinel(ssh)

        commands = [call.args[0] for call in ssh.exec_checked.call_args_list]
        clear = next(
            command for command in commands if "stat -c '%a:%u:%g:%s'" in command
        )
        self.assertIn("[ -f", clear)
        self.assertIn("[ ! -L", clear)
        self.assertIn("= '600:0:0:0'", clear)
        self.assertIn(shared.SHARED_RECOVERY_SENTINEL, clear)

    def test_emergency_sentinel_set_is_atomic_root_owned_and_never_restarts(self):
        ssh = Mock()
        ssh.exec_checked.return_value = ""

        with (
            patch.object(shared, "_remote_entry_exists", return_value=False),
            patch.object(shared, "recovery_sentinel_present", return_value=True),
        ):
            shared.set_recovery_sentinel(ssh)

        commands = [call.args[0] for call in ssh.exec_checked.call_args_list]
        create = next(command for command in commands if " ln " in command)
        self.assertIn("umask 077", create)
        self.assertIn("chmod 0600", create)
        self.assertIn("chown root:root", create)
        self.assertIn(shared.SHARED_RECOVERY_SENTINEL, create)
        self.assertNotIn("mount", create)
        self.assertFalse(any("restart" in command or "reboot" in command for command in commands))
        self.assertTrue(any("600:0:0:0" in command for command in commands))

    def test_old_home_emergency_sentinel_is_validated_then_mirrored_to_data(self):
        ssh = Mock()
        ssh.exec_checked.return_value = ""

        with (
            patch.object(
                shared,
                "_remote_entry_exists",
                side_effect=lambda _ssh, path: path == shared.LEGACY_RECOVERY_SENTINEL,
            ),
            patch.object(shared, "recovery_sentinel_present", return_value=True),
        ):
            shared.set_recovery_sentinel(ssh)

        commands = [call.args[0] for call in ssh.exec_checked.call_args_list]
        self.assertTrue(any(shared.LEGACY_RECOVERY_SENTINEL in item for item in commands))
        self.assertTrue(any(
            " ln " in item and shared.SHARED_RECOVERY_SENTINEL in item
            for item in commands
        ))

    def test_emergency_sentinel_detects_dangling_symlink_and_refuses_it(self):
        ssh = Mock()
        ssh.exec_command.return_value = ("", "", 0)
        ssh.exec_checked.side_effect = ("", RuntimeError("invalid sentinel"), "")

        self.assertTrue(shared.recovery_sentinel_present(ssh))
        with self.assertRaisesRegex(RuntimeError, "invalid sentinel"):
            shared.set_recovery_sentinel(ssh)

    def test_feature_layout_rejects_runtime_peer_and_qmd_path_collisions(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        native = self.multifile_feature()

        collisions = (
            shared.SharedFeatureSpec(
                "native-chinese",
                native.package_id,
                native.archive_path,
                runtime.files[0].path,
                native.sha256,
                native.size,
                native.mode,
            ),
            shared.SharedFeatureSpec(
                "native-chinese",
                native.package_id,
                native.archive_path,
                tap_feature.runtime_path,
                native.sha256,
                native.size,
                native.mode,
            ),
            shared.SharedFeatureSpec(
                "native-chinese",
                native.package_id,
                native.archive_path,
                "native-chinese/outside-qrr.qmd",
                native.sha256,
                native.size,
                native.mode,
            ),
        )
        for feature in collisions:
            with self.subTest(path=feature.runtime_path):
                with self.assertRaises(RuntimeError):
                    shared.assert_feature_layout(
                        runtime, (tap_feature, fast_feature, feature)
                    )

        shared.assert_feature_layout(
            runtime, (tap_feature, fast_feature, native)
        )

    def test_inspection_accepts_exact_legacy_launcher_marker(self):
        tap_package, fast_package = next(iter(self.contexts()))
        ssh, _present, runtime, trusted, _identity = self.shared_residue_ssh(
            tap_package,
            fast_package,
            visible_dropins=(shared.SHARED_LAYOUT.dropin_path,),
            legacy_launcher=True,
        )
        inspection = shared.inspect_shared(ssh, runtime, trusted)
        self.assertEqual(set(inspection.states), set(trusted))
        self.assertTrue(inspection.dropin_present)

    def test_multifile_owned_tree_rejects_modified_and_unknown_extra_paths(self):
        tap_package, fast_package = next(iter(self.contexts()))
        _runtime, tap_feature = tap._shared_specs(tap_package)
        native_feature = self.multifile_feature()
        overrides = {
            tap_feature.feature_id: tap_feature,
            native_feature.feature_id: native_feature,
        }
        for modified_path, extra_path in (
            ("native-chinese/reMarkable_zh_CN.qm", None),
            (None, "extensions.d/untrusted.so"),
        ):
            with self.subTest(modified=modified_path, extra=extra_path):
                ssh, _present, runtime, trusted, _identity = self.shared_residue_ssh(
                    tap_package,
                    fast_package,
                    feature_ids=("tap-page-turn", "native-chinese"),
                    feature_overrides=overrides,
                    modified_path=modified_path,
                    extra_path=extra_path,
                    visible_dropins=(shared.SHARED_LAYOUT.dropin_path,),
                )
                with self.assertRaises(RuntimeError):
                    shared.inspect_shared(ssh, runtime, trusted)

    def test_shared_dropin_always_reaches_fail_open_launcher(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        text = shared.shared_dropin(runtime, (tap_feature, fast_feature))
        conditions = [line for line in text.splitlines() if line.startswith("ConditionPathExists=")]
        self.assertEqual(conditions, [])
        self.assertIn(shared.SHARED_LAYOUT.launcher_path, text)
        self.assertIn("else exec /usr/bin/xochitl --system", text)
        self.assertNotIn(".qmd", text)
        self.assertIn("After=data.mount", text)
        self.assertNotIn("home.mount", text)

    def test_marker_rejects_unknown_feature_and_self_declared_hash(self):
        tap_package, fast_package = next(iter(self.contexts()))
        _runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        trusted = {
            tap_feature.feature_id: tap_feature,
            fast_feature.feature_id: fast_feature,
        }
        record = {
            "enabled": True,
            "package_id": tap_feature.package_id,
            "qmd_path": tap_feature.runtime_path,
            "qmd_sha256": tap_feature.sha256,
            "process_token": self.TOKEN,
        }
        with self.assertRaisesRegex(RuntimeError, "未知功能"):
            shared._parse_states({"features": {"third-plugin": record}}, trusted)
        record["qmd_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "内置信任清单"):
            shared._parse_states(
                {"features": {tap_feature.feature_id: record}}, trusted
            )
        with self.assertRaisesRegex(RuntimeError, "未知功能"):
            shared._parse_states({"features": {}}, trusted)

    def test_owned_tree_rejects_symlink_non_root_and_extra_paths(self):
        expected = {
            "payload.bin": shared.SharedFileSpec("payload.bin", "1" * 64, 4, 0o644)
        }
        cases = (
            ("41ed|1|0|0|/base\n81a4|0|0|4|/base/payload.bin", "不是 root"),
            ("41ed|0|0|0|/base\na1ff|0|0|4|/base/payload.bin", "类型"),
            (
                "41ed|0|0|0|/base\n81a4|0|0|4|/base/payload.bin\n"
                "81a4|0|0|1|/base/extra",
                "未托管",
            ),
        )
        for listing, message in cases:
            ssh = Mock()
            ssh.exec_checked.side_effect = lambda command, listing=listing: (
                listing
                if command.startswith("stat -c")
                else "1" * 64 + "  /base/payload.bin\n"
            )
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                shared._validate_owned_tree(ssh, "/base", expected, "测试")

    def test_owned_tree_accepts_exact_root_owned_regular_files(self):
        expected = {
            "sub/payload.bin": shared.SharedFileSpec(
                "sub/payload.bin", "1" * 64, 4, 0o644
            )
        }
        ssh = Mock()

        def execute(command):
            if command.startswith("stat -c"):
                return (
                    "41ed|0|0|0|/base\n"
                    "41ed|0|0|0|/base/sub\n"
                    "81a4|0|0|4|/base/sub/payload.bin"
                )
            if command.startswith("sha256sum"):
                return "1" * 64 + "  /base/sub/payload.bin\n"
            raise AssertionError(command)

        ssh.exec_checked.side_effect = execute
        shared._validate_owned_tree(ssh, "/base", expected, "测试")

    def test_owned_tree_accepts_only_empty_dirs_from_disabled_features(self):
        expected = {
            "payload.bin": shared.SharedFileSpec(
                "payload.bin", "1" * 64, 4, 0o644
            )
        }

        def validate(listing):
            ssh = Mock()
            ssh.exec_checked.side_effect = lambda command: (
                listing
                if command.startswith("stat -c")
                else "1" * 64 + "  /base/payload.bin\n"
            )
            shared._validate_owned_tree(
                ssh,
                "/base",
                expected,
                "测试",
                {"native-chinese"},
            )

        validate(
            "41ed|0|0|0|/base\n"
            "41ed|0|0|0|/base/native-chinese\n"
            "81a4|0|0|4|/base/payload.bin"
        )
        with self.assertRaisesRegex(RuntimeError, "未托管"):
            validate(
                "41ed|0|0|0|/base\n"
                "41ed|0|0|0|/base/native-chinese\n"
                "81a4|0|0|1|/base/native-chinese/leftover\n"
                "81a4|0|0|4|/base/payload.bin"
            )

    def test_shared_inspection_accepts_empty_disabled_feature_directory(self):
        tap_package, fast_package = next(
            (tap_package, fast_package)
            for tap_package, fast_package in self.contexts()
            if {
                native.FEATURE_ID,
                pinyin.FEATURE_ID,
            } <= set(tap._trusted_shared_context(tap.DeviceIdentity(
                tap_package.firmware,
                tap_package.platform,
                tap_package.architecture,
                tap_package.xochitl_sha256,
            ))[1])
        )
        ssh, _present, runtime, trusted, _identity = self.shared_residue_ssh(
            tap_package,
            fast_package,
            feature_ids=(native.FEATURE_ID, pinyin.FEATURE_ID),
            disabled_feature_ids=(native.FEATURE_ID,),
            visible_dropins=(shared.SHARED_LAYOUT.dropin_path,),
        )

        inspection = shared.inspect_shared(ssh, runtime, trusted)

        self.assertFalse(inspection.states[native.FEATURE_ID].enabled)
        self.assertTrue(inspection.states[pinyin.FEATURE_ID].enabled)

    def test_active_legacy_trees_validate_against_local_manifests(self):
        tap_package, fast_package = next(iter(self.contexts()))
        for legacy in (tap._legacy_spec(tap_package), fast._legacy_spec(fast_package)):
            expected = {item.path: item for item in legacy.files}
            launcher_sha = str(legacy.marker["launcher_sha256"])
            dropin_sha = str(legacy.marker["dropin_sha256"])
            marker_bytes = (
                json.dumps(dict(legacy.marker), ensure_ascii=True, sort_keys=True) + "\n"
            ).encode("ascii")
            expected.update(
                {
                    "launcher.sh": shared.SharedFileSpec("launcher.sh", launcher_sha, -1, 0o755),
                    f"systemd/{legacy.layout.dropin_name}": shared.SharedFileSpec(
                        f"systemd/{legacy.layout.dropin_name}", dropin_sha, -1, 0o644
                    ),
                    "package.json": shared.SharedFileSpec(
                        "package.json", hashlib.sha256(marker_bytes).hexdigest(), len(marker_bytes), 0o644
                    ),
                }
            )
            hashes = {
                posixpath.join(legacy.layout.remote_base, path): item.sha256
                for path, item in expected.items()
            }
            hashes[legacy.layout.dropin_path] = dropin_sha
            dirs = set()
            for path in expected:
                parent = posixpath.dirname(path)
                while parent:
                    dirs.add(parent)
                    parent = posixpath.dirname(parent)
            records = [f"41ed|0|0|0|{legacy.layout.remote_base}"]
            records.extend(
                f"41ed|0|0|0|{legacy.layout.remote_base}/{path}" for path in sorted(dirs)
            )
            records.extend(
                f"{0o100000 | item.mode:x}|0|0|{item.size if item.size >= 0 else 1}|"
                f"{legacy.layout.remote_base}/{path}"
                for path, item in sorted(expected.items())
            )
            ssh = Mock()
            ssh.file_exists.side_effect = lambda path, legacy=legacy: path in {
                legacy.layout.remote_base, legacy.marker_path, legacy.layout.dropin_path
            }
            remote = MagicMock()
            remote.__enter__.return_value = io.BytesIO(marker_bytes)
            ssh.open_remote.return_value = remote

            def execute(command, records=records, hashes=hashes):
                if command.startswith("stat -c '%f|%u|%g"):
                    return "\n".join(records)
                if command.startswith("stat -c '%f %u %g'"):
                    return "81a4 0 0"
                if command.startswith("sha256sum"):
                    path = command.split("sha256sum ", 1)[1]
                    return f"{hashes[path]}  {path}\n"
                if "mount --bind" in command:
                    return ""
                raise AssertionError(command)

            ssh.exec_checked.side_effect = execute
            with self.subTest(feature=legacy.feature.feature_id):
                self.assertTrue(shared.validate_legacy(ssh, legacy))

    def test_marker_identity_is_only_a_selector_for_bundled_manifests(self):
        tap_package, _fast_package = next(iter(self.contexts()))
        marker = {
            "identity": {
                "firmware": tap_package.firmware,
                "platform": tap_package.platform,
                "architecture": tap_package.architecture,
                "xochitl_sha256": tap_package.xochitl_sha256,
            }
        }
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == shared.SHARED_LAYOUT.remote_base
        remote = MagicMock()
        remote.__enter__.return_value = io.StringIO(json.dumps(marker))
        ssh.open_remote.return_value = remote
        runtime, trusted, _legacies = tap._trusted_shared_context_from_marker(ssh)
        self.assertEqual(runtime.xochitl_sha256, tap_package.xochitl_sha256)
        self.assertEqual(
            set(trusted),
            {
                "tap-page-turn",
                "fast-mono-reading",
                "native-chinese",
                "pinyin-input",
                "appload",
                "koreader",
            },
        )

        marker["identity"]["xochitl_sha256"] = "0" * 64
        remote.__enter__.return_value = io.StringIO(json.dumps(marker))
        with self.assertRaisesRegex(RuntimeError, "内置点击翻页清单"):
            tap._trusted_shared_context_from_marker(ssh)

    def test_firmware_mismatch_status_validates_shared_install_for_recovery(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        context = (
            runtime,
            {tap_feature.feature_id: tap_feature, fast_feature.feature_id: fast_feature},
            (tap._legacy_spec(tap_package), fast._legacy_spec(fast_package)),
        )
        current = tap.DeviceIdentity("20990101000000", "ferrari", "aarch64", "f" * 64)
        inspection = shared.SharedInspection(
            {tap_feature.feature_id: shared.SharedFeatureState(tap_feature, True, self.TOKEN)},
            False,
            True,
        )
        ssh = Mock()
        ssh.file_exists.return_value = False
        for module, state in (
            (tap, tap.TapPageTurnState.INCOMPATIBLE),
            (fast, fast.FastMonoReadingState.INCOMPATIBLE),
        ):
            with (
                patch.object(shared, "has_shared_artifacts", return_value=True),
                patch.object(module, "_trusted_shared_context_from_marker", return_value=context),
                patch.object(shared, "inspect_shared", return_value=inspection) as inspect,
                patch.object(
                    tap,
                    "get_device_identity",
                    return_value=current,
                ),
            ):
                result = module.get_status(ssh, ())
            self.assertEqual(result.state, state)
            expected_recovery = module is tap
            self.assertEqual(
                result.dropin_present if module is tap else result.recovery_available,
                expected_recovery,
            )
            inspect.assert_called_once()

    def test_164_upgrade_recognizes_and_removes_exact_163_shared_state(self):
        for platform in ("chiappa", "ferrari"):
            old_tap = next(
                item
                for item in tap._trusted_catalog()
                if item.platform == platform
                and item.release_version == "3.28.0.163"
            )
            new_tap = next(
                item
                for item in tap._trusted_catalog()
                if item.platform == platform
                and item.release_version == "3.28.0.164"
            )
            old_fast = next(
                item
                for item in fast._trusted_catalog()
                if item.platform == platform
                and item.release_version == "3.28.0.163"
            )
            new_fast = next(
                item
                for item in fast._trusted_catalog()
                if item.platform == platform
                and item.release_version == "3.28.0.164"
            )
            new_identity = tap.DeviceIdentity(
                new_tap.firmware,
                new_tap.platform,
                new_tap.architecture,
                new_tap.xochitl_sha256,
            )
            for module, new_package, expected_state in (
                (tap, new_tap, tap.TapPageTurnState.FIRMWARE_RESIDUE),
                (fast, new_fast, fast.FastMonoReadingState.FIRMWARE_RESIDUE),
            ):
                ssh, present, _runtime, _trusted, _old_identity = (
                    self.shared_residue_ssh(old_tap, old_fast)
                )
                with patch.object(tap, "get_device_identity", return_value=new_identity):
                    status = module.get_status(ssh, (new_package,))
                self.assertEqual(status.state, expected_state)
                self.assertIn("上下层 drop-in", status.detail)
                self.assertIn("两项功能均可安装", status.detail)

                def remove(*_args):
                    present.clear()
                    return shared.SharedInspection({}, False, False)

                with (
                    patch.object(tap, "get_device_identity", return_value=new_identity),
                    patch.object(
                        shared,
                        "remove_shared_firmware_residue",
                        side_effect=remove,
                    ) as cleanup,
                ):
                    result = module.disable(ssh, (new_package,))
                installable = (
                    tap.TapPageTurnState.NOT_INSTALLED
                    if module is tap
                    else fast.FastMonoReadingState.NOT_INSTALLED
                )
                self.assertEqual(result.state, installable)
                self.assertIs(result.package, new_package)
                cleanup.assert_called_once()

    def test_firmware_residue_cleanup_is_available_from_either_feature(self):
        old_tap = next(
            item for item in tap._trusted_catalog()
            if item.platform == "chiappa" and item.release_version == "3.28.0.163"
        )
        old_fast = next(
            item for item in fast._trusted_catalog()
            if item.platform == "chiappa" and item.release_version == "3.28.0.163"
        )
        new_tap = next(
            item for item in tap._trusted_catalog()
            if item.platform == "chiappa" and item.release_version == "3.28.0.164"
        )
        new_fast = next(
            item for item in fast._trusted_catalog()
            if item.platform == "chiappa" and item.release_version == "3.28.0.164"
        )
        current = tap.DeviceIdentity(
            new_tap.firmware,
            new_tap.platform,
            new_tap.architecture,
            new_tap.xochitl_sha256,
        )
        _revision, old_fast_r2 = fast._known_shared_predecessor_specs(old_fast)[0]
        cases = (
            (tap, new_tap, {"fast-mono-reading"}, {"fast-mono-reading": old_fast_r2}),
            (fast, new_fast, {"tap-page-turn"}, None),
        )
        for module, package, feature_ids, overrides in cases:
            ssh, present, _runtime, _trusted, _identity = self.shared_residue_ssh(
                old_tap,
                old_fast,
                feature_ids=feature_ids,
                feature_overrides=overrides,
            )
            with self.subTest(module=module.__name__), patch.object(
                tap, "get_device_identity", return_value=current
            ):
                status = module.get_status(ssh, (package,))
            residue_state = (
                tap.TapPageTurnState.FIRMWARE_RESIDUE
                if module is tap
                else fast.FastMonoReadingState.FIRMWARE_RESIDUE
            )
            self.assertEqual(status.state, residue_state)

            def remove(*_args):
                present.clear()
                return shared.SharedInspection({}, False, False)

            with (
                patch.object(tap, "get_device_identity", return_value=current),
                patch.object(
                    shared,
                    "remove_shared_firmware_residue",
                    side_effect=remove,
                ) as cleanup,
            ):
                result = module.disable(ssh, (package,))
            self.assertEqual(
                result.state,
                tap.TapPageTurnState.NOT_INSTALLED
                if module is tap
                else fast.FastMonoReadingState.NOT_INSTALLED,
            )
            cleanup.assert_called_once()
            installed_trusted = cleanup.call_args.args[2]
            if overrides:
                self.assertEqual(
                    installed_trusted["fast-mono-reading"], old_fast_r2
                )

    def test_164_upgrade_rejects_unknown_old_shared_state(self):
        new_tap = next(
            item
            for item in tap._trusted_catalog()
            if item.platform == "chiappa"
            and item.release_version == "3.28.0.164"
        )
        identity = tap.DeviceIdentity(
            new_tap.firmware,
            new_tap.platform,
            new_tap.architecture,
            new_tap.xochitl_sha256,
        )
        ssh = Mock()
        ssh.file_exists.return_value = False
        with (
            patch.object(shared, "has_shared_artifacts", return_value=True),
            patch.object(
                shared,
                "read_shared_identity",
                return_value=(
                    identity.firmware,
                    identity.platform,
                    identity.architecture,
                    "0" * 64,
                ),
            ),
            patch.object(tap, "get_device_identity", return_value=identity),
        ):
            status = tap.get_status(ssh, (new_tap,))
        self.assertEqual(status.state, tap.TapPageTurnState.BROKEN)

    def test_firmware_residue_requires_exact_tree_and_no_dropins(self):
        old_tap = next(
            item for item in tap._trusted_catalog()
            if item.platform == "ferrari" and item.release_version == "3.28.0.163"
        )
        old_fast = next(
            item for item in fast._trusted_catalog()
            if item.platform == "ferrari" and item.release_version == "3.28.0.163"
        )
        new_tap = next(
            item for item in tap._trusted_catalog()
            if item.platform == "ferrari" and item.release_version == "3.28.0.164"
        )
        current = (
            new_tap.firmware,
            new_tap.platform,
            new_tap.architecture,
            new_tap.xochitl_sha256,
        )
        cases = (
            ({"marker_bytes": b"not-json"}, "有效 JSON"),
            ({"modified_path": "xovi.so"}, "已被修改"),
            ({"extra_path": "unknown.bin"}, "未托管"),
            ({"active": True}, "仍在当前 xochitl"),
            (
                {"visible_dropins": (shared.SHARED_LAYOUT.dropin_path,)},
                "非 rmtool 管理",
            ),
            (
                {"visible_dropins": ("/etc/systemd/system/xochitl.service.d/99-foreign.conf",)},
                "非 rmtool 管理",
            ),
            (
                {"lower_dropins": (shared.SHARED_LAYOUT.dropin_path,)},
                "底层 shared drop-in",
            ),
            (
                {"lower_dropins": ("/etc/systemd/system/xochitl.service.d/99-foreign.conf",)},
                "底层 root",
            ),
        )
        for options, message in cases:
            ssh, _present, runtime, trusted, _identity = self.shared_residue_ssh(
                old_tap,
                old_fast,
                **options,
            )
            with self.subTest(options=options), self.assertRaisesRegex(
                RuntimeError, message
            ):
                shared.inspect_shared_firmware_residue(
                    ssh,
                    runtime,
                    trusted,
                    current,
                )

        ssh, _present, runtime, trusted, identity = self.shared_residue_ssh(
            old_tap,
            old_fast,
        )
        with self.assertRaisesRegex(RuntimeError, "身份相同"):
            shared.inspect_shared_firmware_residue(
                ssh,
                runtime,
                trusted,
                (
                    identity.firmware,
                    identity.platform,
                    identity.architecture,
                    identity.xochitl_sha256,
                ),
            )

    def test_164_upgrade_offers_targeted_removal_for_exact_163_vellum_packages(self):
        for platform in ("chiappa", "ferrari"):
            old_tap = next(
                item for item in tap._trusted_catalog()
                if item.platform == platform and item.release_version == "3.28.0.163"
            )
            new_tap = next(
                item for item in tap._trusted_catalog()
                if item.platform == platform and item.release_version == "3.28.0.164"
            )
            old_fast = next(
                item for item in fast._trusted_catalog()
                if item.platform == platform and item.release_version == "3.28.0.163"
            )
            new_fast = next(
                item for item in fast._trusted_catalog()
                if item.platform == platform and item.release_version == "3.28.0.164"
            )
            identity = tap.DeviceIdentity(
                new_tap.firmware,
                new_tap.platform,
                new_tap.architecture,
                new_tap.xochitl_sha256,
            )
            tap_marker = json.loads(
                tap._vellum_marker(old_tap, enabled=True, process_token=self.TOKEN)
            )
            fast_marker = json.loads(
                fast._vellum_marker(old_fast, enabled=True, process_token=self.TOKEN)
            )
            ssh = Mock()
            ssh.file_exists.side_effect = lambda path: path in {
                tap.VELLUM_BIN,
                tap.MARKER_PATH,
                fast.MARKER_PATH,
                tap.SHARED_QMD,
                fast.SHARED_QMD,
            }
            with (
                patch.object(shared, "has_shared_artifacts", return_value=False),
                patch.object(tap, "get_device_identity", return_value=identity),
                patch.object(tap, "_read_marker", return_value=tap_marker),
                patch.object(
                    tap,
                    "_vellum_installed_version",
                    return_value=tap._vellum_package_version(old_tap),
                ),
                patch.object(tap, "_vellum_payload_valid", return_value=(True, "")),
            ):
                tap_status = tap.get_status(ssh, (old_tap, new_tap))
            self.assertEqual(tap_status.state, tap.TapPageTurnState.LEGACY_VELLUM)

            with (
                patch.object(shared, "has_shared_artifacts", return_value=False),
                patch.object(tap, "get_device_identity", return_value=identity),
                patch.object(fast, "_read_marker", return_value=fast_marker),
                patch.object(
                    tap,
                    "_vellum_installed_version",
                    return_value=fast._vellum_package_version(old_fast),
                ),
                patch.object(
                    fast,
                    "_vellum_payload_revision",
                    return_value=(old_fast.package_revision, ""),
                ),
            ):
                fast_status = fast.get_status(ssh, (old_fast, new_fast))
            self.assertEqual(
                fast_status.state, fast.FastMonoReadingState.LEGACY_VELLUM
            )

            result = object()
            tap_ssh = Mock()
            tap_ssh.file_exists.return_value = False
            with (
                patch.object(tap, "_read_marker", return_value=tap_marker),
                patch.object(tap, "_trusted_catalog", return_value=(old_tap, new_tap)),
                patch.object(tap, "_vellum_payload_valid", return_value=(True, "")),
                patch.object(tap, "_vellum_payload_paths_valid", return_value=True),
                patch.object(tap, "_marker_dir_has_only_marker", return_value=True),
                patch.object(
                    tap,
                    "_vellum_installed_version",
                    side_effect=(tap._vellum_package_version(old_tap), None),
                ),
                patch.object(tap, "get_status", return_value=result),
            ):
                self.assertIs(tap._disable_vellum(tap_ssh, (old_tap, new_tap)), result)
            tap_commands = [call.args[0] for call in tap_ssh.exec_checked.call_args_list]
            self.assertEqual(
                tap_commands[0],
                f"{tap.VELLUM_BIN} del {tap.VELLUM_PACKAGE_NAME}",
            )
            self.assertNotIn("self uninstall", "\n".join(tap_commands))
            self.assertNotIn("--all", "\n".join(tap_commands))

            fast_ssh = Mock()
            fast_ssh.file_exists.return_value = False
            with (
                patch.object(fast, "_read_marker", return_value=fast_marker),
                patch.object(fast, "_trusted_catalog", return_value=(old_fast, new_fast)),
                patch.object(
                    fast,
                    "_vellum_payload_revision",
                    return_value=(old_fast.package_revision, ""),
                ),
                patch.object(fast, "_marker_dir_has_only_marker", return_value=True),
                patch.object(tap, "_vellum_installed_version", return_value=None),
                patch.object(fast, "get_status", return_value=result),
            ):
                self.assertIs(
                    fast._disable_vellum(fast_ssh, (old_fast, new_fast)), result
                )
            fast_commands = [call.args[0] for call in fast_ssh.exec_checked.call_args_list]
            self.assertEqual(
                fast_commands[0],
                f"{tap.VELLUM_BIN} del {fast.VELLUM_PACKAGE_NAME}",
            )
            self.assertNotIn("self uninstall", "\n".join(fast_commands))
            self.assertNotIn("--all", "\n".join(fast_commands))

    def test_shared_process_tokens_map_to_each_feature_status(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        trusted = {
            tap_feature.feature_id: tap_feature,
            fast_feature.feature_id: fast_feature,
        }
        identity = tap.DeviceIdentity(
            tap_package.firmware,
            tap_package.platform,
            tap_package.architecture,
            tap_package.xochitl_sha256,
        )
        changed = "12345678-1234-1234-1234-123456789abc:779:2000"
        for module, package, feature, expected in (
            (
                tap,
                tap_package,
                tap_feature,
                (
                    tap.TapPageTurnState.ENABLE_PENDING_REBOOT,
                    tap.TapPageTurnState.ENABLED,
                    tap.TapPageTurnState.DISABLE_PENDING_REBOOT,
                    tap.TapPageTurnState.INSTALLED_DISABLED,
                ),
            ),
            (
                fast,
                fast_package,
                fast_feature,
                (
                    fast.FastMonoReadingState.ENABLE_PENDING_REBOOT,
                    fast.FastMonoReadingState.ENABLED,
                    fast.FastMonoReadingState.DISABLE_PENDING_REBOOT,
                    fast.FastMonoReadingState.INSTALLED_DISABLED,
                ),
            ),
        ):
            context = (runtime, trusted, ())
            cases = (
                (True, self.TOKEN, False, expected[0]),
                (True, changed, True, expected[1]),
                (False, self.TOKEN, True, expected[2]),
                (False, changed, False, expected[3]),
            )
            for enabled, stored, active, expected_state in cases:
                inspection = shared.SharedInspection(
                    {
                        feature.feature_id: shared.SharedFeatureState(
                            feature, enabled, stored
                        )
                    },
                    active,
                    enabled,
                )
                ssh = Mock()
                ssh.file_exists.return_value = False
                with (
                    patch.object(shared, "has_shared_artifacts", return_value=True),
                    patch.object(module, "_trusted_shared_context", return_value=context),
                    patch.object(shared, "inspect_shared", return_value=inspection),
                    patch.object(tap, "get_device_identity", return_value=identity),
                    patch.object(tap, "_xochitl_process_token", return_value=self.TOKEN),
                ):
                    result = module.get_status(ssh, (package,))
                with self.subTest(
                    module=module.__name__, enabled=enabled, stored=stored, active=active
                ):
                    self.assertEqual(result.state, expected_state)

    def test_clean_install_builds_shared_state_for_each_feature(self):
        tap_package, fast_package = next(iter(self.contexts()))
        for module, package in ((tap, tap_package), (fast, fast_package)):
            runtime, feature = module._shared_specs(package)
            trusted = {feature.feature_id: feature}
            legacy = module._legacy_spec(package)
            ssh = Mock()
            ssh.file_exists.return_value = False
            ssh.exec_checked.return_value = ""
            final = shared.SharedInspection({}, False, True)
            stage = Mock(return_value=("1" * 64, "2" * 64))
            with (
                patch.object(shared, "has_shared_artifacts", return_value=False),
                patch.object(shared, "validate_legacy", return_value=False),
                patch.object(shared, "_process_token", return_value=self.TOKEN),
                patch.object(shared, "_stage_shared", stage),
                patch.object(shared, "shared_transaction_script", return_value="#!/bin/sh\n:"),
                patch.object(shared, "_upload_bytes"),
                patch.object(shared, "inspect_shared", return_value=final),
            ):
                result = shared.enable_shared(
                    ssh, runtime, feature, Path("unused"), trusted, (legacy,)
                )
            self.assertIs(result, final)
            states = stage.call_args.args[2]
            self.assertEqual(set(states), {feature.feature_id})
            self.assertTrue(states[feature.feature_id].enabled)

    def test_enable_replaces_exact_predecessor_and_preserves_peer(self):
        pinyin_package = pinyin._trusted_catalog()[0]
        identity = tap.DeviceIdentity(
            pinyin_package.firmware,
            pinyin_package.platform,
            pinyin_package.architecture,
            pinyin_package.xochitl_sha256,
        )
        runtime, current_pinyin = pinyin._shared_specs(pinyin_package)
        _runtime, trusted, legacies = tap._trusted_shared_context(identity)
        tap_feature = trusted["tap-page-turn"]
        for old in pinyin._known_shared_predecessor_specs(pinyin_package):
            predecessor = old.feature
            installed_trusted = dict(trusted)
            installed_trusted[pinyin.FEATURE_ID] = predecessor
            states = {
                tap_feature.feature_id: shared.SharedFeatureState(
                    tap_feature, True, self.TOKEN
                ),
                predecessor.feature_id: shared.SharedFeatureState(
                    predecessor, True, self.TOKEN
                ),
            }
            inspection = shared.SharedInspection(states, True, True)
            final = shared.SharedInspection({}, True, True)
            ssh = Mock()
            ssh.file_exists.return_value = False
            ssh.exec_checked.return_value = ""
            stage = Mock(return_value=("1" * 64, "2" * 64))
            with self.subTest(reason=old.reason):
                with (
                    patch.object(shared, "has_shared_artifacts", return_value=True),
                    patch.object(
                        shared, "inspect_shared", side_effect=(inspection, final)
                    ) as inspect,
                    patch.object(shared, "validate_legacy", return_value=False),
                    patch.object(shared, "_process_token", return_value=self.TOKEN),
                    patch.object(shared, "_stage_shared", stage),
                    patch.object(
                        shared,
                        "shared_transaction_script",
                        return_value="#!/bin/sh\n:",
                    ),
                    patch.object(shared, "_upload_bytes"),
                ):
                    result = shared._enable_shared_locked(
                        ssh,
                        runtime,
                        current_pinyin,
                        Path("unused"),
                        installed_trusted,
                        legacies,
                    )
            self.assertIs(result, final)
            target_states = stage.call_args.args[2]
            self.assertIs(target_states[tap_feature.feature_id].spec, tap_feature)
            self.assertTrue(target_states[tap_feature.feature_id].enabled)
            self.assertIs(target_states[pinyin.FEATURE_ID].spec, current_pinyin)
            self.assertTrue(target_states[pinyin.FEATURE_ID].enabled)
            previous_sources = stage.call_args.args[5]
            self.assertIn(tap_feature.runtime_path, previous_sources)
            self.assertIn(predecessor.runtime_path, previous_sources)
            final_trusted = inspect.call_args_list[-1].args[2]
            self.assertIs(final_trusted[pinyin.FEATURE_ID], current_pinyin)

    def test_multifile_stage_preserves_every_peer_file(self):
        tap_package, _fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        native_feature = self.multifile_feature()
        states = {
            tap_feature.feature_id: shared.SharedFeatureState(
                tap_feature, True, self.TOKEN
            ),
            native_feature.feature_id: shared.SharedFeatureState(
                native_feature, True, self.TOKEN
            ),
        }
        previous_sources = {
            item.runtime_path: f"/installed/{item.runtime_path}"
            for item in native_feature.files
        }
        enabled = (tap_feature, native_feature)
        launcher = shared.shared_launcher(runtime, enabled).encode()
        dropin = shared.shared_dropin(runtime, enabled).encode()
        marker = b"marker"
        expected_hashes = {
            item.path: item.sha256 for item in runtime.files
        }
        expected_hashes.update({
            item.runtime_path: item.sha256
            for feature in enabled
            for item in feature.files
        })
        expected_hashes.update({
            "launcher.sh": hashlib.sha256(launcher).hexdigest(),
            f"systemd/{shared.SHARED_LAYOUT.dropin_name}": hashlib.sha256(
                dropin
            ).hexdigest(),
            "package.json": hashlib.sha256(marker).hexdigest(),
        })

        def remote_sha(_ssh, path):
            relative = path.split("/stage/", 1)[1]
            return expected_hashes[relative]

        ssh = Mock()
        ssh.exec_checked.return_value = ""
        with (
            patch.object(shared, "_upload_path") as upload_path,
            patch.object(shared, "_upload_bytes"),
            patch.object(shared, "_remote_sha256", side_effect=remote_sha),
            patch.object(shared, "shared_marker", return_value=marker),
        ):
            shared._stage_shared(
                ssh,
                runtime,
                states,
                tap_feature,
                Path("unused"),
                previous_sources,
                "/stage",
            )

        commands = "\n".join(
            call.args[0] for call in ssh.exec_checked.call_args_list
        )
        for item in native_feature.files:
            self.assertIn(
                f"cp /installed/{item.runtime_path} /stage/{item.runtime_path}",
                commands,
            )
        uploaded = {call.args[2] for call in upload_path.call_args_list}
        self.assertIn(f"/stage/{tap_feature.runtime_path}", uploaded)
        self.assertNotIn(f"/stage/{native_feature.runtime_path}", uploaded)

    def test_multifile_disable_removes_all_owned_files_and_keeps_peer(self):
        tap_package, _fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        native_feature = self.multifile_feature()
        trusted = {
            tap_feature.feature_id: tap_feature,
            native_feature.feature_id: native_feature,
        }
        states = {
            tap_feature.feature_id: shared.SharedFeatureState(
                tap_feature, True, self.TOKEN
            ),
            native_feature.feature_id: shared.SharedFeatureState(
                native_feature, True, self.TOKEN
            ),
        }
        inspection = shared.SharedInspection(states, True, True)
        final = shared.SharedInspection({}, True, True)
        launcher = shared.shared_launcher(runtime, (tap_feature,)).encode()
        dropin = shared.shared_dropin(runtime, (tap_feature,)).encode()
        marker = b"marker"

        def remote_sha(_ssh, path):
            if path.endswith("/" + tap_feature.runtime_path):
                return tap_feature.sha256
            if path.endswith("/launcher.sh"):
                return hashlib.sha256(launcher).hexdigest()
            if path.endswith("/" + shared.SHARED_LAYOUT.dropin_name):
                return hashlib.sha256(dropin).hexdigest()
            if path.endswith("/package.json"):
                return hashlib.sha256(marker).hexdigest()
            raise AssertionError(path)

        ssh = Mock()
        ssh.exec_checked.return_value = ""
        with (
            patch.object(
                shared, "inspect_shared", side_effect=(inspection, final)
            ),
            patch.object(shared, "_process_token", return_value=self.TOKEN),
            patch.object(shared, "_upload_bytes"),
            patch.object(shared, "_remote_sha256", side_effect=remote_sha),
            patch.object(shared, "shared_marker", return_value=marker),
            patch.object(
                shared,
                "shared_transaction_script",
                return_value="#!/bin/sh\n:",
            ),
        ):
            result = shared.disable_shared(
                ssh, runtime, native_feature.feature_id, trusted
            )

        self.assertIs(result, final)
        commands = "\n".join(
            call.args[0] for call in ssh.exec_checked.call_args_list
        )
        for item in native_feature.files:
            self.assertIn(
                f"rm -f {shared.SHARED_LAYOUT.remote_base}.staging-",
                commands,
            )
            self.assertIn(item.runtime_path, commands)
        self.assertIn(
            f"rmdir {shared.SHARED_LAYOUT.remote_base}.staging-",
            commands,
        )
        self.assertIn("/native-chinese", commands)
        self.assertNotIn(
            f"rm -f {shared.SHARED_LAYOUT.remote_base}.staging-" + tap_feature.runtime_path,
            commands,
        )

    def test_both_legacy_migration_directions_preserve_peer(self):
        tap_package, fast_package = next(iter(self.contexts()))
        cases = (
            (tap._shared_specs(tap_package)[1], fast._legacy_spec(fast_package)),
            (fast._shared_specs(fast_package)[1], tap._legacy_spec(tap_package)),
        )
        runtime = tap._shared_specs(tap_package)[0]
        trusted = {
            tap._shared_specs(tap_package)[1].feature_id: tap._shared_specs(tap_package)[1],
            fast._shared_specs(fast_package)[1].feature_id: fast._shared_specs(fast_package)[1],
        }
        all_legacies = (tap._legacy_spec(tap_package), fast._legacy_spec(fast_package))
        for incoming, existing in cases:
            ssh = Mock()
            ssh.file_exists.side_effect = lambda path, dropin=existing.layout.dropin_path: path == dropin
            ssh.exec_checked.return_value = ""
            stage = Mock(return_value=("1" * 64, "2" * 64))
            transaction = Mock(return_value="#!/bin/sh\n:")
            final = shared.SharedInspection({}, False, True)
            with (
                patch.object(shared, "has_shared_artifacts", return_value=False),
                patch.object(
                    shared,
                    "validate_legacy",
                    side_effect=lambda _ssh, item: item.feature.feature_id == existing.feature.feature_id,
                ),
                patch.object(shared, "_process_token", return_value=self.TOKEN),
                patch.object(shared, "_stage_shared", stage),
                patch.object(shared, "shared_transaction_script", transaction),
                patch.object(shared, "_upload_bytes"),
                patch.object(shared, "inspect_shared", return_value=final),
            ):
                shared.enable_shared(
                    ssh, runtime, incoming, Path("unused"), trusted, all_legacies
                )
            states = stage.call_args.args[2]
            sources = stage.call_args.args[5]
            self.assertEqual(set(states), set(trusted))
            self.assertTrue(all(item.enabled for item in states.values()))
            self.assertEqual(
                sources[existing.feature.feature_id],
                f"{existing.layout.remote_base}/{existing.feature.archive_path}",
            )
            self.assertEqual(
                tuple(transaction.call_args.args[2]), (existing.layout,)
            )

    def test_legacy_migration_rejects_unmanaged_dropin_before_staging(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, incoming = fast._shared_specs(fast_package)
        tap_legacy = tap._legacy_spec(tap_package)
        ssh = Mock()
        ssh.exec_checked.return_value = (
            "/etc/systemd/system/xochitl.service.d/custom-xovi.conf\n"
        )
        with (
            patch.object(shared, "has_shared_artifacts", return_value=False),
            patch.object(shared, "_stage_shared") as stage,
            self.assertRaisesRegex(RuntimeError, "非 rmtool 管理"),
        ):
            shared.enable_shared(
                ssh,
                runtime,
                incoming,
                Path("unused"),
                {
                    incoming.feature_id: incoming,
                    tap_legacy.feature.feature_id: tap_legacy.feature,
                },
                (tap_legacy,),
            )
        stage.assert_not_called()

    def test_disable_one_keeps_peer_and_last_disable_removes_dropin(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        trusted = {
            tap_feature.feature_id: tap_feature,
            fast_feature.feature_id: fast_feature,
        }
        for states, feature_id, expected_dropin in (
            (
                {
                    tap_feature.feature_id: shared.SharedFeatureState(tap_feature, True, self.TOKEN),
                    fast_feature.feature_id: shared.SharedFeatureState(fast_feature, True, self.TOKEN),
                },
                tap_feature.feature_id,
                True,
            ),
            (
                {tap_feature.feature_id: shared.SharedFeatureState(tap_feature, True, self.TOKEN)},
                tap_feature.feature_id,
                False,
            ),
        ):
            inspection = shared.SharedInspection(states, True, True)
            final = shared.SharedInspection({}, True, expected_dropin)
            ssh = Mock()
            ssh.exec_checked.return_value = ""
            transaction = Mock(return_value="#!/bin/sh\n:")
            with (
                patch.object(shared, "inspect_shared", side_effect=(inspection, final)),
                patch.object(shared, "_process_token", return_value=self.TOKEN),
                patch.object(shared, "_upload_bytes"),
                patch.object(shared, "_remote_sha256", side_effect=lambda _ssh, path: (
                    fast_feature.sha256 if path.endswith(fast_feature.runtime_path)
                    else hashlib.sha256(shared.shared_launcher(runtime, (
                        fast_feature,
                    ) if expected_dropin else ()).encode()).hexdigest() if path.endswith("launcher.sh")
                    else hashlib.sha256(shared.shared_dropin(runtime, (
                        fast_feature,
                    ) if expected_dropin else ()).encode()).hexdigest() if path.endswith(shared.SHARED_LAYOUT.dropin_name)
                    else hashlib.sha256(shared.shared_marker(
                        runtime,
                        {
                            key: shared.SharedFeatureState(value.spec, False, self.TOKEN)
                            if key == feature_id else value
                            for key, value in states.items()
                        },
                        hashlib.sha256(shared.shared_launcher(runtime, (fast_feature,) if expected_dropin else ()).encode()).hexdigest(),
                        hashlib.sha256(shared.shared_dropin(runtime, (fast_feature,) if expected_dropin else ()).encode()).hexdigest(),
                    )).hexdigest()
                )),
                patch.object(shared, "shared_transaction_script", transaction),
            ):
                result = shared.disable_shared(
                    ssh, runtime, feature_id, trusted
                )
            self.assertIs(result, final)
            self.assertEqual(transaction.call_args.kwargs["enable_dropin"], expected_dropin)

    def test_disable_predecessor_preserves_peer_and_rewrites_disabled_spec(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, current_fast = fast._shared_specs(fast_package)
        old_revision, old_fast = fast._known_shared_predecessor_specs(fast_package)[0]
        self.assertEqual(old_revision, 1)
        installed_trusted = {
            tap_feature.feature_id: tap_feature,
            old_fast.feature_id: old_fast,
        }
        states = {
            tap_feature.feature_id: shared.SharedFeatureState(
                tap_feature, True, self.TOKEN
            ),
            old_fast.feature_id: shared.SharedFeatureState(
                old_fast, True, self.TOKEN
            ),
        }
        inspection = shared.SharedInspection(states, True, True)
        final = shared.SharedInspection({}, True, True)
        ssh = Mock()
        ssh.exec_checked.return_value = ""
        marker = Mock(return_value=b"marker")

        def remote_sha(_ssh, path):
            if path.endswith(tap_feature.runtime_path):
                return tap_feature.sha256
            if path.endswith("launcher.sh"):
                return hashlib.sha256(
                    shared.shared_launcher(runtime, (tap_feature,)).encode()
                ).hexdigest()
            if path.endswith(shared.SHARED_LAYOUT.dropin_name):
                return hashlib.sha256(
                    shared.shared_dropin(runtime, (tap_feature,)).encode()
                ).hexdigest()
            return hashlib.sha256(b"marker").hexdigest()

        with (
            patch.object(shared, "inspect_shared", side_effect=(inspection, final)) as inspect,
            patch.object(shared, "_process_token", return_value=self.TOKEN),
            patch.object(shared, "_upload_bytes"),
            patch.object(shared, "_remote_sha256", side_effect=remote_sha),
            patch.object(shared, "shared_marker", marker),
            patch.object(shared, "shared_transaction_script", return_value="#!/bin/sh\n:"),
        ):
            result = shared.disable_shared(
                ssh,
                runtime,
                old_fast.feature_id,
                installed_trusted,
                current_fast,
            )

        self.assertIs(result, final)
        target_states = marker.call_args.args[1]
        self.assertIs(target_states[tap_feature.feature_id].spec, tap_feature)
        self.assertTrue(target_states[tap_feature.feature_id].enabled)
        self.assertIs(target_states[current_fast.feature_id].spec, current_fast)
        self.assertFalse(target_states[current_fast.feature_id].enabled)
        final_trusted = inspect.call_args_list[-1].args[2]
        self.assertIs(final_trusted[current_fast.feature_id], current_fast)

    def test_firmware_residue_cleanup_removes_entire_shared_base(self):
        tap_package, fast_package = next(iter(self.contexts()))
        runtime, tap_feature = tap._shared_specs(tap_package)
        _runtime, fast_feature = fast._shared_specs(fast_package)
        trusted = {
            tap_feature.feature_id: tap_feature,
            fast_feature.feature_id: fast_feature,
        }
        ssh = Mock()
        ssh.exec_checked.return_value = ""
        transaction = Mock(return_value="#!/bin/sh\n:")
        current = (runtime.firmware, runtime.platform, runtime.architecture, "f" * 64)

        with (
            patch.object(shared, "inspect_shared_firmware_residue") as inspect,
            patch.object(shared, "_upload_bytes"),
            patch.object(shared, "shared_transaction_script", transaction),
        ):
            result = shared.remove_shared_firmware_residue(
                ssh,
                runtime,
                trusted,
                current,
            )

        self.assertEqual(result, shared.SharedInspection({}, False, False))
        inspect.assert_called_once_with(ssh, runtime, trusted, current)
        self.assertTrue(transaction.call_args.kwargs["remove_base"])
        self.assertFalse(transaction.call_args.kwargs["enable_dropin"])
        self.assertNotIn(tap_feature.runtime_path, transaction.return_value)
        self.assertNotIn(fast_feature.runtime_path, transaction.return_value)

        script = shared.shared_transaction_script(
            "/empty-stage",
            "a" * 32,
            (),
            enable_dropin=False,
            remove_base=True,
        )
        self.assertIn('rmdir "$BASE"', script)
        self.assertLess(script.index('rmdir "$BASE"'), script.index("COMMITTED=1"))

    def test_transaction_orders_unmount_before_reload_and_never_restarts(self):
        script = shared.shared_transaction_script(
            "/stage", "a" * 32, (tap._STANDALONE_LAYOUT,), enable_dropin=True
        )
        lowered = script.lower()
        self.assertNotIn("restart xochitl", lowered)
        self.assertNotIn("reboot", lowered)
        self.assertIn("DROPINS_SNAPSHOTTED=1", script)
        self.assertIn("STAGE_MOVED=1", script)
        reload_at = script.rindex("systemctl daemon-reload")
        self.assertLess(script.rindex("unmount_root", 0, reload_at), reload_at)
        self.assertIn(tap.DROPIN_PATH, script)
        self.assertIn(shared.SHARED_LAYOUT.dropin_path, script)
        self.assertIn("ROLLBACK_OK=0", script)
        self.assertIn("rollback incomplete; recovery kept", script)
        self.assertIn("/data/rmtool/.xovi-dropins-", script)
        self.assertIn(
            'mount -o remount,ro "$MOUNT_DIR" || return 1', script
        )
        self.assertIn('umount "$MOUNT_DIR" || return 1', script)
        self.assertLess(
            script.index("mount -o remount,ro"),
            script.index("systemctl daemon-reload"),
        )

    def test_operation_lock_releases_after_failure(self):
        ssh = Mock()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with shared._operation_lock(ssh):
                raise RuntimeError("boom")
        commands = [call.args[0] for call in ssh.exec_checked.call_args_list]
        self.assertTrue(commands[0].startswith("mkdir "))
        self.assertTrue(commands[-1].startswith("rmdir "))


if __name__ == "__main__":
    unittest.main()
