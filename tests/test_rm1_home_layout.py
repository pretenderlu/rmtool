"""RM1 builds that keep the shared runtime in /home (3.28.0.172 and later)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _appload
import _plugin_recovery as recovery
import _residue_migration as migration
import _tap_page_turn as tap
import _xovi_standalone as shared

SH = shutil.which("sh")
RM1_HOME_FIRMWARE = sorted(shared.HOME_LAYOUT_RM1_FIRMWARE)


def identity(firmware, platform="rm1"):
    architecture = "aarch64" if platform in ("ferrari", "chiappa", "tatsu") else "armv7l"
    return tap.DeviceIdentity(firmware, platform, architecture, "0" * 64)


class LayoutTests(unittest.TestCase):
    def test_preferred_layout_accepts_device_identities(self):
        for firmware in RM1_HOME_FIRMWARE:
            self.assertIs(shared.preferred_layout(identity(firmware)), shared.LEGACY_SHARED_LAYOUT)
        self.assertIs(shared.preferred_layout(identity("20260612085811")), shared.SHARED_LAYOUT)
        self.assertIs(shared.preferred_layout(identity("20261028104944", "rm2")), shared.SHARED_LAYOUT)

    def test_appload_extension_probe_uses_device_layout(self):
        for device, base in (
            (identity("20261028104944"), shared.LEGACY_SHARED_LAYOUT.remote_base),
            (identity("20261028104944", "rm2"), shared.SHARED_LAYOUT.remote_base),
        ):
            ssh = mock.Mock()
            ssh.exec_command.return_value = ("", "", 0)
            self.assertTrue(_appload._extension_active(ssh, device))
            self.assertIn(base + "/extensions.d/appload.so", ssh.exec_command.call_args.args[0])

    def test_incomplete_cleanup_accepts_home_layout_on_every_rm1_home_build(self):
        for firmware in RM1_HOME_FIRMWARE:
            with self.subTest(firmware=firmware):
                ssh = mock.Mock()
                legacy = shared.LEGACY_SHARED_LAYOUT.remote_base
                exists = {legacy}
                with mock.patch.object(shared, "_remote_entry_exists", side_effect=lambda _s, p: p in exists), \
                        mock.patch.object(tap, "get_device_identity", return_value=identity(firmware)), \
                        mock.patch.object(recovery, "_trusted_cleanup_contexts", return_value=()):
                    with self.assertRaisesRegex(RuntimeError, "受信清单"):
                        recovery._inspect_incomplete(ssh)

    def test_incomplete_cleanup_still_rejects_home_layout_elsewhere(self):
        legacy = shared.LEGACY_SHARED_LAYOUT.remote_base
        for device in (identity("20260612085811"), identity("20261028104944", "rm2")):
            with self.subTest(device=device):
                with mock.patch.object(shared, "_remote_entry_exists", side_effect=lambda _s, p: p == legacy), \
                        mock.patch.object(tap, "get_device_identity", return_value=device):
                    with self.assertRaisesRegex(RuntimeError, "旧 /home 共享布局"):
                        recovery._inspect_incomplete(mock.Mock())

    def test_koreader_residue_icon_is_read_from_the_residue_layout(self):
        icon = SimpleNamespace(runtime_path="extensions.d/koreader/icon.png", size=4, sha256="x")
        spec = SimpleNamespace(files=(icon,))
        for present, base in (
            ({shared.LEGACY_SHARED_LAYOUT.remote_base}, shared.LEGACY_SHARED_LAYOUT.remote_base),
            (set(), shared.SHARED_LAYOUT.remote_base),
        ):
            with self.subTest(base=base), tempfile.TemporaryDirectory() as temporary:
                ssh = mock.Mock()
                ssh.file_exists.side_effect = lambda path: path in present
                ssh.download_file.side_effect = lambda _remote, local: Path(local).write_bytes(b"icon")
                destination = Path(temporary) / "koreader"
                destination.mkdir()
                with self.assertRaisesRegex(RuntimeError, "图标校验失败"):
                    migration._prepare_koreader_root(
                        ssh, identity("20261028104944"),
                        {_appload.KOREADER_FEATURE_ID: spec}, "state", destination,
                    )
                self.assertEqual(ssh.download_file.call_args.args[0], f"{base}/{icon.runtime_path}")


@unittest.skipUnless(SH and os.name != "nt", "needs a POSIX shell with symlinks")
class ShimLinkShellTests(unittest.TestCase):
    """Run the generated shim scripts with SHIM_LINK_DIR mapped to a sandbox."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rmtool-shims-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.links = self.root / "shims"
        self.device = identity("20261028104944")
        self.names = sorted(
            Path(item.path).name
            for item in _appload.APPLOAD_FILES[self.device.architecture]
            if item.path.startswith("shims/")
        )
        self.assertTrue(self.names)

    def run_script(self, script):
        script = script.replace(_appload.SHIM_LINK_DIR, str(self.links))
        result = subprocess.run([SH, "-c", script], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr)

    def apply(self, device=None):
        ssh = mock.Mock()
        ssh.exec_checked.side_effect = self.run_script
        _appload.ensure_shim_links(ssh, device or self.device)

    def link(self, name):
        return os.readlink(self.links / name)

    def test_new_links_point_at_the_home_runtime_on_rm1(self):
        self.apply()
        for name in self.names:
            self.assertEqual(self.link(name), f"{shared.LEGACY_SHARED_LAYOUT.remote_base}/shims/{name}")

    def test_links_from_the_data_layout_are_moved_after_a_firmware_update(self):
        self.links.mkdir()
        for name in self.names:
            os.symlink(f"{shared.SHARED_LAYOUT.remote_base}/shims/{name}", self.links / name)
        self.apply()
        for name in self.names:
            self.assertEqual(self.link(name), f"{shared.LEGACY_SHARED_LAYOUT.remote_base}/shims/{name}")

    def test_foreign_link_is_refused(self):
        self.links.mkdir()
        os.symlink("/opt/other/shim.so", self.links / self.names[0])
        with self.assertRaisesRegex(RuntimeError, "unexpected shim link"):
            self.apply()
        self.assertEqual(self.link(self.names[0]), "/opt/other/shim.so")

    def test_remove_handles_both_layouts_and_keeps_foreign_links(self):
        self.links.mkdir()
        os.symlink(f"{shared.SHARED_LAYOUT.remote_base}/shims/{self.names[0]}", self.links / self.names[0])
        if len(self.names) > 1:
            os.symlink("/opt/other/shim.so", self.links / self.names[1])
        ssh = mock.Mock()
        ssh.exec_checked.side_effect = self.run_script
        _appload.remove_shim_links(ssh, self.device)
        self.assertFalse(os.path.lexists(self.links / self.names[0]))
        if len(self.names) > 1:
            self.assertEqual(self.link(self.names[1]), "/opt/other/shim.so")


if __name__ == "__main__":
    unittest.main()
