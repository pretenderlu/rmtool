"""Offline candidate checks for the five-device 3.28.0.172 matrix."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native-chinese"))
import build_172 as builder
import stage_172 as staging


class Candidate172Tests(unittest.TestCase):
    def test_published_records_unchanged(self):
        # Canonical fingerprints of HEAD's published rows before .172 adaptation.
        expected = {
            "tap-page-turn": "c1e4118012d7660c189e6026760ebabfd5681ccf3d56a2832dc19ae23f7783b5",
            "fast-mono-reading": "f00f8f71423bf365e3aeb41e30e0d621f20cc00f2d07b800ac4c6e1ad32940cc",
            "native-chinese": "4c3f5c9eb081ac44538bbabfd923a626e978c6513ad9c5956fdbe4e183e61429",
            "pinyin-input": "d47c77d2ec9dedcaa0884226d3b6978591febd7f52958fd466c23c39b02d264f",
            "reading-enhancements": "1a141c85f6c93e14c4cd64e0c98bcffbf3dd4d29556272c15a7e5b16b96b7da3",
            "note-enhancements": "5dfd6e9c565e38201da517d9c8c578fcf63bcdf301bae19275b7d9eda09b61ea",
        }
        for feature, fingerprint in expected.items():
            entries = json.loads((ROOT / feature / "manifest.json").read_bytes())["packages"]
            old = [e for e in entries if e["release_version"] != builder.RELEASE]
            canonical = json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(builder.digest(canonical), fingerprint, feature)

    def test_exact_application_targets_and_published_predecessors(self):
        import _native_chinese as native
        import _reading_enhancements as reading
        import _pinyin_input as pinyin
        for platform, (architecture, sha) in builder.TARGETS.items():
            identity = builder.tap.DeviceIdentity(builder.FIRMWARE, platform, architecture, sha)
            _runtime, peers, _legacy = builder.tap._trusted_shared_context(identity)
            self.assertTrue(set(builder.FEATURES_BY_PLATFORM[platform]).issubset(peers))
            if platform not in builder.COLOR_PLATFORMS:
                self.assertTrue({"reading-enhancements", "note-enhancements", "fast-mono-reading"}.isdisjoint(peers))
            package = native.select_package(native._trusted_catalog(), identity)
            self.assertTrue(package.offline_verified)
            self.assertFalse(package.device_verified)
            self.assertEqual(native._known_shared_predecessor_specs(package), ())
            self.assertIsNotNone(pinyin.select_package(pinyin._trusted_catalog(), identity))
            reading_package = reading.select_package(reading._trusted_catalog(), identity)
            if platform in builder.COLOR_PLATFORMS:
                self.assertIsNotNone(reading_package)
                predecessors = reading._known_shared_predecessor_specs(
                    reading_package, peers[reading.FEATURE_ID]
                )
                self.assertEqual(
                    tuple(reason for reason, _feature in predecessors),
                    (
                        "package-revision-10",
                        "package-revision-9",
                        "package-revision-8",
                    ),
                )
            else:
                self.assertIsNone(reading_package)
            forged = builder.tap.DeviceIdentity(builder.FIRMWARE, platform, architecture, "0" * 64)
            self.assertIsNone(native.select_package(native._trusted_catalog(), forged))

    def test_tap_status_survives_old_remote_catalog(self):
        tap = builder.tap
        package = next(p for p in tap._trusted_catalog() if p.release_version == builder.RELEASE)
        identity = tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
        old = tuple(p for p in tap._trusted_catalog() if p.release_version != builder.RELEASE)
        ssh = Mock()
        ssh.file_exists.return_value = False
        ssh.exec_command.return_value = ("", "", 1)
        with patch.object(tap, "get_device_identity", return_value=identity), patch.object(
            tap._xovi_standalone, "has_shared_artifacts", return_value=False
        ):
            status = tap.get_status(ssh, old)
        self.assertEqual(status.package, package)

    def test_deterministic_archive(self):
        files = {"test.txt": (b"exact bytes", 0o644)}
        self.assertEqual(builder.pack(files), builder.pack(files))

    def test_append_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            staging.append_only(path, b"published")
            staging.append_only(path, b"published")
            with self.assertRaises(RuntimeError):
                staging.append_only(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"published")

    def test_wrong_input_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"modified")
            with self.assertRaises(RuntimeError):
                builder.verified(path, 8, "0" * 64)

    def test_incomplete_report_refuses_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "validation.json").write_text('{"package_count": 1}')
            with self.assertRaises(RuntimeError):
                staging.stage(path, path / "cache")
            self.assertFalse((path / "cache").exists())

    @unittest.skipUnless((ROOT / "build/resources-172-five-device/validation.json").is_file(), "local .172 assets not built")
    def test_real_candidates(self):
        root = ROOT / "build/resources-172-five-device"
        common = {}
        for feature in builder.FEATURES:
            entries = json.loads((root / feature / "manifest.candidate.json").read_text())["packages"]
            expected = {
                platform for platform, features in builder.FEATURES_BY_PLATFORM.items()
                if feature in features
            }
            self.assertEqual({e["platform"] for e in entries}, expected)
            for entry in entries:
                staging.verify_candidate(root / feature / entry["asset"], entry, feature)
                shared = {f["path"]: f for f in entry["files"] if f["path"] in builder.COMMON}
                self.assertEqual(shared, common.setdefault(entry["platform"], shared))
        report = json.loads((root / "validation.json").read_text())
        self.assertEqual(report["package_count"], 21)
        self.assertEqual(len(report["checks"]), 6)
        self.assertFalse(report["application_enabled"])

    def test_committed_legacy_binary_inputs_are_exact(self):
        translator = ROOT / "native-chinese/native-chinese-translator-armv7.so"
        catalog = ROOT / "translations/reMarkable_zh_CN-3.28.0.172-rm1-rm2.qm"
        builder.verified(translator, *builder.ARM_TRANSLATOR)
        builder.verified(
            catalog, 205621,
            "0f1de519ab4ac1998f432dab014d40fb0cdae2fe528ab30ca47c7a507df82485",
        )

    @unittest.skipUnless((ROOT / ".rmtool/cache/native-chinese/20260827113527").is_dir(), "local .172 cache not staged")
    def test_application_uses_verified_local_archives_without_network(self):
        for feature in builder.FEATURES:
            app = __import__("_" + feature.replace("-", "_"))
            packages = [p for p in app._trusted_catalog() if p.release_version == builder.RELEASE]
            expected = sum(feature in names for names in builder.FEATURES_BY_PLATFORM.values())
            self.assertEqual(len(packages), expected)
            for package in packages:
                with patch.object(builder.tap, "_download_limited", side_effect=AssertionError("network forbidden")):
                    path = app.download_package(package, str(ROOT / ".rmtool"))
                self.assertEqual(path, ROOT / ".rmtool/cache" / feature / builder.FIRMWARE / package.asset)
                builder.verified(path, package.size, package.sha256)


if __name__ == "__main__":
    unittest.main()
