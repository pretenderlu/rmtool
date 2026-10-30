import hashlib
import io
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _appload
import _tap_page_turn as tap
import _xovi_standalone as shared


class OfficialAssetTests(unittest.TestCase):
    def test_exact_firmware_selects_architecture_assets(self):
        packages = tap._trusted_catalog()
        self.assertEqual(_appload.APPLOAD_VERSION, "v0.6.0")
        self.assertTrue(any(item.channel == "stable" for item in packages))
        self.assertTrue(any(item.channel != "stable" for item in packages))
        for package in packages:
            identity = tap.DeviceIdentity(
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )
            with self.subTest(
                platform=package.platform, release=package.release_version
            ):
                if package.firmware in _appload.APPLOAD_COMPATIBLE_FIRMWARES:
                    self.assertEqual(
                        _appload.app_asset(identity),
                        _appload.APPLOAD_ASSETS[package.architecture],
                    )
                    self.assertEqual(
                        _appload.koreader_asset(identity),
                        _appload.KOREADER_ASSETS[package.architecture],
                    )
                else:
                    self.assertIsNone(_appload.app_asset(identity))
                    self.assertIsNone(_appload.koreader_asset(identity))

    def test_stable_328_firmware_is_installable_and_recognizable(self):
        package = next(
            item
            for item in tap._trusted_catalog()
            if item.release_version == "3.28.0.172" and item.platform == "chiappa"
        )
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
        runtime, features = _appload.trusted_specs(identity)
        self.assertIsNotNone(runtime)
        self.assertEqual(set(features), {"appload", "koreader"})

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


    def test_tar_extraction_preserves_executable_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary, "official.tar.gz")
            payload = b"official-binary"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("appload.so")
                info.size = len(payload)
                info.mode = 0o755
                bundle.addfile(info, io.BytesIO(payload))
            output = Path(temporary, "out")
            with mock.patch.object(_appload.os, "chmod") as chmod:
                self.assertEqual(
                    _appload.extract_official_tar(
                        archive, output, maximum_unpacked=1024
                    ),
                    output,
                )
            self.assertEqual((output / "appload.so").read_bytes(), payload)
            chmod.assert_called_once_with(output / "appload.so", 0o755)

    def test_tar_extraction_rejects_unsafe_entries(self):
        for case in ("traversal", "symlink", "duplicate", "oversize"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary, "bad.tar.gz")
                with tarfile.open(archive, "w:gz") as bundle:
                    if case == "symlink":
                        info = tarfile.TarInfo("link")
                        info.type = tarfile.SYMTYPE
                        info.linkname = "target"
                        bundle.addfile(info)
                    else:
                        name = "../outside" if case == "traversal" else "appload.so"
                        for _index in range(2 if case == "duplicate" else 1):
                            payload = b"bad" if case != "oversize" else b"too-large"
                            info = tarfile.TarInfo(name)
                            info.size = len(payload)
                            info.mode = 0o755
                            bundle.addfile(info, io.BytesIO(payload))
                message = {
                    "traversal": "不安全路径",
                    "symlink": "链接或特殊文件",
                    "duplicate": "重复路径",
                    "oversize": "超过允许大小",
                }[case]
                with self.assertRaisesRegex(RuntimeError, message):
                    _appload.extract_official_tar(
                        archive,
                        Path(temporary, "out"),
                        maximum_unpacked=4 if case == "oversize" else 1024,
                    )


class EnableTests(unittest.TestCase):
    def test_enable_prepares_receipt_peers_for_one_joint_transaction(self):
        package = next(
            item
            for item in tap._trusted_catalog()
            if item.release_version == "3.29.0.153" and item.platform == "chiappa"
        )
        identity = tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        app_feature = mock.Mock(feature_id=_appload.FEATURE_ID)
        qr_feature = mock.Mock(feature_id="qr-upload")
        old_qr = mock.Mock(feature_id="qr-upload", package_id="old-qr")
        inspection = shared.SharedInspection(
            {
                "qr-upload": shared.SharedFeatureState(
                    old_qr, True, "old-process"
                )
            },
            True,
            True,
            receipt_features=frozenset({"qr-upload"}),
        )
        trusted = {
            _appload.FEATURE_ID: app_feature,
            "qr-upload": qr_feature,
        }
        final = mock.Mock()
        with (
            mock.patch.object(_appload.tap, "get_device_identity", return_value=identity),
            mock.patch.object(_appload, "verify_official_asset", return_value=Path("official")),
            mock.patch.object(_appload.tap, "_preflight_device"),
            mock.patch.object(
                _appload.tap,
                "_trusted_shared_context",
                return_value=(mock.Mock(), trusted, ()),
            ),
            mock.patch.object(_appload.shared, "has_shared_artifacts", return_value=True),
            mock.patch.object(_appload.shared, "inspect_shared", return_value=inspection),
            mock.patch.object(_appload, "_prepare_appload_root", return_value=Path("app")),
            mock.patch.object(_appload, "_prepare_peer_root", return_value=Path("qr")) as prepare_peer,
            mock.patch.object(_appload.shared, "enable_shared") as enable_shared,
            mock.patch.object(_appload, "get_status", return_value=final),
        ):
            result = _appload.enable(mock.Mock(), "official", "state")

        self.assertIs(result, final)
        prepare_peer.assert_called_once_with(
            mock.ANY, identity, "qr-upload", qr_feature, "state", mock.ANY
        )
        self.assertEqual(
            enable_shared.call_args.kwargs["additional_roots"],
            {"qr-upload": Path("qr")},
        )

    def test_qr_peer_prepare_rechecks_stopped_before_download(self):
        import _qr_upload as qr

        package = next(
            item
            for item in qr._trusted_catalog()
            if item.release_version == "3.29.0.153" and item.platform == "chiappa"
        )
        identity = tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        feature = qr._shared_specs(package)[1]
        events = []
        with (
            mock.patch.object(qr, "_assert_stopped", side_effect=lambda _ssh: events.append("stop")),
            mock.patch.object(
                qr,
                "download_package",
                side_effect=lambda *_args: events.append("download") or Path("archive"),
            ),
            mock.patch.object(
                qr,
                "extract_verified_package",
                side_effect=lambda *_args: events.append("extract") or Path("root"),
            ),
        ):
            result = _appload._prepare_peer_root(
                mock.Mock(), identity, "qr-upload", feature, "state", "destination"
            )

        self.assertEqual(result, Path("root"))
        self.assertEqual(events, ["stop", "download", "extract"])


class PinyinPeerTests(unittest.TestCase):
    def setUp(self):
        import _pinyin_input as pinyin

        self.pinyin = pinyin
        self.package = next(
            item for item in pinyin._trusted_catalog() if item.platform == "chiappa"
        )
        self.identity = tap.DeviceIdentity(
            self.package.firmware,
            self.package.platform,
            self.package.architecture,
            self.package.xochitl_sha256,
        )
        self.feature = pinyin._shared_specs(self.package)[1]

    def prepare(self, validate):
        ssh = mock.Mock()
        with (
            mock.patch.object(self.pinyin, "_server_running", return_value=False),
            mock.patch.object(self.pinyin, "_has_external_payload", return_value=True),
            mock.patch.object(self.pinyin, "_validate_external_payload", side_effect=validate) as check,
            mock.patch.object(self.pinyin, "download_package", return_value=Path("archive")) as download,
            mock.patch.object(_appload.tap, "extract_verified_package", return_value=Path("root")),
        ):
            try:
                return _appload._prepare_peer_root(
                    ssh, self.identity, "pinyin-input", self.feature, "state", "destination"
                ), check, download, ssh
            except RuntimeError:
                return None, check, download, ssh

    def test_installed_service_is_validated_on_the_device(self):
        result, check, _download, ssh = self.prepare(lambda *_args: False)
        self.assertEqual(result, Path("root"))
        check.assert_called_once_with(ssh, self.package)

    def test_mismatched_service_blocks_before_download(self):
        def reject(*_args):
            raise RuntimeError("拼音输入法服务 校验失败")

        result, _check, download, _ssh = self.prepare(reject)
        self.assertIsNone(result)
        download.assert_not_called()


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

    def test_uninstall_removes_feature_before_shim_links(self):
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
            _appload.AppLoadState.INSTALLED_DISABLED,
            identity,
            _appload.APPLOAD_ASSETS[identity.architecture],
        )
        feature = mock.Mock(feature_id=_appload.FEATURE_ID)
        ssh = mock.Mock()
        ssh.file_exists.return_value = False
        order = []
        with (
            mock.patch.object(_appload, "get_status", side_effect=(status, status)),
            mock.patch.object(
                _appload.tap,
                "_trusted_shared_context",
                return_value=(mock.Mock(), {_appload.FEATURE_ID: feature}, ()),
            ),
            mock.patch.object(
                _appload.shared,
                "remove_shared_features",
                side_effect=lambda *_args: order.append("feature"),
            ),
            mock.patch.object(
                _appload,
                "remove_shim_links",
                side_effect=lambda *_args: order.append("shims"),
            ),
        ):
            _appload.uninstall(ssh)
        self.assertEqual(order, ["feature", "shims"])


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

    def test_v053_receipt_is_offered_as_v060_repair(self):
        package = next(
            item
            for item in tap._trusted_catalog()
            if item.release_version == "3.28.0.172" and item.platform == "chiappa"
        )
        identity = tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        current = mock.Mock(package_id="appload-v0.6.0-aarch64")
        old = mock.Mock(package_id="appload-v0.5.3-aarch64")
        inspection = shared.SharedInspection(
            {
                _appload.FEATURE_ID: shared.SharedFeatureState(
                    old, True, "old-process"
                )
            },
            False,
            True,
            receipt_features=frozenset({_appload.FEATURE_ID}),
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
                return_value=(mock.Mock(), {_appload.FEATURE_ID: current}, ()),
            ),
            mock.patch.object(
                _appload.shared, "inspect_shared", return_value=inspection
            ),
            mock.patch.object(_appload, "_extension_active") as active,
        ):
            status = _appload.get_status(ssh)

        self.assertEqual(status.state, _appload.AppLoadState.REPAIRABLE)
        self.assertIn("旧版 AppLoad", status.detail)
        active.assert_not_called()


if __name__ == "__main__":
    unittest.main()
