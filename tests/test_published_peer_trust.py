"""Real shared inspection across published predecessors and local receipts."""

from collections import Counter
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _appload as app
import _native_import_control as control
import _pinyin_input as pinyin
import _qr_upload as qr
import _reading_enhancements as reading
import _note_enhancements as note
import _tap_page_turn as tap
import _xovi_standalone as shared
from tests.test_plugin_recovery import Device, TOKEN


class PublishedPeerTrustTests(unittest.TestCase):
    def test_baseline_fingerprints_are_from_published_manifests_not_test_builds(self):
        root = Path(__file__).resolve().parents[1]
        # Cross-checked against v1.21.3/aa50e522 before recording; no Git history needed in CI.
        self.assertEqual(
            hashlib.sha256((root / "assets/published-plugin-features.json").read_bytes()).hexdigest(),
            "4ea3439e15689a5663dbef621f98b57affcdd876cb1aceabb4d7274ff60ed423",
        )

    def test_current_published_baseline_is_recognized_for_every_changed_target(self):
        for feature_id, identities in tap._published_payloads().items():
            for key in identities:
                identity = tap.DeviceIdentity(*key.split("|"))
                runtime, trusted, _ = tap._trusted_shared_context(identity)
                predecessor = tap._published_payload_predecessor(identity, trusted[feature_id])
                device = Device(package=next(p for p in tap._trusted_catalog() if all(
                    getattr(p, field) == getattr(identity, field)
                    for field in ("firmware", "platform", "architecture", "xochitl_sha256")
                )))
                device.install({feature_id: shared.SharedFeatureState(predecessor, True, TOKEN)})
                for sidecar in predecessor.sidecars:
                    device.add(sidecar.remote_path, mode=0o100000 | sidecar.mode,
                               digest=sidecar.sha256, size=sidecar.size)
                try:
                    _inspection, installed, _reasons = shared.inspect_shared_revisions(
                        device, runtime, trusted, tap._published_feature_revisions(identity, trusted),
                    )
                except RuntimeError as exc:
                    self.fail(f"{feature_id}/{key}: {exc}")
                self.assertEqual(installed[feature_id], predecessor, (feature_id, key))

    def device(self, platform="chiappa", release="3.28.0.172"):
        package = next(p for p in pinyin._trusted_catalog() if p.platform == platform and p.release_version == release)
        return Device(package=package)

    def test_published_appload_is_recognized_without_this_computers_receipts(self):
        shared.configure_managed_receipt_store(None)
        for platform in ("chiappa", "rm2"):
            for schema in (1, 2):
                with self.subTest(platform=platform, schema=schema):
                    device = self.device(platform)
                    previous = app.published_predecessors(device.identity, device.trusted["appload"])[0][1]
                    device.install({"appload": shared.SharedFeatureState(previous, True, TOKEN)}, schema_version=schema)
                    with patch.object(tap, "get_device_identity", return_value=device.identity):
                        status = app.get_status(device)
                        self.assertEqual(status.state, app.AppLoadState.REPAIRABLE, status.detail)
                        for module in (qr, control):
                            if module.select_package(module._trusted_catalog(), device.identity) is None:
                                continue
                            _inspection, installed, reasons = module._inspect(device, device.identity, device.runtime, device.trusted)
                            self.assertEqual(installed["appload"], previous)
                            self.assertEqual(reasons["appload"], "published-appload-v0.5.3")
                    self.assertFalse(any(command.startswith(("rm ", "mv ", "mkdir ")) for command in device.events))

    def test_registered_receipt_does_not_hide_a_published_predecessor(self):
        device = self.device()
        previous = tap._published_feature_revisions(device.identity, device.trusted)["reading-enhancements"][0][1]
        device.install({"reading-enhancements": shared.SharedFeatureState(previous, True, TOKEN)})
        with tempfile.TemporaryDirectory() as temporary:
            shared.configure_managed_receipt_store(temporary)
            try:
                shared.inspect_shared(device, device.runtime, {**device.trusted, "reading-enhancements": previous})
                _inspection, installed, reasons = control._inspect(device, device.identity, device.runtime, device.trusted)
                self.assertEqual(installed["reading-enhancements"], previous)
                self.assertNotEqual(reasons["reading-enhancements"], shared.MANAGED_RECEIPT_REASON)
            finally:
                shared.configure_managed_receipt_store(None)

    def test_one_read_snapshot_does_not_survive_into_the_next_detection(self):
        device = self.device()
        previous = tap._published_feature_revisions(device.identity, device.trusted)["reading-enhancements"][0][1]
        device.install({"reading-enhancements": shared.SharedFeatureState(previous, True, TOKEN)})
        shared.inspect_shared_revisions(device, device.runtime, device.trusted,
            tap._published_feature_revisions(device.identity, device.trusted))
        hashes = Counter(command for command in device.events if command.startswith("sha256sum "))
        self.assertTrue(hashes)
        self.assertTrue(all(count == 1 for count in hashes.values()), hashes)
        device.entries[device.layout.remote_base + "/" + previous.runtime_path].digest = "0" * 64
        with self.assertRaises(RuntimeError):
            control._inspect(device, device.identity, device.runtime, device.trusted)

    def test_inactive_external_files_do_not_require_installing_another_plugin_first(self):
        device = self.device()
        for path in tuple(device.entries):
            if path.startswith(device.layout.remote_base) or path == shared.SHARED_LAYOUT.dropin_path:
                del device.entries[path]
        device.loader_config = "FragmentPath=/usr/lib/systemd/system/xochitl.service\nDropInPaths=\n"
        device.add(tap.SHARED_XOVI_LIBRARY, b"inactive unknown library")
        before = dict(device.entries)
        with patch.object(tap, "get_device_identity", return_value=device.identity):
            self.assertEqual(tap._external_xovi_conflict_detail(device), "")
            device.maps = "1-2 r-xp 0 0:0 1 " + tap.SHARED_XOVI_LIBRARY
            self.assertIn("未知外部插件", tap._external_xovi_conflict_detail(device))
        self.assertEqual(device.entries, before)

    def test_firmware_residue_is_diagnosed_without_attempting_current_payload_checks(self):
        device = self.device()
        device.identity = replace(device.identity, firmware="20261028104944")
        detail = shared.firmware_residue_detail(device, device.identity)
        self.assertIn("固件管理", detail)
        self.assertIn("USB", detail)

    def test_cross_firmware_feature_pages_only_offer_migration_guidance(self):
        for module in (reading, note, control, qr):
            with self.subTest(feature=module.FEATURE_ID):
                device = self.device()
                old = device.trusted[module.FEATURE_ID]
                device.install({module.FEATURE_ID: shared.SharedFeatureState(old, True, TOKEN)})
                del device.entries[shared.SHARED_LAYOUT.dropin_path]
                device.loader_config = "FragmentPath=/usr/lib/systemd/system/xochitl.service\nDropInPaths=\n"
                current = next(p for p in pinyin._trusted_catalog() if p.platform == "chiappa" and p.release_version == "3.29.1.0")
                device.identity = tap.DeviceIdentity(current.firmware, current.platform, current.architecture, current.xochitl_sha256)
                with patch.object(tap, "get_device_identity", return_value=device.identity):
                    status = module.get_status(device, module._trusted_catalog())
                    self.assertEqual(status.state.value, "broken", status.detail)
                    self.assertIn("固件管理", status.detail)
                    self.assertFalse(getattr(status, "recovery_available", False))
                    if module is control:
                        self.assertIsNone(control.loaded_helper(device))
