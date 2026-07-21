import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    FILES = {
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
        self.assertEqual(len(parsed), 9)
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

    def test_device_qmd_check_uses_required_hashtab_prefix(self):
        command = tap._qmd_check_command("/stage")
        self.assertIn("/hashtabs/hashtab-device", command)
        self.assertNotIn("/hashtabs/device", command)

    def test_status_matches_exact_device_identity(self):
        status = tap.get_status(FakeSSH(), (self.package(),))
        self.assertEqual(status.state, tap.TapPageTurnState.NOT_INSTALLED)
        self.assertEqual(status.package, self.package())

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


if __name__ == "__main__":
    unittest.main()
