import hashlib
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _appload
import _tap_page_turn as tap
import _xovi_standalone as shared


class OfficialAssetTests(unittest.TestCase):
    def test_exact_stable_firmware_selects_architecture_assets(self):
        packages = tap._trusted_catalog()
        stable = [item for item in packages if item.channel == "stable"]
        beta = [item for item in packages if item.channel != "stable"]
        self.assertTrue(stable)
        self.assertTrue(beta)
        for package in stable:
            identity = tap.DeviceIdentity(
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )
            self.assertEqual(
                _appload.app_asset(identity),
                _appload.APPLOAD_ASSETS[package.architecture],
            )
            self.assertEqual(
                _appload.koreader_asset(identity),
                _appload.KOREADER_ASSETS[package.architecture],
            )
        for package in beta:
            identity = tap.DeviceIdentity(
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )
            self.assertIsNone(_appload.app_asset(identity))
            self.assertIsNone(_appload.koreader_asset(identity))

    def test_official_asset_requires_exact_name_size_and_hash(self):
        payload = b"official-payload"
        asset = _appload.OfficialAsset(
            "Example",
            "v1",
            "aarch64",
            "official.zip",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            "https://example.invalid/official.zip",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, asset.name)
            path.write_bytes(payload)
            self.assertEqual(_appload.verify_official_asset(path, asset), path)
            path.write_bytes(payload + b"x")
            with self.assertRaisesRegex(RuntimeError, "大小"):
                _appload.verify_official_asset(path, asset)

    def test_safe_extraction_rejects_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary, "bad.zip")
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside", b"bad")
            with self.assertRaisesRegex(RuntimeError, "不安全路径"):
                _appload.extract_official_zip(
                    archive,
                    Path(temporary, "out"),
                    maximum_unpacked=1024,
                )

            with zipfile.ZipFile(archive, "w") as bundle:
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(info, b"target")
            with self.assertRaisesRegex(RuntimeError, "链接或特殊文件"):
                _appload.extract_official_zip(
                    archive,
                    Path(temporary, "out2"),
                    maximum_unpacked=1024,
                )


class DisableOrderTests(unittest.TestCase):
    def test_shared_feature_is_disabled_before_shim_links_are_removed(self):
        package = next(
            item for item in tap._trusted_catalog() if item.channel == "stable"
        )
        identity = tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        status = _appload.AppLoadStatus(
            _appload.AppLoadState.ENABLED,
            identity,
            _appload.APPLOAD_ASSETS[identity.architecture],
        )
        ssh = mock.Mock()
        ssh.file_exists.return_value = False
        inspection = mock.Mock(states={})
        order = []
        with (
            mock.patch.object(_appload, "get_status", return_value=status),
            mock.patch.object(
                _appload.tap,
                "_trusted_shared_context",
                return_value=(mock.Mock(), {}, ()),
            ),
            mock.patch.object(
                _appload.shared, "inspect_shared", return_value=inspection
            ),
            mock.patch.object(
                _appload.shared,
                "disable_shared",
                side_effect=lambda *_args: order.append("disable"),
            ),
            mock.patch.object(
                _appload,
                "remove_shim_links",
                side_effect=lambda *_args: order.append("shims"),
            ),
        ):
            _appload.disable(ssh)
        self.assertEqual(order, ["disable", "shims"])


class StatusTests(unittest.TestCase):
    def test_predecessor_launcher_is_offered_as_repairable(self):
        package = next(
            item for item in tap._trusted_catalog() if item.channel == "stable"
        )
        identity = tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        feature = mock.Mock(feature_id=_appload.FEATURE_ID)
        inspection = shared.SharedInspection(
            {
                _appload.FEATURE_ID: shared.SharedFeatureState(
                    feature, True, "old-process"
                )
            },
            False,
            True,
            launcher_update_available=True,
        )
        ssh = mock.Mock()
        with (
            mock.patch.object(_appload.tap, "get_device_identity", return_value=identity),
            mock.patch.object(_appload.shared, "has_shared_artifacts", return_value=True),
            mock.patch.object(
                _appload.shared,
                "read_shared_identity",
                return_value=(
                    identity.firmware,
                    identity.platform,
                    identity.architecture,
                    identity.xochitl_sha256,
                ),
            ),
            mock.patch.object(
                _appload.tap,
                "_trusted_shared_context",
                return_value=(mock.Mock(), {_appload.FEATURE_ID: feature}, ()),
            ),
            mock.patch.object(
                _appload.shared, "inspect_shared", return_value=inspection
            ),
            mock.patch.object(_appload, "_extension_active") as active,
        ):
            status = _appload.get_status(ssh)

        self.assertEqual(status.state, _appload.AppLoadState.REPAIRABLE)
        self.assertIn("可直接修复", status.detail)
        active.assert_not_called()


if __name__ == "__main__":
    unittest.main()
