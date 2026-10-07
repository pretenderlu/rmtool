import unittest
import importlib
from dataclasses import replace
from unittest.mock import Mock, patch

import tools.validate_build_matrix as matrix
import _rmkit_cn as fonts
import _tap_page_turn as tap


class BuildMatrixTests(unittest.TestCase):
    def test_current_device_and_firmware_matrix_is_complete(self):
        matrix.validate()

    def test_missing_device_is_rejected_even_when_backend_accepts_manifest(self):
        document = matrix._manifest("tap-page-turn")
        document["packages"] = [p for p in document["packages"] if p["platform"] != "rm1"]
        with patch.object(matrix, "_manifest", return_value=document), patch.object(tap, "parse_manifest"):
            with self.assertRaisesRegex(RuntimeError, "coverage mismatch"):
                matrix._validate_feature_matrix("tap-page-turn")

    def test_same_release_with_different_runtime_identity_is_rejected(self):
        import _note_enhancements as note

        catalog = note._trusted_catalog()
        for field, value in (("architecture", "armv7l"), ("xochitl_sha256", "0" * 64)):
            with self.subTest(field=field), patch.object(
                note, "parse_manifest", return_value=(replace(catalog[0], **{field: value}), *catalog[1:])
            ):
                with self.assertRaisesRegex(RuntimeError, "identity disagrees"):
                    matrix._validate_cross_feature_support()

    def test_plugin_selectors_cover_every_known_target_and_reject_unknown_hashes(self):
        for feature, name in matrix.FEATURE_MODULES.items():
            module = importlib.import_module(name)
            catalog = module._trusted_catalog()
            for package in tap._trusted_catalog():
                identity = tap.DeviceIdentity(
                    package.firmware, package.platform, package.architecture, package.xochitl_sha256
                )
                with self.subTest(feature=feature, platform=package.platform, release=package.release_version):
                    self.assertEqual(
                        module.select_package(catalog, identity) is not None,
                        (package.platform, package.release_version) in matrix.EXPECTED_COVERAGE[feature],
                    )
                    self.assertIsNone(module.select_package(catalog, replace(identity, xochitl_sha256="0" * 64)))
                    self.assertIsNone(module.select_package(catalog, replace(identity, architecture="unknown")))

    def test_epub_menu_is_not_implied_by_basic_font_management(self):
        ssh = Mock()
        for package in tap._trusted_catalog():
            identity = tap.DeviceIdentity(
                package.firmware, package.platform, package.architecture, package.xochitl_sha256
            )
            with self.subTest(platform=package.platform, release=package.release_version), patch.object(
                tap, "get_device_identity", return_value=identity
            ), patch.object(fonts, "_epub_font_slots", return_value=()) as read_slots:
                status = fonts.get_epub_font_slot_status(ssh)
                expected = package.platform in matrix.COLOR_PLATFORMS and package.release_version.startswith("3.28.")
                self.assertEqual(status.supported, expected)
                self.assertEqual(read_slots.called, expected)


if __name__ == "__main__":
    unittest.main()
