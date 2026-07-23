import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

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

    @staticmethod
    def apk_members(data):
        members = []
        remaining = data
        while remaining:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            payload = decompressor.decompress(remaining) + decompressor.flush()
            if not decompressor.unused_data and len(members) > 0:
                remaining = b""
            else:
                remaining = decompressor.unused_data
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as bundle:
                members.append(
                    {
                        item.name: (
                            bundle.extractfile(item).read() if item.isfile() else None
                        )
                        for item in bundle.getmembers()
                    }
                )
        return members

    def test_manifest_parses_exact_package(self):
        package = self.package()
        parsed = tap.parse_manifest(self.manifest(package))
        self.assertEqual(parsed, (package,))

    def test_repository_manifest_is_valid(self):
        parsed = tap.parse_manifest(Path("tap-page-turn/manifest.json").read_bytes())
        self.assertEqual(len(parsed), 11)
        self.assertEqual(
            {(item.platform, item.firmware) for item in parsed},
            {
                ("ferrari", "20260506100933"),
                ("chiappa", "20260506100933"),
                ("ferrari", "20260612085811"),
                ("chiappa", "20260612085811"),
                ("tatsu", "20260612085811"),
                ("rm1", "20260612085811"),
                ("rm2", "20260612085811"),
                ("ferrari", "20260629074044"),
                ("chiappa", "20260629074044"),
                ("ferrari", "20260702125656"),
                ("chiappa", "20260702125656"),
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

    def test_vellum_apk_is_deterministic_and_firmware_gated(self):
        package = self.package()
        first = tap._build_vellum_apk(package, self.FILES[
            "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
        ], b"GPL-3.0")
        second = tap._build_vellum_apk(package, self.FILES[
            "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
        ], b"GPL-3.0")
        self.assertEqual(first, second)
        first_stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
        control_raw = first_stream.decompress(first) + first_stream.flush()
        data_stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data_raw = (
            data_stream.decompress(first_stream.unused_data) + data_stream.flush()
        )
        self.assertFalse(control_raw.endswith(b"\0" * 1024))
        self.assertEqual(len(data_raw) % (20 * 512), 0)
        self.assertTrue(data_raw.endswith(b"\0" * 1024))
        control, data = self.apk_members(first)
        pkginfo = control[".PKGINFO"].decode()
        self.assertIn(f"pkgname = {tap.VELLUM_PACKAGE_NAME}", pkginfo)
        self.assertIn("pkgver = 3.28.0.162-r0", pkginfo)
        self.assertIn("depend = remarkable-os=3.28.0.162-r0", pkginfo)
        self.assertIn("depend = rmpp=1.0.0-r0", pkginfo)
        for conflict in tap.VELLUM_CONFLICTS:
            self.assertIn(f"depend = !{conflict}", pkginfo)
        self.assertEqual(
            data[tap.SHARED_QMD.removeprefix("/")],
            self.FILES["exthome/qt-resource-rebuilder/tap-page-turn.qmd"],
        )
        self.assertEqual(
            data[f"{tap.VELLUM_LICENSE_DIR.removeprefix('/')}/LICENSE"],
            b"GPL-3.0",
        )
        self.assertEqual(
            {name for name, payload in data.items() if payload is not None},
            {
                tap.SHARED_QMD.removeprefix("/"),
                f"{tap.VELLUM_LICENSE_DIR.removeprefix('/')}/LICENSE",
                f"{tap.VELLUM_LICENSE_DIR.removeprefix('/')}/SOURCES",
            },
        )

    def test_manifest_refresh_is_cached_for_offline_use(self):
        package = self.package()
        manifest = self.manifest(package)
        with tempfile.TemporaryDirectory() as state_dir:
            with patch.object(tap, "_download_limited", return_value=manifest):
                self.assertEqual(tap.load_catalog(state_dir), (package,))
            with patch.object(tap, "_download_limited", side_effect=OSError("offline")):
                self.assertEqual(tap.load_catalog(state_dir), (package,))

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

    def test_standard_vellum_xovi_uses_vellum_mode(self):
        package = self.package()
        ssh = Mock()
        ssh.exec_checked.side_effect = (
            tap.SHARED_XOVI_DROPIN,
            f"{tap.SHARED_XOVI_BASE}/extensions.d\n{tap.SHARED_XOVI_BASE}/exthome\n",
        )
        existing = {tap.VELLUM_BIN, tap.SHARED_HASHTAB}
        ssh.file_exists.side_effect = lambda path: path in existing
        dropin = "\n".join(
            (
                "[Service]",
                f'Environment="LD_PRELOAD={tap.SHARED_XOVI_LIBRARY}"',
                'Environment="XOVI_ROOT=/home/root/xovi/services/xochitl.service/"',
            )
        )
        with (
            patch.object(tap, "_remote_text", return_value=dropin),
            patch.object(tap, "_vellum_installed_version", return_value="1.0.0-r0"),
            patch.object(tap, "_assert_vellum_runtime"),
        ):
            self.assertEqual(tap._deployment_mode(ssh, package), "vellum")

    def test_unknown_xovi_dropin_is_still_rejected(self):
        ssh = Mock()
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

    def test_vellum_qmd_check_uses_existing_hashtab(self):
        command = tap._vellum_qmd_check_command("/tmp/stage")
        self.assertIn(tap.SHARED_HASHTAB, command)
        self.assertIn("/tmp/stage/qmd-tool check", command)

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

    def test_vellum_enable_uses_add_and_never_restarts_xochitl(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "payload.tar.gz"
            self.make_archive(archive)
            package = self.package(archive.read_bytes())
            uploaded = {}
            commands = []
            ssh = Mock()

            def transfer(local, remote):
                uploaded[remote] = Path(local).read_bytes()

            def execute(command):
                commands.append(command)
                return ""

            def remote_sha(_client, remote):
                if remote == tap.SHARED_QMD:
                    return package.file(
                        "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
                    ).sha256
                return hashlib.sha256(uploaded[remote]).hexdigest()

            ssh.transfer_file.side_effect = transfer
            ssh.exec_checked.side_effect = execute
            ssh.file_exists.return_value = False
            result = object()
            with (
                patch.object(tap, "_xochitl_process_token", return_value=self.PROCESS_TOKEN),
                patch.object(
                    tap,
                    "_vellum_installed_version",
                    side_effect=(None, tap._vellum_package_version(package)),
                ),
                patch.object(tap, "_vellum_installed_packages", return_value=set()),
                patch.object(tap, "_remote_sha256", side_effect=remote_sha),
                patch.object(tap, "_vellum_package_owns_path", return_value=True),
                patch.object(tap, "_vellum_payload_paths_valid", return_value=True),
                patch.object(tap, "_write_vellum_marker") as write_marker,
                patch.object(tap, "get_status", return_value=result),
            ):
                self.assertIs(tap._enable_vellum(ssh, package, archive), result)

        joined = "\n".join(commands)
        self.assertIn("vellum add --allow-untrusted --simulate", joined)
        self.assertIn("vellum add --allow-untrusted", joined)
        self.assertNotIn("systemctl restart", joined)
        self.assertNotIn("reboot", joined)
        write_marker.assert_called_once()

    def test_vellum_enable_rejects_conflicting_tap_package(self):
        package = self.package()
        ssh = Mock()
        ssh.file_exists.return_value = False
        with (
            patch.object(tap, "_xochitl_process_token", return_value=self.PROCESS_TOKEN),
            patch.object(tap, "_vellum_installed_version", return_value=None),
            patch.object(
                tap,
                "_vellum_installed_packages",
                return_value={tap.VELLUM_CONFLICTS[0]},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "冲突"):
                tap._enable_vellum(ssh, package, "unused.tar.gz")
        self.assertFalse(
            any(" vellum add " in call.args[0] for call in ssh.exec_checked.call_args_list)
        )

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
        ssh.file_exists.return_value = False
        result = object()
        with (
            patch.object(
                tap,
                "_vellum_installed_version",
                side_effect=(tap._vellum_package_version(package), None),
            ),
            patch.object(tap, "_xochitl_process_token", return_value=self.PROCESS_TOKEN),
            patch.object(tap, "_vellum_payload_paths_valid", return_value=True),
            patch.object(tap, "get_device_identity", return_value=identity),
            patch.object(tap, "_write_vellum_marker") as write_marker,
            patch.object(tap, "get_status", return_value=result),
        ):
            self.assertIs(tap._disable_vellum(ssh, (package,)), result)
        joined = "\n".join(commands)
        self.assertIn(f"vellum del {tap.VELLUM_PACKAGE_NAME}", joined)
        self.assertNotIn(tap.SHARED_XOVI_LIBRARY, joined)
        self.assertNotIn(tap.SHARED_QRR_LIBRARY, joined)
        self.assertNotIn("systemctl", joined)
        self.assertNotIn("reboot", joined)
        write_marker.assert_called_once()

    def test_legacy_shared_qmd_is_restored_when_vellum_migration_rolls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "payload.tar.gz"
            self.make_archive(archive)
            package = self.package(archive.read_bytes())
            uploaded = {}
            commands = []
            ssh = Mock()
            ssh.file_exists.side_effect = lambda path: path == tap.MARKER_PATH
            ssh.transfer_file.side_effect = (
                lambda local, remote: uploaded.__setitem__(remote, Path(local).read_bytes())
            )
            ssh.exec_checked.side_effect = lambda command: commands.append(command) or ""

            def remote_sha(_client, remote):
                if remote == tap.SHARED_QMD:
                    return package.file(
                        "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
                    ).sha256
                return hashlib.sha256(uploaded[remote]).hexdigest()

            old_marker = b'{"schema_version": 2, "deployment_mode": "shared_xovi"}\n'
            expected_version = tap._vellum_package_version(package)
            with (
                patch.object(tap, "_legacy_shared_qmd_owned", return_value=True),
                patch.object(tap, "_remote_text", return_value=old_marker.decode()),
                patch.object(tap, "_xochitl_process_token", return_value=self.PROCESS_TOKEN),
                patch.object(
                    tap,
                    "_vellum_installed_version",
                    side_effect=(None, expected_version, expected_version, None),
                ),
                patch.object(tap, "_vellum_installed_packages", return_value=set()),
                patch.object(tap, "_remote_sha256", side_effect=remote_sha),
                patch.object(tap, "_vellum_package_owns_path", return_value=True),
                patch.object(tap, "_vellum_payload_paths_valid", return_value=True),
                patch.object(
                    tap,
                    "_write_vellum_marker",
                    side_effect=(RuntimeError("marker write failed"), None),
                ) as write_marker,
            ):
                with self.assertRaisesRegex(RuntimeError, "marker write failed"):
                    tap._enable_vellum(ssh, package, archive)

        joined = "\n".join(commands)
        self.assertIn(f"vellum del {tap.VELLUM_PACKAGE_NAME}", joined)
        self.assertIn("legacy-shared-qmd.backup", joined)
        self.assertIn(tap.SHARED_QMD, joined)
        self.assertEqual(write_marker.call_count, 2)
        self.assertEqual(write_marker.call_args_list[1].args[1], old_marker)

    def test_device_qmd_check_uses_required_hashtab_prefix(self):
        command = tap._qmd_check_command("/stage")
        self.assertIn("/hashtabs/hashtab-device", command)
        self.assertNotIn("/hashtabs/device", command)

    def test_status_matches_exact_device_identity(self):
        status = tap.get_status(FakeSSH(), (self.package(),))
        self.assertEqual(status.state, tap.TapPageTurnState.NOT_INSTALLED)
        self.assertEqual(status.package, self.package())

    def test_vellum_status_waits_for_new_xochitl_process(self):
        package = self.package()
        ssh = FakeSSH()
        original = json.loads(
            tap._vellum_marker(
                package,
                enabled=True,
                process_token=self.PROCESS_TOKEN,
            )
        )
        ssh.file_exists = lambda path: path == tap.MARKER_PATH
        with (
            patch.object(tap, "_read_marker", return_value=original),
            patch.object(tap, "_vellum_payload_valid", return_value=(True, "")),
            patch.object(tap, "_active_with_shared_xovi", return_value=True),
        ):
            with patch.object(
                tap, "_xochitl_process_token", return_value=self.PROCESS_TOKEN
            ):
                pending = tap.get_status(ssh, (package,))
            with patch.object(
                tap, "_xochitl_process_token", return_value=self.NEXT_PROCESS_TOKEN
            ):
                enabled = tap.get_status(ssh, (package,))
        self.assertEqual(pending.state, tap.TapPageTurnState.ENABLE_PENDING_REBOOT)
        self.assertEqual(enabled.state, tap.TapPageTurnState.ENABLED)

    def test_vellum_disable_waits_for_new_xochitl_process(self):
        package = self.package()
        ssh = FakeSSH()
        marker = json.loads(
            tap._vellum_marker(
                package,
                enabled=False,
                process_token=self.PROCESS_TOKEN,
            )
        )
        ssh.file_exists = lambda path: path == tap.MARKER_PATH
        with (
            patch.object(tap, "_read_marker", return_value=marker),
            patch.object(tap, "_vellum_payload_valid", return_value=(True, "")),
        ):
            with patch.object(
                tap, "_xochitl_process_token", return_value=self.PROCESS_TOKEN
            ):
                pending = tap.get_status(ssh, (package,))
            with patch.object(
                tap, "_xochitl_process_token", return_value=self.NEXT_PROCESS_TOKEN
            ):
                disabled = tap.get_status(ssh, (package,))
        self.assertEqual(pending.state, tap.TapPageTurnState.DISABLE_PENDING_REBOOT)
        self.assertEqual(disabled.state, tap.TapPageTurnState.INSTALLED_DISABLED)

    def test_vellum_status_waits_for_manual_xovi_activation(self):
        package = self.package()
        ssh = FakeSSH()
        marker = json.loads(
            tap._vellum_marker(
                package,
                enabled=True,
                process_token=self.PROCESS_TOKEN,
            )
        )
        ssh.file_exists = lambda path: path == tap.MARKER_PATH
        with (
            patch.object(tap, "_read_marker", return_value=marker),
            patch.object(tap, "_vellum_payload_valid", return_value=(True, "")),
            patch.object(
                tap, "_xochitl_process_token", return_value=self.NEXT_PROCESS_TOKEN
            ),
            patch.object(tap, "_active_with_shared_xovi", return_value=False),
        ):
            status = tap.get_status(ssh, (package,))
        self.assertEqual(status.state, tap.TapPageTurnState.WAITING_FOR_XOVI)
        self.assertIn("手动激活", status.detail)

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
