import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, Mock, patch

import _note_enhancements as note
import _tap_page_turn as tap
import _xovi_standalone as shared


MANIFEST_PATH = Path("note-enhancements/manifest.json")


class NoteEnhancementsBackendTests(unittest.TestCase):
    def test_all_targets_and_manifest_are_offline_only_before_device_canary(self):
        self.assertTrue(note.ALLOWED_TARGETS)
        self.assertEqual(
            {identity[0] for identity in note.ALLOWED_TARGETS},
            note.SUPPORTED_PLATFORMS,
        )
        self.assertTrue(
            all(offline and not device for _, _, offline, device in note.ALLOWED_TARGETS.values())
        )
        packages = note.parse_manifest(MANIFEST_PATH.read_bytes())
        self.assertTrue(packages)
        self.assertTrue(
            all(package.offline_verified and not package.device_verified for package in packages)
        )

    def setUp(self):
        self.catalog = note.parse_manifest(
            MANIFEST_PATH.read_bytes(), require_local_match=False
        )
        self.package = self.catalog[0]
        self.identity = tap.DeviceIdentity(
            self.package.firmware,
            self.package.platform,
            self.package.architecture,
            self.package.xochitl_sha256,
        )
        self.runtime, self.feature = note._shared_specs(self.package)

    def test_manifest_is_exact_color_device_matrix_with_two_mirrors(self):
        self.assertEqual(len(self.catalog), 14)
        self.assertEqual(
            {
                (item.platform, item.firmware, item.xochitl_sha256)
                for item in self.catalog
            },
            {(key[0], key[1], key[3]) for key in note.ALLOWED_TARGETS},
        )
        self.assertEqual({item.platform for item in self.catalog}, {"ferrari", "chiappa"})
        self.assertTrue(
            all(item.package_revision == note.PACKAGE_REVISION for item in self.catalog)
        )
        self.assertTrue(all(item.offline_verified for item in self.catalog))
        for package in self.catalog:
            self.assertEqual(
                set(package.urls),
                {f"{base}/{package.asset}" for base in note.REMOTE_BASE_URLS},
            )
            self.assertEqual({item.path for item in package.files}, note._PAYLOAD_PATHS)

    def test_unpublished_revisions_are_not_trusted_as_predecessors(self):
        self.assertEqual(
            note._known_shared_predecessor_specs(self.package, self.feature), ()
        )

    def test_manifest_rejects_changed_identity_url_and_extra_file(self):
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        mutations = []
        changed = json.loads(json.dumps(document))
        changed["packages"][0]["platform"] = "rm2"
        mutations.append(changed)
        changed = json.loads(json.dumps(document))
        changed["packages"][0]["urls"][0] = "https://example.invalid/package"
        mutations.append(changed)
        changed = json.loads(json.dumps(document))
        changed["packages"][0]["files"].append(changed["packages"][0]["files"][0])
        mutations.append(changed)
        for mutation in mutations:
            with self.subTest(), self.assertRaises(RuntimeError):
                note.parse_manifest(
                    json.dumps(mutation).encode(), require_local_match=False
                )

    def test_status_without_shared_runtime_is_not_installed(self):
        client = Mock()
        with (
            patch.object(tap, "get_device_identity", return_value=self.identity),
            patch.object(tap, "_vellum_runtime_present", return_value=False),
            patch.object(note, "_known_upstream_qmd", return_value=None),
            patch.object(
                note,
                "_trusted_context",
                return_value=(
                    self.runtime,
                    {note.FEATURE_ID: self.feature},
                    (),
                    self.feature,
                ),
            ),
            patch.object(shared, "has_shared_artifacts", return_value=False),
        ):
            status = note.get_status(client, self.catalog)
        self.assertEqual(status.state, note.NoteEnhancementsState.NOT_INSTALLED)

    def test_install_preserves_verified_peer_specs(self):
        client = Mock()
        peer = Mock()
        installed_trusted = {"reading-enhancements": peer}
        inspection = shared.SharedInspection({}, False, False)
        final = note.NoteEnhancementsStatus(
            note.NoteEnhancementsState.ENABLE_PENDING_REBOOT,
            self.identity,
            self.package,
            self.catalog,
        )
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(tap, "get_device_identity", return_value=self.identity),
                patch.object(tap, "_vellum_runtime_present", return_value=False),
                patch.object(note, "_known_upstream_qmd", return_value=None),
                patch.object(note, "_trusted_catalog", return_value=self.catalog),
                patch.object(tap, "_preflight_device"),
                patch.object(
                    note,
                    "_trusted_context",
                    return_value=(
                        self.runtime,
                        {note.FEATURE_ID: self.feature},
                        (),
                        self.feature,
                    ),
                ),
                patch.object(shared, "has_shared_artifacts", return_value=True),
                patch.object(
                    note,
                    "_inspect_shared",
                    return_value=(inspection, installed_trusted, {}),
                ),
                patch.object(
                    note, "extract_verified_package", return_value=Path(temporary)
                ),
                patch.object(shared, "enable_shared") as enable_shared,
                patch.object(note, "get_status", return_value=final),
            ):
                result = note.install(
                    client, self.package, Path(temporary) / "package.tar.gz"
                )
        self.assertIs(result, final)
        enable_shared.assert_called_once_with(
            client,
            self.runtime,
            self.feature,
            Path(temporary),
            installed_trusted,
            (),
        )

    def test_unknown_upstream_patch_fails_closed(self):
        client = Mock()
        client.file_exists.side_effect = lambda path: path == note._UPSTREAM_QMD_PATHS[0]
        with (
            patch.object(tap, "get_device_identity", return_value=self.identity),
            patch.object(shared, "_remote_sha256", return_value="0" * 64),
        ):
            status = note.get_status(client, self.catalog)
        self.assertEqual(status.state, note.NoteEnhancementsState.BROKEN)
        self.assertIn("未知", status.detail)

    def test_known_upstream_patch_is_reported_without_being_adopted(self):
        client = Mock()
        with (
            patch.object(tap, "get_device_identity", return_value=self.identity),
            patch.object(tap, "_vellum_runtime_present", return_value=False),
            patch.object(note, "_known_upstream_qmd", return_value=note._UPSTREAM_QMD_PATHS[0]),
        ):
            status = note.get_status(client, self.catalog)
        self.assertEqual(status.state, note.NoteEnhancementsState.BROKEN)
        self.assertIn("社区延迟刷新补丁", status.detail)
        self.assertFalse(status.cleanup_available)


if __name__ == "__main__":
    unittest.main()
