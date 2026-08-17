import hashlib
import inspect
import io
import json
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import _fast_mono_reading as fast
import _tap_page_turn as tap


class FakeSSH:
    def __init__(self, *, files=(), dropins=""):
        self.files = set(files)
        self.dropins = dropins
        self.commands = []
        self.transfers = {}

    def file_exists(self, path):
        return path in self.files

    def exec_checked(self, command):
        self.commands.append(command)
        if command.startswith(f"find {fast.REMOTE_BASE} "):
            return "\n".join(
                sorted(
                    path
                    for path in self.files
                    if path.rpartition("/")[0] == fast.REMOTE_BASE
                )
            )
        if command.startswith(f"rm -f {fast.MARKER_PATH}; rmdir {fast.REMOTE_BASE}"):
            self.files.discard(fast.MARKER_PATH)
            if not any(
                path.rpartition("/")[0] == fast.REMOTE_BASE
                for path in self.files
            ):
                self.files.discard(fast.REMOTE_BASE)
            return ""
        if command.startswith("for file in /etc/systemd/system/xochitl.service.d/"):
            return self.dropins
        if command.startswith("readlink -f /home/root/xovi/services"):
            return (
                "/home/root/xovi/extensions.d\n"
                "/home/root/xovi/exthome\n"
            )
        return ""

    def exec_command(self, _command):
        return "", "", 1

    def transfer_file(self, local, remote):
        self.transfers[remote] = Path(local).read_bytes()


class FastMonoReadingTests(unittest.TestCase):
    def packages(self):
        return fast.parse_manifest(
            Path("fast-mono-reading/manifest.json").read_bytes()
        )

    def package(self):
        return self.package_for("chiappa", "20260612085811")

    def package_for(self, platform, firmware):
        return next(
            package
            for package in self.packages()
            if package.platform == platform and package.firmware == firmware
        )

    def tap_package_for(self, package=None):
        package = package or self.package()
        identity = tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        return tap.select_package(tap._trusted_catalog(), identity)

    def vellum_status(
        self,
        package,
        *,
        revision,
        qmd_sha256,
        enabled=True,
        installed_version=None,
        owns_qmd=True,
        paths_valid=True,
        extra_marker=None,
    ):
        token = "12345678-1234-1234-1234-123456789abc:1:1"
        marker = json.loads(
            fast._vellum_marker(package, enabled=enabled, process_token=token)
        )
        marker["vellum_version"] = f"{package.release_version}-r{revision}"
        marker["qmd_sha256"] = qmd_sha256
        if extra_marker:
            marker.update(extra_marker)
        files = {fast.MARKER_PATH, tap.VELLUM_BIN}
        if enabled:
            files.add(fast.SHARED_QMD)
        ssh = FakeSSH(files=files)
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        installed = (
            f"{package.release_version}-r{revision}"
            if installed_version is None and enabled
            else installed_version
        )
        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            fast, "_read_marker", return_value=marker
        ), patch.object(
            tap, "_vellum_installed_version", return_value=installed
        ), patch.object(
            tap, "_vellum_package_owns_path", return_value=owns_qmd
        ), patch.object(
            fast, "_vellum_payload_paths_valid", return_value=paths_valid
        ), patch.object(
            tap, "_remote_sha256", return_value=qmd_sha256
        ), patch.object(
            tap, "_xochitl_process_token", return_value="new-token"
        ), patch.object(
            tap, "_active_with_shared_xovi", return_value=True
        ):
            return fast.get_status(ssh, (package,))

    def test_repository_manifest_matches_complete_local_allowlist(self):
        packages = self.packages()
        identities = {
            (
                package.platform,
                package.firmware,
                package.architecture,
                package.xochitl_sha256,
            ): (
                package.release_version,
                package.channel,
                package.offline_verified,
                package.device_verified,
            )
            for package in packages
        }
        self.assertEqual(identities, fast.ALLOWED_TARGETS)
        self.assertEqual(len(packages), 12)
        self.assertTrue(all(package.offline_verified for package in packages))
        self.assertFalse(any(package.device_verified for package in packages))
        self.assertEqual({package.channel for package in packages}, {"stable", "beta"})
        self.assertTrue(all(package.package_revision == 4 for package in packages))
        verified = self.package_for("chiappa", "20260612085811")
        self.assertEqual(
            verified.file(fast.QMD_PAYLOAD_PATH).sha256,
            "a53c7de04cdb33a4ad15ea7afae976e2310854e2ce5868b5941af7ebd12d0279",
        )
        for platform in ("chiappa", "ferrari"):
            package = next(
                item
                for item in packages
                if item.platform == platform
                and item.release_version == "3.28.0.164"
            )
            self.assertEqual(
                package.file(fast.QMD_PAYLOAD_PATH).sha256,
                "5ad0a13fff4a49716b2b2c31cf96a048d5f3cf23a6d6f615ea874c5043a3554f",
            )
            self.assertTrue(package.asset.endswith("-3.28.0.164.tar.gz"))
        ferrari_166 = next(
            item
            for item in packages
            if item.platform == "ferrari"
            and item.release_version == "3.28.0.166"
        )
        self.assertEqual(
            ferrari_166.file(fast.QMD_PAYLOAD_PATH).sha256,
            "5ad0a13fff4a49716b2b2c31cf96a048d5f3cf23a6d6f615ea874c5043a3554f",
        )
        self.assertFalse(ferrari_166.device_verified)

    def test_known_shared_predecessors_are_exact_and_revision_bounded(self):
        predecessors = {
            package.firmware: fast._known_shared_predecessor_specs(package)
            for package in self.packages()
        }
        self.assertEqual(
            {
                spec.sha256
                for specs in predecessors.values()
                for _revision, spec in specs
            },
            {
                "0fa777c1278318d1f98d18e7bbdbbb5dfadbd5baf463e4d7a8df0107c36a0f9d",
                "587844a02383b70b1851b78b1d0bb3a5a2ff6c38559d6d3c78ac673bd964f18f",
                "4d8f829d81d83f84d37e16668a3366468758c04b4247b809f8f843d6d0abcc8d",
                "7fec635a5939b1929959e84464bccfe0788d905e91d1e1704f1d0ec980237a4a",
                "643f5569e65149798888d267f616b77034b3abb9f1b695806d12f6c22a378cea",
                "6949f58896651a3254c9e143461b384892f4d779e8f2553f9adf11ff8fe5707d",
                "9eb1e98a731458f1b46b170e11bfd29d11edbec04caf8befedc859fefd9acf5d",
            },
        )
        self.assertTrue(all(len(specs) in (0, 3, 4) for specs in predecessors.values()))
        self.assertTrue(
            all(
                {revision for revision, _spec in specs} == {1, 2, 3}
                for specs in predecessors.values()
                if specs
            )
        )
        self.assertEqual(
            {
                (spec.sha256, spec.size)
                for specs in predecessors.values()
                for _revision, spec in specs
            },
            {
                (
                    "0fa777c1278318d1f98d18e7bbdbbb5dfadbd5baf463e4d7a8df0107c36a0f9d",
                    3990,
                ),
                (
                    "4d8f829d81d83f84d37e16668a3366468758c04b4247b809f8f843d6d0abcc8d",
                    9327,
                ),
                (
                    "587844a02383b70b1851b78b1d0bb3a5a2ff6c38559d6d3c78ac673bd964f18f",
                    3106,
                ),
                (
                    "7fec635a5939b1929959e84464bccfe0788d905e91d1e1704f1d0ec980237a4a",
                    8448,
                ),
                (
                    "643f5569e65149798888d267f616b77034b3abb9f1b695806d12f6c22a378cea",
                    12017,
                ),
                (
                    "6949f58896651a3254c9e143461b384892f4d779e8f2553f9adf11ff8fe5707d",
                    11138,
                ),
                (
                    "9eb1e98a731458f1b46b170e11bfd29d11edbec04caf8befedc859fefd9acf5d",
                    11339,
                ),
            },
        )
        current = self.package()
        self.assertEqual(
            fast._known_shared_predecessor_specs(
                replace(current, package_revision=current.package_revision + 1)
            ),
            (),
        )

    def test_vellum_enabled_predecessors_are_legacy_removal_targets(self):
        package = self.package()
        for revision, predecessor in fast._known_shared_predecessor_specs(package):
            with self.subTest(revision=revision):
                status = self.vellum_status(
                    package,
                    revision=revision,
                    qmd_sha256=predecessor.sha256,
                )
                self.assertEqual(status.state, fast.FastMonoReadingState.LEGACY_VELLUM)
                self.assertTrue(status.recovery_available)

    def test_predecessor_revision_does_not_depend_on_record_order(self):
        package = self.package()
        key = (package.package_revision, package.firmware)
        records = fast._KNOWN_SHARED_PREDECESSOR_QMDS[key]
        with patch.dict(
            fast._KNOWN_SHARED_PREDECESSOR_QMDS,
            {key: tuple(reversed(records))},
        ):
            predecessors = fast._known_shared_predecessor_specs(package)
            self.assertEqual([revision for revision, _spec in predecessors], [3, 2, 1])
            revision, r1 = predecessors[-1]
            status = self.vellum_status(
                package,
                revision=revision,
                qmd_sha256=r1.sha256,
            )
        self.assertEqual(revision, 1)
        self.assertEqual(status.state, fast.FastMonoReadingState.LEGACY_VELLUM)

    def test_vellum_predecessor_rejects_modified_or_mismatched_payload(self):
        package = self.package()
        revision, r2 = fast._known_shared_predecessor_specs(package)[1]
        self.assertEqual(revision, 2)
        cases = (
            (
                "unknown qmd",
                dict(revision=2, qmd_sha256="a" * 64),
            ),
            (
                "wrong apk version",
                dict(
                    revision=2,
                    qmd_sha256=r2.sha256,
                    installed_version=f"{package.release_version}-r1",
                ),
            ),
            (
                "wrong ownership",
                dict(revision=2, qmd_sha256=r2.sha256, owns_qmd=False),
            ),
            (
                "unexpected payload path",
                dict(revision=2, qmd_sha256=r2.sha256, paths_valid=False),
            ),
            (
                "unexpected marker field",
                dict(
                    revision=2,
                    qmd_sha256=r2.sha256,
                    extra_marker={"unexpected": True},
                ),
            ),
            (
                "wrong package id",
                dict(
                    revision=2,
                    qmd_sha256=r2.sha256,
                    extra_marker={"package_id": "other-package"},
                ),
            ),
            (
                "invalid enabled state",
                dict(
                    revision=2,
                    qmd_sha256=r2.sha256,
                    extra_marker={"enabled": 1},
                ),
            ),
            (
                "invalid process token",
                dict(
                    revision=2,
                    qmd_sha256=r2.sha256,
                    extra_marker={"process_token": "invalid"},
                ),
            ),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                status = self.vellum_status(package, **arguments)
                self.assertEqual(status.state, fast.FastMonoReadingState.BROKEN)

    def test_vellum_disabled_predecessor_keeps_strict_absence_rules(self):
        package = self.package()
        revision, r2 = fast._known_shared_predecessor_specs(package)[1]
        self.assertEqual(revision, 2)
        status = self.vellum_status(
            package,
            revision=2,
            qmd_sha256=r2.sha256,
            enabled=False,
        )
        self.assertEqual(status.state, fast.FastMonoReadingState.LEGACY_VELLUM)

        status = self.vellum_status(
            package,
            revision=2,
            qmd_sha256=r2.sha256,
            enabled=False,
            installed_version=f"{package.release_version}-r2",
        )
        self.assertEqual(status.state, fast.FastMonoReadingState.BROKEN)

    def test_current_vellum_r4_enabled_status_is_unchanged(self):
        package = self.package()
        status = self.vellum_status(
            package,
            revision=package.package_revision,
            qmd_sha256=package.file(fast.QMD_PAYLOAD_PATH).sha256,
        )
        self.assertEqual(status.state, fast.FastMonoReadingState.LEGACY_VELLUM)

    def test_shared_revision_accepts_only_exact_known_predecessor(self):
        package = self.package_for("ferrari", "20260702125656")
        runtime, current = fast._shared_specs(package)
        predecessors = fast._known_shared_predecessor_specs(package)
        _revision, predecessor = predecessors[1]
        trusted = {current.feature_id: current}
        inspection = fast._xovi_standalone.SharedInspection({}, False, True)

        with patch.object(
            fast._xovi_standalone,
            "inspect_shared",
            side_effect=(
                RuntimeError("current mismatch"),
                RuntimeError("r1 mismatch"),
                inspection,
            ),
        ) as inspect:
            result, installed_trusted, outdated = fast._inspect_shared_revision(
                Mock(), runtime, trusted, package
            )

        self.assertIs(result, inspection)
        self.assertTrue(outdated)
        self.assertEqual(
            installed_trusted[current.feature_id].sha256,
            predecessor.sha256,
        )
        self.assertEqual(inspect.call_count, 3)

        with patch.object(
            fast._xovi_standalone,
            "inspect_shared",
            side_effect=(
                RuntimeError("current mismatch"),
                RuntimeError("not r1"),
                RuntimeError("not r2"),
                RuntimeError("not r3-a"),
                RuntimeError("not r3-b"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "current mismatch"):
                fast._inspect_shared_revision(Mock(), runtime, trusted, package)

    def test_qmd_sources_define_periodic_stock_cleanup(self):
        for version in ("3.27", "3.28"):
            source = Path(
                f"fast-mono-reading/qmd-src/fast-mono-reading-{version}.qmd"
            ).read_text(encoding="utf-8")
            with self.subTest(version=version):
                self.assertEqual(source.count("Connections {"), 1)
                self.assertIn("function onCurrentPageChanged()", source)
                self.assertNotIn("onCurrentPageChanged:", source)
                self.assertIn("property int rmtoolFastMonoCleanupInterval: 10", source)
                self.assertIn("rmtoolFastMonoCleanupTimer.stop()", source)
                self.assertIn("if (page === root.rmtoolFastMonoLastPage)", source)
                self.assertIn("function onDocumentChanged()", source)
                self.assertIn("function onRmtoolFastMonoReadingEnabledChanged()", source)
                self.assertIn("function onRmtoolFastMonoCleanupIntervalChanged()", source)
                self.assertIn(
                    'function onDocumentChanged() {\n'
                    '                    root.rmtoolFastMonoLastDocumentId = ""\n'
                    '                    root.rmtoolResetFastMonoCleanup()\n'
                    '                    root.rmtoolFastMonoLastPage = -1',
                    source,
                )
                self.assertIn(
                    "function onRmtoolFastMonoReadingEnabledChanged() {\n"
                    "                    root.rmtoolFastMonoLastDocumentId = root.document\n"
                    '                        ? root.document.id.toString() : ""\n'
                    "                    root.rmtoolResetFastMonoCleanup()\n"
                    "                    root.rmtoolFastMonoLastPage = root.currentPage",
                    source,
                )
                self.assertIn("interval: 500", source)
                self.assertIn("repeat: false", source)
                self.assertIn(
                    'root.ghostBuster.forceClearNow("rmtool fast mono periodic cleanup")',
                    source,
                )
                self.assertNotIn("ArkControls.Dropdown", source)
                self.assertNotIn("ArkControls.TextInput", source)
                self.assertEqual(source.count("ArkControls.FoldoutItem {"), 6)
                self.assertIn("id: rmtoolFastMonoCleanupOptions", source)
                self.assertIn('label: "\\u5f3a\\u5236\\u5237\\u65b0"', source)
                self.assertIn("description: {", source)
                self.assertIn(
                    "onClicked: stackView.push(rmtoolFastMonoCleanupOptions)",
                    source,
                )
                self.assertIn(
                    "visible: (root.toolbar.rmtoolDocumentView?.rmtoolFastMonoReadingAvailable ?? false)\n"
                    "                    && (root.toolbar.rmtoolDocumentView?.rmtoolFastMonoReadingEnabled ?? false)",
                    source,
                )
                self.assertIn(
                    "if (root.rmtoolFastMonoReadingAvailable\n"
                    "                        && root.rmtoolFastMonoReadingEnabled\n"
                    "                        && root.rmtoolFastMonoCleanupInterval > 0)",
                    source,
                )
                self.assertIn(
                    "if (!root.rmtoolFastMonoReadingAvailable\n"
                    "                        || !root.rmtoolFastMonoReadingEnabled\n"
                    "                        || root.rmtoolFastMonoCleanupInterval <= 0)",
                    source,
                )
                self.assertIn("const interval = root.toolbar.rmtoolDocumentView?.rmtoolFastMonoCleanupInterval ?? 10", source)
                for interval in (5, 10, 20, 30, 0):
                    self.assertIn(
                        f"rmtoolFastMonoCleanupInterval = {interval}", source
                    )
                    self.assertIn(
                        f"rmtoolFastMonoCleanupInterval === {interval}", source
                    )
                self.assertEqual(source.count("stackView.popCurrentItem()"), 5)
                self.assertNotIn("tapPageTurn", source)
                self.assertNotIn("TouchHandler", source)
                self.assertNotIn("EPFramebuffer.", source.replace(
                    "EPFramebuffer.hasCapability(EPFramebuffer.Capability.Color)", ""
                ))

    def test_manifest_rejects_unknown_or_forged_verification_identity(self):
        document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
        document["packages"][0]["offline_verified"] = False
        with self.assertRaisesRegex(RuntimeError, "本地白名单"):
            fast.parse_manifest(json.dumps(document).encode())
        document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
        document["packages"][0]["device_verified"] = True
        with self.assertRaisesRegex(RuntimeError, "本地白名单"):
            fast.parse_manifest(json.dumps(document).encode())
        document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
        document["packages"][0]["xochitl_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "未列入本地白名单"):
            fast.parse_manifest(json.dumps(document).encode())

    def test_manifest_rejects_all_policy_and_content_forgery(self):
        for field, value in (
            ("release_version", "9.9.9.9"),
            ("channel", "beta"),
            ("architecture", "armv7l"),
            ("offline_verified", False),
            ("device_verified", True),
            ("sha256", "0" * 64),
        ):
            document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
            document["packages"][0][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                fast.parse_manifest(json.dumps(document).encode())

        document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
        document["packages"][0]["files"][4]["sha256"] = "0" * 64
        with self.assertRaises(RuntimeError):
            fast.parse_manifest(json.dumps(document).encode())

    def test_manifest_rejects_missing_target_and_ninth_identity(self):
        document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
        document["packages"].pop()
        with self.assertRaises(RuntimeError):
            fast.parse_manifest(json.dumps(document).encode())

        document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
        ninth = dict(document["packages"][0])
        ninth["firmware"] = "99999999999999"
        ninth["asset"] = "rmtool-fast-mono-reading-chiappa-99999999999999.tar.gz"
        document["packages"].append(ninth)
        with self.assertRaises(RuntimeError):
            fast.parse_manifest(json.dumps(document).encode())

    def test_manifest_requires_boolean_verification_fields(self):
        for field in ("offline_verified", "device_verified"):
            document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
            document["packages"][0][field] = 1
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "必须是布尔值"):
                fast.parse_manifest(json.dumps(document).encode())

    def test_manifest_rejects_files_outside_fixed_payload_whitelist(self):
        document = json.loads(Path("fast-mono-reading/manifest.json").read_text())
        document["packages"][0]["files"].append(
            {
                "path": "extensions.d/unreviewed.so",
                "sha256": "0" * 64,
                "size": 1,
                "mode": 420,
            }
        )
        with self.assertRaisesRegex(RuntimeError, "固定白名单"):
            fast.parse_manifest(json.dumps(document).encode())

    def test_selection_requires_every_identity_field(self):
        package = self.package()
        exact = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        self.assertEqual(fast.select_package((package,), exact), package)
        for identity in (
            fast.DeviceIdentity("0" * 14, package.platform, package.architecture, package.xochitl_sha256),
            fast.DeviceIdentity(package.firmware, "ferrari" if package.platform == "chiappa" else "chiappa", package.architecture, package.xochitl_sha256),
            fast.DeviceIdentity(package.firmware, package.platform, "armv7l", package.xochitl_sha256),
            fast.DeviceIdentity(package.firmware, package.platform, package.architecture, "0" * 64),
        ):
            self.assertIsNone(fast.select_package((package,), identity))

    def test_every_offline_or_device_verified_target_is_exactly_selectable(self):
        packages = self.packages()
        for package in packages:
            identity = fast.DeviceIdentity(
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )
            with self.subTest(platform=package.platform, firmware=package.firmware):
                self.assertEqual(fast.select_package(packages, identity), package)

        forged = replace(packages[0], offline_verified=False)
        identity = fast.DeviceIdentity(
            forged.firmware,
            forged.platform,
            forged.architecture,
            forged.xochitl_sha256,
        )
        self.assertIsNone(fast.select_package((forged,), identity))
        self.assertIsNone(
            fast.select_package((replace(packages[0], sha256="0" * 64),), identity)
        )

    def test_status_lists_only_packages_for_connected_platform(self):
        package = self.package()
        other = replace(package, platform="ferrari", asset="other.tar.gz")
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            fast, "_active_with_standalone", return_value=False
        ):
            status = fast.get_status(FakeSSH(), (package, other))
        self.assertEqual(status.available_packages, (package,))

    def test_bundled_manifest_is_offline_fallback(self):
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            tap, "_download_limited", side_effect=OSError("offline")
        ):
            self.assertEqual(fast.load_catalog(state_dir), self.packages())

    def test_download_sources_are_cos_first_and_github_second(self):
        package = self.package()
        self.assertEqual(
            fast.MANIFEST_URLS,
            tuple(f"{base}/manifest.json" for base in fast.REMOTE_BASE_URLS),
        )
        self.assertEqual(package.download_urls[0], f"{fast.COS_URL}/{package.asset}")
        self.assertEqual(
            package.download_urls[1], f"{fast.ASSET_RELEASE_URL}/{package.asset}"
        )
        self.assertEqual(package.download_url, package.download_urls[0])

    def test_manifest_falls_back_from_invalid_cos_to_github(self):
        manifest = Path("fast-mono-reading/manifest.json").read_bytes()
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            tap,
            "_download_limited",
            side_effect=(b"not-json", manifest),
        ) as download:
            self.assertEqual(fast.load_catalog(state_dir), self.packages())
        self.assertEqual(
            [call.args[0] for call in download.call_args_list],
            list(fast.MANIFEST_URLS),
        )

    def test_package_falls_back_from_invalid_cos_to_github(self):
        archive = b"archive"
        package = replace(
            self.package(),
            sha256=hashlib.sha256(archive).hexdigest(),
            size=len(archive),
        )
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            tap,
            "_download_limited",
            side_effect=(b"invalid", archive),
        ) as download:
            destination = fast.download_package(package, state_dir)
            self.assertEqual(destination.read_bytes(), archive)
        self.assertEqual(
            [call.args[0] for call in download.call_args_list],
            list(package.download_urls),
        )

    def test_invalid_remote_data_does_not_replace_valid_manifest_cache(self):
        manifest = Path("fast-mono-reading/manifest.json").read_bytes()
        with tempfile.TemporaryDirectory() as state_dir:
            cache = fast._cache_dir(state_dir) / "manifest.json"
            tap._write_atomic(cache, manifest)
            with patch.object(tap, "_download_limited", return_value=b"not-json"):
                self.assertEqual(fast.load_catalog(state_dir), self.packages())
            self.assertEqual(cache.read_bytes(), manifest)

    def test_invalid_remote_package_does_not_replace_existing_cache(self):
        package = self.package()
        with tempfile.TemporaryDirectory() as state_dir:
            cache = fast._cache_dir(state_dir) / package.firmware / package.asset
            tap._write_atomic(cache, b"existing-invalid-cache")
            with patch.object(tap, "_download_limited", return_value=b"invalid"):
                with self.assertRaisesRegex(RuntimeError, "可用镜像"):
                    fast.download_package(package, state_dir)
            self.assertEqual(cache.read_bytes(), b"existing-invalid-cache")

    def test_valid_package_cache_needs_no_remote_source(self):
        archive = b"archive"
        package = replace(
            self.package(),
            sha256=hashlib.sha256(archive).hexdigest(),
            size=len(archive),
        )
        with tempfile.TemporaryDirectory() as state_dir:
            cache = fast._cache_dir(state_dir) / package.firmware / package.asset
            tap._write_atomic(cache, archive)
            with patch.object(tap, "_download_limited") as download:
                self.assertEqual(fast.download_package(package, state_dir), cache)
            download.assert_not_called()

    def test_clean_device_selects_standalone(self):
        ssh = FakeSSH()
        with patch.object(tap, "_vellum_installed_version", return_value=None):
            self.assertEqual(fast._deployment_mode(ssh, self.package()), "standalone")

    def test_vellum_runtime_without_rmtool_package_requires_manual_removal(self):
        package = self.package()
        ssh = FakeSSH(files={tap.VELLUM_BIN})
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        with patch.object(
            tap, "get_device_identity", return_value=identity
        ), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ), patch.object(
            tap, "_vellum_runtime_present", return_value=True
        ), patch.object(
            fast._xovi_standalone, "has_shared_artifacts", return_value=False
        ):
            status = fast.get_status(ssh, (package,))

        self.assertEqual(status.state, fast.FastMonoReadingState.VELLUM_RUNTIME)
        self.assertFalse(status.recovery_available)
        self.assertIn(tap.VELLUM_UNINSTALL_COMMAND, status.detail)

    def test_module_has_no_vellum_install_path(self):
        source = inspect.getsource(fast)
        self.assertIn("_xovi_standalone.enable_shared", source)
        self.assertNotIn("_enable_vellum", source)
        self.assertNotIn("_build_vellum_apk", source)
        self.assertNotIn("vellum add", source.casefold())

    def test_fast_disabled_vellum_state_missing_runtime_refuses_before_writes(self):
        package = self.package()
        marker = json.loads(
            fast._vellum_marker(
                package,
                enabled=False,
                process_token="12345678-1234-1234-1234-123456789abc:1:1",
            )
        )
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        ssh = FakeSSH(files={fast.REMOTE_BASE, fast.MARKER_PATH, tap.VELLUM_BIN})

        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            tap, "_preflight_device"
        ), patch.object(fast, "_read_marker", return_value=marker), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ), patch.object(
            tap, "_vellum_installed_packages", return_value=set()
        ):
            with self.assertRaisesRegex(RuntimeError, "Vellum 官方说明"):
                fast.enable(ssh, package, "unused.tar.gz")

        self.assertEqual(ssh.transfers, {})
        self.assertFalse(
            any(
                command.startswith(("rm ", "mv ", "cp ", "mkdir ", "rmdir "))
                or "vellum add" in command
                or "vellum del" in command
                for command in ssh.commands
            )
        )

    def test_fast_disabled_vellum_peer_missing_runtime_refuses_before_writes(self):
        package = self.package()
        tap_package = self.tap_package_for(package)
        marker = json.loads(
            tap._vellum_marker(
                tap_package,
                enabled=False,
                process_token="12345678-1234-1234-1234-123456789abc:1:1",
            )
        )
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        ssh = FakeSSH(files={tap.REMOTE_BASE, tap.MARKER_PATH, tap.VELLUM_BIN})

        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            tap, "_preflight_device"
        ), patch.object(tap, "_read_marker", return_value=marker), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ), patch.object(
            tap, "_vellum_installed_packages", return_value=set()
        ):
            with self.assertRaisesRegex(RuntimeError, "Vellum 官方说明"):
                fast.enable(ssh, package, "unused.tar.gz")

        self.assertEqual(ssh.transfers, {})
        self.assertFalse(
            any(
                command.startswith(("rm ", "mv ", "cp ", "mkdir ", "rmdir "))
                or "vellum add" in command
                or "vellum del" in command
                for command in ssh.commands
            )
        )

    def test_disabled_r2_vellum_marker_is_cleared_without_vellum_del(self):
        package = self.package()
        revision, r2 = fast._known_shared_predecessor_specs(package)[1]
        token = "12345678-1234-1234-1234-123456789abc:1:1"
        marker = json.loads(
            fast._vellum_marker(package, enabled=False, process_token=token)
        )
        marker["vellum_version"] = f"{package.release_version}-r{revision}"
        marker["qmd_sha256"] = r2.sha256
        ssh = FakeSSH(
            files={fast.REMOTE_BASE, fast.MARKER_PATH, tap.VELLUM_BIN}
        )
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            fast, "_read_marker", return_value=marker
        ), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ):
            status = fast.disable(ssh, (package,))

        self.assertEqual(status.state, fast.FastMonoReadingState.VELLUM_RUNTIME)
        self.assertNotIn(fast.MARKER_PATH, ssh.files)
        self.assertNotIn(fast.REMOTE_BASE, ssh.files)
        self.assertFalse(any("vellum del" in command for command in ssh.commands))

    def test_disabled_vellum_marker_directory_with_extra_file_is_rejected(self):
        package = self.package()
        revision, r2 = fast._known_shared_predecessor_specs(package)[1]
        token = "12345678-1234-1234-1234-123456789abc:1:1"
        marker = json.loads(
            fast._vellum_marker(package, enabled=False, process_token=token)
        )
        marker["vellum_version"] = f"{package.release_version}-r{revision}"
        marker["qmd_sha256"] = r2.sha256
        extra = f"{fast.REMOTE_BASE}/keep.txt"
        ssh = FakeSSH(
            files={fast.REMOTE_BASE, fast.MARKER_PATH, extra, tap.VELLUM_BIN}
        )
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            fast, "_read_marker", return_value=marker
        ), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "包含未知文件"):
                fast.disable(ssh, (package,))

        self.assertIn(fast.MARKER_PATH, ssh.files)
        self.assertIn(extra, ssh.files)
        self.assertFalse(any("rm -rf" in command for command in ssh.commands))

    def test_disabled_vellum_unknown_hash_is_not_cleared(self):
        package = self.package()
        token = "12345678-1234-1234-1234-123456789abc:1:1"
        marker = json.loads(
            fast._vellum_marker(package, enabled=False, process_token=token)
        )
        marker["vellum_version"] = f"{package.release_version}-r2"
        marker["qmd_sha256"] = "a" * 64
        ssh = FakeSSH(
            files={fast.REMOTE_BASE, fast.MARKER_PATH, tap.VELLUM_BIN}
        )
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            fast, "_read_marker", return_value=marker
        ), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                fast.disable(ssh, (package,))

        self.assertIn(fast.MARKER_PATH, ssh.files)
        self.assertFalse(any(command.startswith("rm -f") for command in ssh.commands))

    def test_enabled_vellum_predecessor_still_uses_vellum_delete(self):
        package = self.package()
        ssh = FakeSSH(files={fast.MARKER_PATH, tap.VELLUM_BIN})
        result = Mock()
        with patch.object(
            tap, "_vellum_installed_version", return_value="3.27.3.0-r2"
        ), patch.object(fast, "_disable_vellum", return_value=result) as disable_vellum:
            self.assertIs(fast.disable(ssh, (package,)), result)
        disable_vellum.assert_called_once_with(ssh, (package,))

    def test_vellum_disable_refuses_untrusted_payload_before_mutation(self):
        package = self.package()
        current_hash = package.file(fast.QMD_PAYLOAD_PATH).sha256
        current_version = f"{package.release_version}-r{package.package_revision}"
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        base_marker = json.loads(
            fast._vellum_marker(
                package,
                enabled=True,
                process_token="12345678-1234-1234-1234-123456789abc:1:1",
            )
        )
        cases = (
            (
                "unknown r99",
                {**base_marker, "vellum_version": f"{package.release_version}-r99"},
                f"{package.release_version}-r99",
                True,
                True,
                current_hash,
            ),
            (
                "missing marker field",
                {key: value for key, value in base_marker.items() if key != "qmd_path"},
                current_version,
                True,
                True,
                current_hash,
            ),
            (
                "extra marker field",
                {**base_marker, "unexpected": True},
                current_version,
                True,
                True,
                current_hash,
            ),
            (
                "disabled marker",
                {**base_marker, "enabled": False},
                current_version,
                True,
                True,
                current_hash,
            ),
            (
                "wrong installed version",
                base_marker,
                f"{package.release_version}-r2",
                True,
                True,
                current_hash,
            ),
            (
                "wrong ownership",
                base_marker,
                current_version,
                False,
                True,
                current_hash,
            ),
            (
                "unexpected package path",
                base_marker,
                current_version,
                True,
                False,
                current_hash,
            ),
            (
                "changed qmd",
                base_marker,
                current_version,
                True,
                True,
                "a" * 64,
            ),
        )
        for name, marker, installed, owns_qmd, paths_valid, actual_hash in cases:
            with self.subTest(name=name):
                ssh = FakeSSH(files={fast.MARKER_PATH, fast.SHARED_QMD, tap.VELLUM_BIN})
                with patch.object(
                    tap, "get_device_identity", return_value=identity
                ), patch.object(
                    fast, "_read_marker", return_value=marker
                ), patch.object(
                    tap, "_vellum_installed_version", return_value=installed
                ), patch.object(
                    tap, "_vellum_package_owns_path", return_value=owns_qmd
                ), patch.object(
                    fast, "_vellum_payload_paths_valid", return_value=paths_valid
                ), patch.object(
                    tap, "_remote_sha256", return_value=actual_hash
                ), patch.object(
                    fast, "_marker_dir_has_only_marker", return_value=True
                ):
                    with self.assertRaises(RuntimeError):
                        fast.disable(ssh, (package,))

                self.assertEqual(ssh.transfers, {})
                self.assertFalse(
                    any(
                        "vellum del" in command
                        or command.startswith(("rm ", "mv ", "cp ", "mkdir ", "rmdir "))
                        for command in ssh.commands
                    )
                )

    def test_vellum_disable_accepts_exact_current_and_r2_payloads(self):
        package = self.package()
        identity = fast.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        r2_revision, r2 = fast._known_shared_predecessor_specs(package)[1]
        revisions = (
            (package.package_revision, package.file(fast.QMD_PAYLOAD_PATH).sha256),
            (r2_revision, r2.sha256),
        )
        for revision, qmd_hash in revisions:
            marker = json.loads(
                fast._vellum_marker(
                    package,
                    enabled=True,
                    process_token="12345678-1234-1234-1234-123456789abc:1:1",
                )
            )
            marker["vellum_version"] = f"{package.release_version}-r{revision}"
            marker["qmd_sha256"] = qmd_hash
            ssh = Mock()
            ssh.file_exists.return_value = False
            ssh.exec_checked.return_value = ""
            result = Mock()
            with self.subTest(revision=revision), patch.object(
                tap, "get_device_identity", return_value=identity
            ), patch.object(
                fast, "_read_marker", return_value=marker
            ), patch.object(
                fast, "_trusted_catalog", return_value=(package,)
            ), patch.object(
                fast, "_vellum_payload_revision", return_value=(revision, "")
            ), patch.object(
                tap,
                "_vellum_installed_version",
                return_value=None,
            ), patch.object(
                tap, "_vellum_package_owns_path", return_value=True
            ), patch.object(
                fast, "_vellum_payload_paths_valid", return_value=True
            ), patch.object(
                tap, "_remote_sha256", return_value=qmd_hash
            ), patch.object(
                fast, "_marker_dir_has_only_marker", return_value=True
            ), patch.object(
                fast, "get_status", return_value=result
            ):
                self.assertIs(fast._disable_vellum(ssh, (package,)), result)

            self.assertTrue(
                any("vellum del" in call.args[0] for call in ssh.exec_checked.call_args_list)
            )

    def test_tap_standalone_is_selected_for_shared_migration(self):
        ssh = FakeSSH(files={tap.DROPIN_PATH}, dropins=tap.DROPIN_PATH)
        self.assertEqual(fast._deployment_mode(ssh, self.package()), "standalone")
        self.assertEqual(ssh.transfers, {})

    def test_disabled_tap_standalone_is_selected_for_shared_migration(self):
        tap_package = self.tap_package_for()
        marker = json.loads(
            tap._marker(
                tap_package,
                hashlib.sha256(tap._launcher(tap_package).encode()).hexdigest(),
                hashlib.sha256(tap._dropin(tap_package).encode()).hexdigest(),
            )
        )
        ssh = FakeSSH(files={tap.REMOTE_BASE, tap.MARKER_PATH})
        with patch.object(tap, "_read_marker", return_value=marker):
            self.assertEqual(fast._deployment_mode(ssh, self.package()), "standalone")
        self.assertEqual(ssh.transfers, {})

    def test_tap_backend_selects_fast_mono_for_shared_migration(self):
        ssh = FakeSSH(files={fast.DROPIN_PATH}, dropins=fast.DROPIN_PATH)
        self.assertEqual(tap._deployment_mode(ssh, self.package()), "standalone")

    def test_unmanaged_xovi_dropin_is_blocked(self):
        ssh = FakeSSH(dropins="/etc/systemd/system/xochitl.service.d/50-custom.conf")
        with self.assertRaisesRegex(RuntimeError, "其他 xochitl/Xovi"):
            fast._deployment_mode(ssh, self.package())

    def test_own_orphan_dropin_is_deferred_to_shared_validator(self):
        ssh = FakeSSH(files={fast.DROPIN_PATH})
        self.assertEqual(fast._deployment_mode(ssh, self.package()), "standalone")
        self.assertEqual(ssh.transfers, {})

    def test_disable_refuses_orphan_resources_without_ownership_marker(self):
        ssh = FakeSSH(files={fast.DROPIN_PATH, fast.REMOTE_BASE})
        with self.assertRaisesRegex(RuntimeError, "所有权标记"):
            fast.disable(ssh)
        self.assertEqual(ssh.transfers, {})

    def test_fast_install_rejects_unowned_or_mislabeled_tap_peer_before_writes(self):
        package = self.package()
        for name, files, marker in (
            ("missing marker", {tap.VELLUM_BIN, tap.REMOTE_BASE}, None),
            (
                "unknown mode",
                {tap.VELLUM_BIN, tap.REMOTE_BASE, tap.MARKER_PATH},
                {"deployment_mode": "vellum-like"},
            ),
        ):
            ssh = FakeSSH(files=files)
            context = (
                patch.object(tap, "_read_marker", return_value=marker)
                if marker is not None
                else patch.object(tap, "_read_marker")
            )
            with self.subTest(name=name), context, self.assertRaises(RuntimeError):
                fast._deployment_mode(ssh, package)
            self.assertEqual(ssh.transfers, {})
            self.assertFalse(
                any(
                    command.startswith(("rm ", "mv ", "cp ", "mkdir ", "rmdir "))
                    or "vellum add" in command
                    or "vellum del" in command
                    for command in ssh.commands
                )
            )

    def test_tap_disabled_vellum_state_missing_runtime_refuses_before_writes(self):
        fast_package = self.package()
        tap_package = self.tap_package_for(fast_package)
        marker = json.loads(
            tap._vellum_marker(
                tap_package,
                enabled=False,
                process_token="12345678-1234-1234-1234-123456789abc:1:1",
            )
        )
        identity = tap.DeviceIdentity(
            tap_package.firmware,
            tap_package.platform,
            tap_package.architecture,
            tap_package.xochitl_sha256,
        )
        ssh = FakeSSH(files={tap.REMOTE_BASE, tap.MARKER_PATH, tap.VELLUM_BIN})

        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            tap, "_preflight_device"
        ), patch.object(tap, "_read_marker", return_value=marker), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "Vellum 官方说明"):
                tap.enable(ssh, tap_package, "unused.tar.gz")

        self.assertEqual(ssh.transfers, {})
        self.assertFalse(
            any(
                command.startswith(("rm ", "mv ", "cp ", "mkdir ", "rmdir "))
                or "vellum add" in command
                or "vellum del" in command
                for command in ssh.commands
            )
        )

    def test_tap_disabled_vellum_peer_missing_runtime_refuses_before_writes(self):
        fast_package = self.package()
        tap_package = self.tap_package_for(fast_package)
        marker = json.loads(
            fast._vellum_marker(
                fast_package,
                enabled=False,
                process_token="12345678-1234-1234-1234-123456789abc:1:1",
            )
        )
        identity = tap.DeviceIdentity(
            tap_package.firmware,
            tap_package.platform,
            tap_package.architecture,
            tap_package.xochitl_sha256,
        )
        ssh = FakeSSH(files={fast.REMOTE_BASE, fast.MARKER_PATH, tap.VELLUM_BIN})

        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            tap, "_preflight_device"
        ), patch.object(fast, "_read_marker", return_value=marker), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "Vellum 官方说明"):
                tap.enable(ssh, tap_package, "unused.tar.gz")

        self.assertEqual(ssh.transfers, {})
        self.assertFalse(
            any(
                command.startswith(("rm ", "mv ", "cp ", "mkdir ", "rmdir "))
                or "vellum add" in command
                or "vellum del" in command
                for command in ssh.commands
            )
        )

    def test_standalone_scripts_are_feature_owned_and_never_restart(self):
        package = self.package()
        scripts = (
            fast._launcher(package),
            fast._dropin(package),
            fast._xovi_standalone.activation_script("/tmp/s", "/tmp/b", "abc", fast._STANDALONE_LAYOUT),
            fast._xovi_standalone.disable_script("abc", fast._STANDALONE_LAYOUT),
        )
        joined = "\n".join(scripts)
        self.assertIn(fast.REMOTE_BASE, joined)
        self.assertIn(fast.DROPIN_PATH, joined)
        self.assertNotIn(tap.REMOTE_BASE, joined)
        self.assertNotIn("restart xochitl", joined)
        self.assertNotIn("reboot", joined)

    def test_shared_activation_rollback_restores_backup_if_stage_move_fails(self):
        script = fast._xovi_standalone.activation_script(
            "/tmp/stage", "/tmp/backup", "abc", fast._STANDALONE_LAYOUT
        )
        moved_guard = 'if [ "$MOVED" -eq 1 ]; then\n            rm -rf "$BASE"\n        fi'
        restore_guard = (
            'if [ "$HAD_BASE" -eq 1 ] && [ -d "$BACKUP" ]; then\n'
            '            mv "$BACKUP" "$BASE"\n        fi'
        )
        self.assertIn(moved_guard, script)
        self.assertIn(restore_guard, script)
        self.assertLess(script.index(moved_guard), script.index(restore_guard))

    def test_enable_rejects_identity_before_preflight_or_write(self):
        package = self.package()
        wrong = fast.DeviceIdentity(
            package.firmware, "ferrari", package.architecture, package.xochitl_sha256
        )
        ssh = FakeSSH()
        with patch.object(tap, "get_device_identity", return_value=wrong), patch.object(
            tap, "_preflight_device"
        ) as preflight:
            with self.assertRaisesRegex(RuntimeError, "不精确匹配"):
                fast.enable(ssh, package, "unused.tar.gz")
        preflight.assert_not_called()
        self.assertEqual(ssh.transfers, {})


if __name__ == "__main__":
    unittest.main()
