import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import _weread_app as app


ROOT = Path(__file__).resolve().parents[1]


class WeReadAppTests(unittest.TestCase):
    def setUp(self):
        self.package = app.trusted_package()
        self.device = app.WeReadAppDevice("chiappa", "aarch64", "3.28.0.172")

    def test_manifest_pins_exact_upstream_payload_and_support_boundary(self):
        document = json.loads(app.BUNDLED_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            document["asset"],
            "remarkable-weread-v1.0.0-universal-release.zip",
        )
        self.assertEqual(document["size"], 43336015)
        self.assertEqual(
            document["sha256"],
            "3b2a918d67ab0d8dfbc7b2ce1cb96145a44af1b5520a9b6310ef183bc2a0dfd5",
        )
        self.assertEqual(document["urls"], [app.OFFICIAL_URL])
        self.assertEqual(
            document["inner"],
            {
                "asset": "remarkable-weread-v1.0.0-universal-aarch64.tar.gz",
                "size": 43329981,
                "sha256": "b14398e9564e1e86f99329772f5eedc55c1cb65794cbba3ea89c175104ba8ccf",
            },
        )
        self.assertEqual(
            document["source_commit"],
            "ba506af03a959092ecdea4e075e93ce9d204ac5e",
        )
        self.assertEqual(document["supported"]["platforms"], ["ferrari", "chiappa"])
        self.assertEqual(document["supported"]["architectures"], ["aarch64"])
        self.assertEqual(document["supported"]["firmware_series"], ["3.28"])

    def test_manifest_rejects_changed_hash_or_download_source(self):
        document = json.loads(app.BUNDLED_MANIFEST.read_text(encoding="utf-8"))
        for field, value in (("sha256", "0" * 64), ("urls", ["https://example.com/a"])):
            with self.subTest(field=field):
                changed = dict(document)
                changed[field] = value
                with self.assertRaises(RuntimeError):
                    app.parse_manifest(json.dumps(changed).encode())

    def test_wrong_series_device_or_arch_refuses_before_download(self):
        cases = (
            app.WeReadAppDevice("chiappa", "aarch64", "3.27.3.0"),
            app.WeReadAppDevice("rm2", "aarch64", "3.28.0.172"),
            app.WeReadAppDevice("ferrari", "armv7l", "3.28.0.172"),
        )
        for device in cases:
            with self.subTest(device=device), patch.object(
                app, "inspect_device", return_value=device
            ), patch.object(app, "download_package") as download:
                with self.assertRaisesRegex(RuntimeError, "未下载或修改设备"):
                    app.install_online(Mock(), "state")
                download.assert_not_called()

    def test_install_rechecks_device_after_local_validation_and_cleans_temp(self):
        client = Mock()
        installed = app.WeReadAppStatus(
            app.WeReadAppState.INSTALLED, self.device, self.package
        )
        with patch.object(app, "trusted_package", return_value=self.package), patch.object(
            app, "inspect_device", return_value=self.device
        ) as inspect, patch.object(app, "_validate_archive"), patch.object(
            app, "get_status", return_value=installed
        ):
            result = app.install(client, self.package, Path("official.tar.gz"))
        self.assertIs(result, installed)
        inspect.assert_called_once_with(client)
        client.transfer_file.assert_called_once()
        install_command = client.exec_checked.call_args_list[0].args[0]
        self.assertIn("unsupported firmware series", install_command)
        self.assertIn("unsupported architecture", install_command)
        self.assertIn("unsupported device", install_command)
        self.assertIn("archive checksum mismatch", install_command)
        self.assertIn('remarkable-weread/install.sh', install_command)
        self.assertIn("trap cleanup EXIT HUP INT TERM", install_command)
        self.assertNotIn("systemctl restart", install_command)
        self.assertNotIn("systemctl reboot", install_command)
        self.assertNotIn(" reboot", install_command)
        cleanup_command = client.exec_checked.call_args_list[1].args[0]
        self.assertIn("rm -f", cleanup_command)

    def test_install_refuses_untrusted_package_before_transfer(self):
        client = Mock()
        forged = replace(self.package, sha256="0" * 64)
        with patch.object(app, "trusted_package", return_value=self.package), self.assertRaisesRegex(
            RuntimeError, "不在本地信任清单"
        ):
            app.install(client, forged, Path("unused"))
        client.transfer_file.assert_not_called()

    def test_status_offers_install_or_repair_and_accepts_exact_files(self):
        with patch.object(app, "inspect_device", return_value=self.device):
            for hashes, expected in (
                ({path: "missing" for path in app.OFFICIAL_FILES}, app.WeReadAppState.NOT_INSTALLED),
                ({**app.OFFICIAL_FILES, next(iter(app.OFFICIAL_FILES)): "0" * 64}, app.WeReadAppState.REPAIR_AVAILABLE),
                (app.OFFICIAL_FILES, app.WeReadAppState.INSTALLED),
            ):
                with self.subTest(expected=expected), patch.object(
                    app, "_installed_file_hashes", return_value=hashes
                ):
                    self.assertIs(app.get_status(Mock()).state, expected)

    def test_status_rejects_incomplete_hash_report(self):
        with patch.object(app, "inspect_device", return_value=self.device), patch.object(
            app, "_installed_file_hashes", return_value={}
        ):
            status = app.get_status(Mock())
        self.assertIs(status.state, app.WeReadAppState.BROKEN)
        self.assertIn("无法完整读取", status.detail)

    def test_archive_rejects_unsafe_member_even_with_matching_outer_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                info = tarfile.TarInfo("../escape")
                payload = b"bad"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            forged = replace(
                self.package,
                inner_size=path.stat().st_size,
                inner_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            with self.assertRaisesRegex(RuntimeError, "不安全路径"):
                app._validate_archive(path, forged)

    def test_official_zip_extracts_only_the_exact_inner_archive(self):
        payload = b"fixed upstream archive"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_zip = root / "release.zip"
            with zipfile.ZipFile(source_zip, "w") as release:
                release.writestr(self.package.inner_asset, payload)
                release.writestr("README.md", "upstream")
            package = replace(
                self.package,
                size=source_zip.stat().st_size,
                sha256=hashlib.sha256(source_zip.read_bytes()).hexdigest(),
                inner_size=len(payload),
                inner_sha256=hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(app._extract_verified_inner(source_zip, package), payload)

    def test_official_zip_rejects_unsafe_extra_member(self):
        payload = b"fixed upstream archive"
        with tempfile.TemporaryDirectory() as temporary:
            source_zip = Path(temporary) / "release.zip"
            with zipfile.ZipFile(source_zip, "w") as release:
                release.writestr(self.package.inner_asset, payload)
                release.writestr("../escape", "bad")
            package = replace(
                self.package,
                size=source_zip.stat().st_size,
                sha256=hashlib.sha256(source_zip.read_bytes()).hexdigest(),
                inner_size=len(payload),
                inner_sha256=hashlib.sha256(payload).hexdigest(),
            )
            with self.assertRaisesRegex(RuntimeError, "不安全路径"):
                app._extract_verified_inner(source_zip, package)

    def test_local_import_rejects_wrong_size_before_reading_file(self):
        source = Mock()
        source.stat.return_value.st_size = self.package.size + 1
        with self.assertRaisesRegex(RuntimeError, "固定清单校验不匹配"):
            app._read_verified_outer(source, self.package)
        source.read_bytes.assert_not_called()

    def test_local_import_atomically_caches_verified_outer_and_inner(self):
        outer = b"official zip"
        inner = b"official inner archive"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / self.package.asset
            source.write_bytes(outer)
            with patch.object(app, "_read_verified_outer", return_value=outer), patch.object(
                app, "_extract_verified_inner_bytes", return_value=inner
            ), patch.object(
                app, "_validate_archive"
            ) as validate, patch.object(app.tap, "_write_atomic") as write:
                destination = app.store_local_package(
                    str(source), str(Path(temporary) / "state")
                )
        cache = Path(temporary) / "state" / "weread-app"
        self.assertEqual(destination, cache / self.package.inner_asset)
        self.assertEqual(
            write.call_args_list,
            [
                unittest.mock.call(cache / self.package.asset, outer),
                unittest.mock.call(cache / self.package.inner_asset, inner),
            ],
        )
        validate.assert_called_once_with(cache / self.package.inner_asset, self.package)

    def test_ui_exposes_separate_app_and_launcher_actions_without_revision_labels(self):
        source = (ROOT / "_tab_toolbox.py").read_text(encoding="utf-8")
        section = source[source.index("class WeReadLauncherSection"):source.index("_LEGACY_PLATFORM_LABELS")]
        self.assertIn('QLabel("微信读书 App")', section)
        self.assertIn('QLabel("设备启动入口")', section)
        self.assertIn('QPushButton("安装微信读书 App")', section)
        self.assertIn('self.detect_button.setText("检测启动入口")', section)
        self.assertIn("仅支持运行 3.28 系列固件的 Paper Pro 和 Move", section)
        self.assertIn("腾讯微信读书官方 CDN", section)
        self.assertIn(
            "state is _weread_app.WeReadAppState.INSTALLED",
            section,
        )
        self.assertNotIn("revision 2", section.casefold())
        self.assertNotIn("revision 3", section.casefold())
        self.assertNotIn("revision 4", section.casefold())

    def test_app_assets_have_no_rmtool_mirror_or_publisher(self):
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "_weread_app.py",
                ROOT / "weread-app" / "manifest.json",
                ROOT / "weread-app" / "README.md",
                ROOT / "tools" / "publish_resources.py",
                ROOT / ".github" / "workflows" / "sync-feature-assets.yml",
            )
        )
        self.assertNotIn("weread-app-assets", sources)
        self.assertNotIn("myqcloud.com/weread-app", sources)
        self.assertNotIn("releases/download/weread-app", sources)

    def test_manual_download_fallback_accepts_the_official_outer_zip(self):
        source = (ROOT / "_tab_toolbox.py").read_text(encoding="utf-8")
        dialog = source[
            source.index("def _show_package_download_error"):
            source.index("def _collect_diagnostics")
        ]
        self.assertIn('exc.asset.casefold().endswith(".zip")', dialog)
        self.assertIn('"资源包 (*.zip);;所有文件 (*)"', dialog)
        self.assertIn('"资源包 (*.tar.gz);;所有文件 (*)"', dialog)


if __name__ == "__main__":
    unittest.main()
