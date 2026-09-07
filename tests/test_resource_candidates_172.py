"""Offline candidate checks; published support gates remain unchanged."""

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
            "reading-enhancements": "18bcc013a4227b7520908c94e0cf49a5827a1ff5db4c0e6de2135b614bf4710b",
            "note-enhancements": "5dfd6e9c565e38201da517d9c8c578fcf63bcdf301bae19275b7d9eda09b61ea",
        }
        for feature, fingerprint in expected.items():
            entries = json.loads((ROOT / feature / "manifest.json").read_bytes())["packages"]
            old = [e for e in entries if e["release_version"] != builder.RELEASE]
            canonical = json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(builder.digest(canonical), fingerprint, feature)

    def test_exact_application_targets_and_no_fabricated_predecessors(self):
        import _native_chinese as native
        import _reading_enhancements as reading
        for platform, sha in builder.XOCHITL.items():
            identity = builder.tap.DeviceIdentity(builder.FIRMWARE, platform, "aarch64", sha)
            _runtime, peers, _legacy = builder.tap._trusted_shared_context(identity)
            self.assertTrue(set(builder.FEATURES).issubset(peers))
            package = native.select_package(native._trusted_catalog(), identity)
            self.assertTrue(package.offline_verified)
            self.assertFalse(package.device_verified)
            self.assertEqual(native._known_shared_predecessor_specs(package), ())
            french = native._bundled_french_slot_package(identity)
            self.assertEqual(french.firmware, builder.FIRMWARE)
            self.assertEqual(french.xochitl_sha256, sha)
            package = reading.select_package(reading._trusted_catalog(), identity)
            self.assertEqual(reading._known_shared_predecessor_specs(package, peers[reading.FEATURE_ID]), ())
            forged = builder.tap.DeviceIdentity(builder.FIRMWARE, platform, "aarch64", "0" * 64)
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

    @unittest.skipUnless((ROOT / "build/resources-172/validation.json").is_file(), "local .172 assets not built")
    def test_real_candidates(self):
        root = ROOT / "build/resources-172"
        common = {}
        for feature in builder.FEATURES:
            entries = json.loads((root / feature / "manifest.candidate.json").read_text())["packages"]
            self.assertEqual({e["platform"] for e in entries}, set(builder.XOCHITL))
            for entry in entries:
                staging.verify_candidate(root / feature / entry["asset"], entry, feature)
                shared = {f["path"]: f for f in entry["files"] if f["path"] in builder.COMMON}
                self.assertEqual(shared, common.setdefault(entry["platform"], shared))
        report = json.loads((root / "validation.json").read_text())
        self.assertEqual(len(report["checks"]), 8)
        self.assertFalse(report["application_enabled"])

    @unittest.skipUnless((ROOT / ".rmtool/cache/native-chinese/20260827113527").is_dir(), "local .172 cache not staged")
    def test_application_uses_verified_local_archives_without_network(self):
        for feature in builder.FEATURES:
            app = __import__("_" + feature.replace("-", "_"))
            packages = [p for p in app._trusted_catalog() if p.release_version == builder.RELEASE]
            self.assertEqual(len(packages), 2)
            for package in packages:
                with patch.object(builder.tap, "_download_limited", side_effect=AssertionError("network forbidden")):
                    path = app.download_package(package, str(ROOT / ".rmtool"))
                self.assertEqual(path, ROOT / ".rmtool/cache" / feature / builder.FIRMWARE / package.asset)
                builder.verified(path, package.size, package.sha256)


if __name__ == "__main__":
    unittest.main()
