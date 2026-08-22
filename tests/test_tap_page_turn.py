import hashlib
import inspect
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import _package_download
import _tap_page_turn as tap


class FakeSSH:
    def __init__(self, *, dropin=False):
        self.dropin = dropin

    def exec_checked(self, command):
        responses = {
            "tr -cd '0-9' < /etc/version": "20260629074044\n",
            "uname -m": "aarch64\n",
            (
                "cat /sys/devices/soc0/machine 2>/dev/null || "
                "tr -d '\\0' < /proc/device-tree/model 2>/dev/null || true"
            ): "reMarkable Ferrari\n",
            "sha256sum /usr/bin/xochitl": f"{'1' * 64}  /usr/bin/xochitl\n",
        }
        if command not in responses:
            raise AssertionError(f"Unexpected command: {command}")
        return responses[command]

    def exec_command(self, _command):
        return "", "", 1

    def file_exists(self, path):
        return self.dropin and path == tap.DROPIN_PATH


class TapPageTurnTests(unittest.TestCase):
    PROCESS_TOKEN = "12345678-1234-1234-1234-123456789abc:778:1000"
    NEXT_PROCESS_TOKEN = "12345678-1234-1234-1234-123456789abc:901:2000"
    FILES = {
        "LICENSE.qmd-tool": b"GPL-3.0",
        "xovi.so": b"xovi",
        "extensions.d/qt-resource-rebuilder.so": b"qrr",
        "exthome/qt-resource-rebuilder/tap-page-turn.qmd": b"qmd",
        "exthome/qt-resource-rebuilder/hashtab": b"hashtab",
        "qmd-tool": b"tool",
    }

    def package(self, archive=b"archive"):
        files = tuple(
            tap.PayloadFile(
                path,
                hashlib.sha256(data).hexdigest(),
                len(data),
                0o755 if path in {"xovi.so", "qmd-tool"} else 0o644,
            )
            for path, data in self.FILES.items()
        )
        return tap.TapPageTurnPackage(
            firmware="20260629074044",
            release_version="3.28.0.162",
            channel="beta",
            platform="ferrari",
            architecture="aarch64",
            xochitl_sha256="1" * 64,
            asset="tap-ferrari.tar.gz",
            sha256=hashlib.sha256(archive).hexdigest(),
            size=len(archive),
            files=files,
        )

    def manifest(self, package):
        return json.dumps(
            {
                "schema_version": 1,
                "packages": [
                    {
                        "firmware": package.firmware,
                        "release_version": package.release_version,
                        "channel": package.channel,
                        "platform": package.platform,
                        "architecture": package.architecture,
                        "xochitl_sha256": package.xochitl_sha256,
                        "asset": package.asset,
                        "sha256": package.sha256,
                        "size": package.size,
                        "files": [
                            {
                                "path": item.path,
                                "sha256": item.sha256,
                                "size": item.size,
                                "mode": item.mode,
                            }
                            for item in package.files
                        ],
                    }
                ],
            }
        ).encode()

    def make_archive(self, path, files=None):
        with tarfile.open(path, "w:gz") as bundle:
            for name, data in (files or self.FILES).items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                bundle.addfile(info, io.BytesIO(data))

    def test_manifest_parses_exact_package(self):
        package = self.package()
        parsed = tap.parse_manifest(self.manifest(package))
        self.assertEqual(parsed, (package,))

    def test_repository_manifest_is_valid(self):
        parsed = tap.parse_manifest(Path("tap-page-turn/manifest.json").read_bytes())
        self.assertEqual(len(parsed), 17)
        self.assertEqual(
            {
                (item.platform, item.firmware, item.release_version)
                for item in parsed
            },
            {
                ("ferrari", "20260506100933", "3.27.1.0"),
                ("chiappa", "20260506100933", "3.27.1.0"),
                ("ferrari", "20260612085811", "3.27.3.0"),
                ("chiappa", "20260612085811", "3.27.3.0"),
                ("tatsu", "20260612085811", "3.27.3.0"),
                ("rm1", "20260612085811", "3.27.3.0"),
                ("rm2", "20260612085811", "3.27.3.0"),
                ("ferrari", "20260629074044", "3.28.0.162"),
                ("chiappa", "20260629074044", "3.28.0.162"),
                ("ferrari", "20260702125656", "3.28.0.163"),
                ("chiappa", "20260702125656", "3.28.0.163"),
                ("ferrari", "20260702125656", "3.28.0.164"),
                ("chiappa", "20260702125656", "3.28.0.164"),
                ("ferrari", "20260806095513", "3.28.0.166"),
                ("chiappa", "20260806095513", "3.28.0.166"),
                ("ferrari", "20260806095513", "3.28.0.169"),
                ("chiappa", "20260806095513", "3.28.0.169"),
            },
        )
        architecture_by_platform = {
            item.platform: item.architecture for item in parsed
        }
        self.assertEqual(architecture_by_platform["rm1"], "armv7l")
        self.assertEqual(architecture_by_platform["rm2"], "armv7l")
        for platform in ("ferrari", "chiappa", "tatsu"):
            self.assertEqual(architecture_by_platform[platform], "aarch64")

    def test_manifest_rejects_traversal_path(self):
        package = self.package()
        document = json.loads(self.manifest(package))
        document["packages"][0]["files"][0]["path"] = "../xovi.so"
        with self.assertRaisesRegex(RuntimeError, "不安全"):
            tap.parse_manifest(json.dumps(document).encode())

    def test_manifest_requires_every_runtime_file(self):
        package = self.package()
        document = json.loads(self.manifest(package))
        document["packages"][0]["files"] = document["packages"][0]["files"][:-1]
        with self.assertRaisesRegex(RuntimeError, "缺少必要文件"):
            tap.parse_manifest(json.dumps(document).encode())

    def test_archive_extracts_only_verified_files(self):
        package = self.package()
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "payload.tar.gz"
            output = Path(temporary) / "output"
            self.make_archive(archive)
            tap.extract_verified_package(archive, package, output)
            for name, data in self.FILES.items():
                self.assertEqual(output.joinpath(*name.split("/")).read_bytes(), data)

    def test_archive_rejects_unlisted_file(self):
        package = self.package()
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "payload.tar.gz"
            files = dict(self.FILES)
            files["extra.so"] = b"extra"
            self.make_archive(archive, files)
            with self.assertRaisesRegex(RuntimeError, "未授权"):
                tap.extract_verified_package(archive, package, Path(temporary) / "out")

    def test_manifest_refresh_is_cached_for_offline_use(self):
        package = self.package()
        manifest = self.manifest(package)
        with tempfile.TemporaryDirectory() as state_dir:
            with patch.object(tap, "_download_limited", return_value=manifest):
                self.assertEqual(tap.load_catalog(state_dir), (package,))
            with patch.object(tap, "_download_limited", side_effect=OSError("offline")):
                self.assertEqual(tap.load_catalog(state_dir), (package,))

    def test_download_sources_are_github_first_and_cos_fallback(self):
        package = self.package()
        self.assertEqual(
            tap.MANIFEST_URLS,
            tuple(f"{base}/manifest.json" for base in tap.REMOTE_BASE_URLS),
        )
        self.assertEqual(
            package.download_urls[0], f"{tap.ASSET_RELEASE_URL}/{package.asset}"
        )
        self.assertEqual(package.download_urls[1], f"{tap.COS_URL}/{package.asset}")
        self.assertEqual(package.download_url, package.download_urls[0])

    def test_manifest_falls_back_from_invalid_cos_to_github(self):
        package = self.package()
        manifest = self.manifest(package)
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            tap,
            "_download_limited",
            side_effect=(b"not-json", manifest),
        ) as download:
            self.assertEqual(tap.load_catalog(state_dir), (package,))
        self.assertEqual(
            [call.args[0] for call in download.call_args_list],
            list(tap.MANIFEST_URLS),
        )

    def test_package_falls_back_from_invalid_cos_to_github(self):
        archive = b"archive"
        package = self.package(archive)
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            tap,
            "_download_limited",
            side_effect=(b"invalid", archive),
        ) as download:
            destination = tap.download_package(package, state_dir)
            self.assertEqual(destination.read_bytes(), archive)
        self.assertEqual(
            [call.args[0] for call in download.call_args_list],
            list(package.download_urls),
        )

    def test_catalog_uses_bundled_manifest_when_both_sources_fail(self):
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            tap, "_download_limited", side_effect=OSError("offline")
        ):
            self.assertEqual(tap.load_catalog(state_dir), tap._trusted_catalog())

    def test_invalid_remote_data_does_not_replace_valid_manifest_cache(self):
        package = self.package()
        manifest = self.manifest(package)
        with tempfile.TemporaryDirectory() as state_dir:
            cache = tap._cache_dir(state_dir) / "manifest.json"
            tap._write_atomic(cache, manifest)
            with patch.object(tap, "_download_limited", return_value=b"not-json"):
                self.assertEqual(tap.load_catalog(state_dir), (package,))
            self.assertEqual(cache.read_bytes(), manifest)

    def test_atomic_cache_write_flushes_to_disk_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            tap.os, "fsync", wraps=tap.os.fsync
        ) as fsync:
            destination = Path(state_dir) / "cache" / "manifest.json"
            tap._write_atomic(destination, b"validated")

            self.assertEqual(destination.read_bytes(), b"validated")
            self.assertEqual(fsync.call_count, 1)
            self.assertEqual(list(destination.parent.glob(".manifest.json.*.tmp")), [])

    def test_invalid_remote_package_does_not_replace_existing_cache(self):
        package = self.package()
        with tempfile.TemporaryDirectory() as state_dir:
            cache = tap._cache_dir(state_dir) / package.firmware / package.asset
            tap._write_atomic(cache, b"existing-invalid-cache")
            with patch.object(tap, "_download_limited", return_value=b"invalid"):
                with self.assertRaises(_package_download.PackageDownloadError) as ctx:
                    tap.download_package(package, state_dir)
                error = ctx.exception
                self.assertEqual(
                    error.urls,
                    (
                        f"{tap.ASSET_RELEASE_URL}/{package.asset}",
                        f"{tap.COS_URL}/{package.asset}",
                    ),
                )
                self.assertIn(package.asset, str(error))
                self.assertIn("github.com", str(error))
                self.assertIn("myqcloud.com", str(error))
            self.assertEqual(cache.read_bytes(), b"existing-invalid-cache")

    def test_download_failure_error_can_store_a_verified_local_file(self):
        package = self.package()
        archive = b"manual-archive"
        package = self.package(archive)
        with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as other:
            source = Path(other) / "manual.tar.gz"
            source.write_bytes(archive)
            with patch.object(tap, "_download_limited", side_effect=OSError("down")):
                with self.assertRaises(_package_download.PackageDownloadError) as ctx:
                    tap.download_package(package, state_dir)
            stored = ctx.exception.store(str(source))
            self.assertEqual(stored.read_bytes(), archive)
            self.assertEqual(
                stored,
                tap._cache_dir(state_dir) / package.firmware / package.asset,
            )
            with patch.object(tap, "_download_limited") as download:
                self.assertEqual(tap.download_package(package, state_dir), stored)
            download.assert_not_called()

    def test_load_local_package_rejects_size_and_hash_mismatches(self):
        archive = b"manual-archive"
        package = self.package(archive)
        with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as other:
            tampered = Path(other) / "tampered.tar.gz"
            tampered.write_bytes(archive + b"x")
            with self.assertRaisesRegex(RuntimeError, "大小与清单不符"):
                tap.load_local_package(package, tampered, state_dir)
            wrong = Path(other) / "wrong.tar.gz"
            wrong.write_bytes(b"x" * len(archive))
            with self.assertRaisesRegex(RuntimeError, "SHA-256 与清单不符"):
                tap.load_local_package(package, wrong, state_dir)

    def test_valid_package_cache_needs_no_remote_source(self):
        archive = b"archive"
        package = self.package(archive)
        with tempfile.TemporaryDirectory() as state_dir:
            cache = tap._cache_dir(state_dir) / package.firmware / package.asset
            tap._write_atomic(cache, archive)
            with patch.object(tap, "_download_limited") as download:
                self.assertEqual(tap.download_package(package, state_dir), cache)
            download.assert_not_called()

    def test_launcher_fails_open_and_gates_every_runtime_file(self):
        package = self.package()
        launcher = tap._launcher(package)
        self.assertIn("exec /usr/bin/xochitl --system", launcher)
        self.assertIn(package.firmware, launcher)
        self.assertIn(package.xochitl_sha256, launcher)
        for path in tap._RUNTIME_PATHS:
            self.assertIn(f"{tap.REMOTE_BASE}/{path}", launcher)

    def test_launcher_recognizes_every_supported_platform(self):
        launcher = tap._launcher(self.package())
        for machine, platform in (
            ("Ferrari", "ferrari"),
            ("Chiappa", "chiappa"),
            ("Tatsu", "tatsu"),
            ('"reMarkable 1"', "rm1"),
            ('"reMarkable 2"', "rm2"),
        ):
            self.assertIn(f"*{machine}*) platform={platform}", launcher)

    def test_standard_vellum_xovi_blocks_rmtool_install(self):
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == tap.VELLUM_BIN
        with patch.object(tap, "_vellum_installed_packages", return_value=set()):
            with self.assertRaisesRegex(RuntimeError, "Vellum 官方说明"):
                tap._deployment_mode(ssh, self.package())

    def test_both_historical_rmtool_vellum_packages_must_be_removed_first(self):
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == tap.VELLUM_BIN
        with patch.object(
            tap,
            "_vellum_installed_packages",
            return_value=set(tap.RMTOOL_VELLUM_PACKAGE_NAMES),
        ), self.assertRaises(RuntimeError) as caught:
            tap._deployment_mode(ssh, self.package())

        message = str(caught.exception)
        for package_name in tap.RMTOOL_VELLUM_PACKAGE_NAMES:
            self.assertIn(package_name, message)
        self.assertNotIn("self uninstall", message)
        self.assertNotIn("--all", message)

    def test_verified_legacy_vellum_package_is_offered_for_removal(self):
        package = self.package()
        marker = json.loads(
            tap._vellum_marker(
                package,
                enabled=True,
                process_token=self.PROCESS_TOKEN,
            )
        )
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path in {
            tap.MARKER_PATH,
            tap.VELLUM_BIN,
            tap.SHARED_QMD,
        }
        with patch.object(
            tap, "get_device_identity", return_value=tap.DeviceIdentity(
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )
        ), patch.object(
            tap, "_trusted_catalog", return_value=(package,)
        ), patch.object(
            tap, "_read_marker", return_value=marker
        ), patch.object(
            tap, "_vellum_installed_version",
            return_value=tap._vellum_package_version(package),
        ), patch.object(
            tap, "_vellum_payload_valid", return_value=(True, "")
        ), patch.object(
            tap._xovi_standalone, "has_shared_artifacts", return_value=False
        ):
            status = tap.get_status(ssh, (package,))

        self.assertEqual(status.state, tap.TapPageTurnState.LEGACY_VELLUM)
        self.assertTrue(status.dropin_present)

    def test_vellum_runtime_without_rmtool_package_requires_manual_removal(self):
        package = self.package()
        ssh = Mock()
        ssh.file_exists.side_effect = lambda path: path == tap.VELLUM_BIN
        with patch.object(
            tap, "get_device_identity", return_value=tap.DeviceIdentity(
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )
        ), patch.object(
            tap, "_vellum_installed_version", return_value=None
        ), patch.object(
            tap, "_vellum_runtime_present", return_value=True
        ), patch.object(
            tap._xovi_standalone, "has_shared_artifacts", return_value=False
        ):
            status = tap.get_status(ssh, (package,))

        self.assertEqual(status.state, tap.TapPageTurnState.VELLUM_RUNTIME)
        self.assertFalse(status.dropin_present)
        self.assertIn(tap.VELLUM_UNINSTALL_COMMAND, status.detail)

    def test_module_has_no_vellum_install_path(self):
        source = inspect.getsource(tap)
        self.assertIn("_xovi_standalone.enable_shared", source)
        self.assertNotIn("_enable_vellum", source)
        self.assertNotIn("_build_vellum_apk", source)
        self.assertNotIn("vellum add", source.casefold())

    def test_unknown_xovi_dropin_is_still_rejected(self):
        ssh = Mock()
        ssh.file_exists.return_value = False
        ssh.exec_checked.return_value = (
            "/etc/systemd/system/xochitl.service.d/custom-xovi.conf"
        )
        with self.assertRaisesRegex(RuntimeError, "拒绝自动合并"):
            tap._deployment_mode(ssh, self.package())

    def test_dropin_has_boot_guards_without_hard_home_dependency(self):
        dropin = tap._dropin(self.package())
        self.assertIn("After=home.mount", dropin)
        self.assertNotIn("Requires=home.mount", dropin)
        self.assertIn(f"ConditionPathExists={tap.LAUNCHER_PATH}", dropin)
        self.assertIn(f"ExecStart={tap.LAUNCHER_PATH}", dropin)
        self.assertNotIn("LD_PRELOAD", dropin)

    def test_activation_script_never_restarts_or_reboots(self):
        script = tap._activation_script("/stage", "/backup", "a" * 32)
        self.assertNotIn("systemctl restart", script)
        self.assertNotIn("systemctl start", script)
        self.assertNotIn("reboot", script)
        self.assertIn("mount --bind /", script)
        self.assertIn("cmp -s", script)
        self.assertIn("unmount_root\nsystemctl daemon-reload", script)

    def test_disable_script_removes_both_copies_before_daemon_reload(self):
        script = tap._disable_script("b" * 32)
        self.assertNotIn("systemctl restart", script)
        self.assertNotIn("reboot", script)
        self.assertLess(script.rfind("rm -f"), script.rfind("systemctl daemon-reload"))
        self.assertIn("$MOUNT_DIR$DROPIN", script)

    def test_vellum_package_ownership_comes_from_package_database(self):
        ssh = Mock()
        ssh.exec_checked.return_value = (
            "xovi-0.3.3-r2 contains:\n"
            "home/root/xovi/xovi.so\n"
            "home/root/.vellum/licenses/xovi/LICENSE\n"
        )
        self.assertTrue(
            tap._vellum_package_owns_path(ssh, "xovi", tap.SHARED_XOVI_LIBRARY)
        )
        self.assertFalse(
            tap._vellum_package_owns_path(ssh, "xovi", tap.SHARED_QRR_LIBRARY)
        )

    def test_vellum_version_parser_ignores_package_name_prefix_collision(self):
        ssh = Mock()
        ssh.exec_checked.side_effect = (
            "xovi\nxovi-extensions\n",
            "xovi-extensions-19.0.0-r1 description\n"
            "xovi-0.3.3-r2 description\n",
        )
        self.assertEqual(tap._vellum_installed_version(ssh, "xovi"), "0.3.3-r2")

    def test_vellum_version_requires_exact_installed_package_name(self):
        ssh = Mock()
        ssh.exec_checked.return_value = "xovi-1\nxovi-extensions\n"
        self.assertIsNone(tap._vellum_installed_version(ssh, "xovi"))
        self.assertEqual(ssh.exec_checked.call_count, 1)

    def test_vellum_payload_ownership_rejects_unexpected_file(self):
        ssh = Mock()
        expected = (
            f"{tap.VELLUM_PACKAGE_NAME}-3.27.3.0-r0 contains:\n"
            f"{tap.SHARED_QMD.lstrip('/')}\n"
            f"{tap.VELLUM_LICENSE_PATH.lstrip('/')}\n"
            f"{tap.VELLUM_SOURCES_PATH.lstrip('/')}\n"
        )
        ssh.exec_checked.return_value = expected
        self.assertTrue(tap._vellum_payload_paths_valid(ssh))
        ssh.exec_checked.return_value = expected + "etc/systemd/system/xochitl.service\n"
        self.assertFalse(tap._vellum_payload_paths_valid(ssh))

    def test_vellum_payload_rejects_unexpected_marker_field(self):
        package = self.package()
        marker = json.loads(
            tap._vellum_marker(
                package,
                enabled=True,
                process_token=self.PROCESS_TOKEN,
            )
        )
        marker["unexpected"] = True

        valid, detail = tap._vellum_payload_valid(Mock(), package, marker)

        self.assertFalse(valid)
        self.assertIn("字段集合", detail)

    def test_vellum_disable_uses_del_and_keeps_runtime_untouched(self):
        package = self.package()
        identity = tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        commands = []
        ssh = Mock()
        ssh.exec_checked.side_effect = lambda command: commands.append(command) or ""
        ssh.file_exists.side_effect = (
            lambda path: path == tap._xovi_standalone.SHARED_LAYOUT.remote_base
        )
        result = object()
        marker = json.loads(
            tap._vellum_marker(
                package,
                enabled=True,
                process_token=self.PROCESS_TOKEN,
            )
        )
        with (
            patch.object(tap, "_read_marker", return_value=marker),
            patch.object(tap, "_trusted_catalog", return_value=(package,)),
            patch.object(tap, "_vellum_payload_valid", return_value=(True, "")),
            patch.object(
                tap,
                "_vellum_installed_version",
                side_effect=(
                    tap._vellum_package_version(package),
                    None,
                ),
            ),
            patch.object(tap, "_vellum_payload_paths_valid", return_value=True),
            patch.object(tap, "_marker_dir_has_only_marker", return_value=True),
            patch.object(tap, "get_device_identity", return_value=identity),
            patch.object(tap, "get_status", return_value=result),
        ):
            self.assertIs(tap._disable_vellum(ssh, (package,)), result)
        joined = "\n".join(commands)
        self.assertIn(f"vellum del {tap.VELLUM_PACKAGE_NAME}", joined)
        self.assertNotIn(tap.SHARED_XOVI_LIBRARY, joined)
        self.assertNotIn(tap.SHARED_QRR_LIBRARY, joined)
        self.assertNotIn(tap._xovi_standalone.SHARED_LAYOUT.remote_base, joined)
        self.assertNotIn("systemctl", joined)
        self.assertNotIn("reboot", joined)
        self.assertIn(f"rm -f {tap.MARKER_PATH}", joined)
        self.assertTrue(
            ssh.file_exists(tap._xovi_standalone.SHARED_LAYOUT.remote_base)
        )

    def test_status_matches_exact_device_identity(self):
        status = tap.get_status(FakeSSH(), (self.package(),))
        self.assertEqual(status.state, tap.TapPageTurnState.NOT_INSTALLED)
        self.assertEqual(status.package, self.package())

    def test_enable_rejects_untrusted_package_before_preflight_in_all_modes(self):
        package = self.package()
        identity = tap.DeviceIdentity(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        )
        with patch.object(
            tap, "get_device_identity", return_value=identity
        ), patch.object(tap, "_preflight_device") as preflight, patch.object(
            tap, "_deployment_mode"
        ) as deployment_mode:
            with self.assertRaisesRegex(RuntimeError, "内置信任清单"):
                tap.enable(Mock(), package, "unused.tar.gz")

        preflight.assert_not_called()
        deployment_mode.assert_not_called()

    def test_legacy_marker_with_installed_vellum_package_is_broken(self):
        package = self.package()
        ssh = FakeSSH()
        legacy_marker = {
            "schema_version": 2,
            "deployment_mode": "shared_xovi",
        }
        ssh.file_exists = lambda path: path in (tap.MARKER_PATH, tap.VELLUM_BIN)
        with (
            patch.object(tap, "_read_marker", return_value=legacy_marker),
            patch.object(
                tap,
                "_vellum_installed_version",
                return_value=tap._vellum_package_version(package),
            ),
        ):
            status = tap.get_status(ssh, (package,))
        self.assertEqual(status.state, tap.TapPageTurnState.BROKEN)
        self.assertTrue(status.dropin_present)

    def test_status_lists_only_packages_for_connected_platform(self):
        matching = self.package()
        other = tap.TapPageTurnPackage(
            **{
                **matching.__dict__,
                "platform": "chiappa",
                "asset": "tap-chiappa.tar.gz",
            }
        )
        status = tap.get_status(FakeSSH(), (other, matching))
        self.assertEqual(status.available_packages, (matching,))

    def test_active_detection_uses_loaded_library_map(self):
        class ActiveSSH:
            def exec_command(self, command):
                self.command = command
                return "", "", 0

        ssh = ActiveSSH()
        self.assertTrue(tap._active_with_rmtool_payload(ssh))
        self.assertIn("/proc/$pid/maps", ssh.command)
        self.assertIn(f"{tap.REMOTE_BASE}/xovi.so", ssh.command)
        self.assertNotIn("/proc/$pid/environ", ssh.command)

    def test_incompatible_status_still_exposes_own_dropin_for_recovery(self):
        other = self.package()
        other = tap.TapPageTurnPackage(
            **{**other.__dict__, "xochitl_sha256": "2" * 64}
        )
        status = tap.get_status(FakeSSH(dropin=True), (other,))
        self.assertEqual(status.state, tap.TapPageTurnState.INCOMPATIBLE)
        self.assertTrue(status.dropin_present)

    def test_incompatible_status_exposes_shared_marker_for_recovery(self):
        other = tap.TapPageTurnPackage(
            **{**self.package().__dict__, "xochitl_sha256": "2" * 64}
        )
        ssh = FakeSSH()
        ssh.file_exists = lambda path: path == tap.MARKER_PATH
        status = tap.get_status(ssh, (other,))
        self.assertEqual(status.state, tap.TapPageTurnState.INCOMPATIBLE)
        self.assertTrue(status.dropin_present)


if __name__ == "__main__":
    unittest.main()
