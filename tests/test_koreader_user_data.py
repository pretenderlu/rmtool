"""Run the KOReader install/uninstall device scripts in a local sandbox.

These tests check that user data stored inside the KOReader data directory
(the install directory, since rmtool does not set KO_HOME) survives repair,
reinstall and uninstall.
"""

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _appload
import _koreader


SH = shutil.which("sh")
if os.name == "nt":
    git_sh = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if git_sh.is_file():
        SH = str(git_sh)


def _shell_path(path):
    if os.name != "nt":
        return str(path)
    # Git Bash drops unquoted backslashes and its tar reads "C:" as a host.
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    return "/" + drive.rstrip(":").lower() + rest.replace("\\", "/")


HOME = "/home/root"
TARGET_REL = _koreader.APPLOAD_INSTALL_DIR[len(HOME) + 1:]
PRESERVED_REL = _koreader.PRESERVED_INSTALL_DIR[len(HOME) + 1:]

OLD_OFFICIAL = {
    "koreader.sh": b"#!/bin/sh\nold\n",
    "reader.lua": b"old reader\n",
    "external.manifest.json": b"{}\n",
    "git-rev": b"v0\n",
    "defaults.lua": b"old defaults\n",
    "plugins/statistics.koplugin/main.lua": b"old official plugin\n",
    "frontend/removed-in-new-release.lua": b"old program file\n",
}
NEW_OFFICIAL = {
    "koreader.sh": b"#!/bin/sh\nnew\n",
    "reader.lua": b"new reader\n",
    "external.manifest.json": b"{}\n",
    "git-rev": b"v1\n",
    "defaults.lua": b"new defaults\n",
    "plugins/statistics.koplugin/main.lua": b"new official plugin\n",
}
USER_FILES = {
    "settings.reader.lua": b"return {}\n",
    "docsettings/home/root/books/a.sdr/metadata.epub.lua": b"progress\n",
    "hashdocsettings/ab/abcdef.sdr/metadata.pdf.lua": b"highlights\n",
    "data/dict/user.ifo": b"dictionary\n",
    "data/tessdata/chi_sim.traineddata": b"ocr\n",
    "plugins/my.koplugin/main.lua": b"user plugin\n",
    "patches/2-user.lua": b"user patch\n",
    "fonts/user/MyFont.ttf": b"font\n",
    "settings/statistics.sqlite3": b"stats\n",
    "file with space.txt": b"odd name\n",
}
REGENERABLE = {
    "cache/cr3cache/x.cr3": b"cache\n",
    "ota/staged.tar": b"ota\n",
}


def _index(files):
    lines = sorted("koreader/" + name for name in files)
    lines.append("koreader/ota/package.index")
    return ("\n".join(sorted(lines)) + "\n").encode()


def _write_tree(root, files):
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if (root / "koreader.sh").exists():
        (root / "koreader.sh").chmod(0o755)


def _read_tree(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class SandboxSSH:
    """Runs device commands with /home/root mapped into a temporary folder."""

    def __init__(self, root, shell):
        self.root = Path(root)
        self.home = self.root / "home/root"
        self.shell = shell
        (self.home / ".local/share").mkdir(parents=True)
        self.commands = []

    def _map(self, text):
        # The sandbox does not run as root; BusyBox sh prefers its own
        # applets over PATH, so drop the ownership reset textually.
        text = text.replace('chown -R root:root "$STAGE/koreader"', ": no-chown")
        return text.replace(HOME, _shell_path(self.home))

    def exec_checked(self, command):
        self.commands.append(command)
        result = subprocess.run(
            [self.shell, "-c", self._map(command)],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"exit {result.returncode}: {result.stderr.decode(errors='replace')}"
            )
        return result.stdout.decode()

    def exec_command(self, command):
        return "", "", 1

    def transfer_file(self, local, remote):
        destination = Path(remote.replace(HOME, str(self.home)))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, destination)


def _shells():
    shells = []
    if SH:
        shells.append(SH)
    busybox = shutil.which("busybox")
    if busybox and os.name != "nt":
        wrapper = Path(tempfile.mkdtemp(prefix="rmtool-busybox-")) / "sh"
        # Resolve find/sed/awk/... to BusyBox applets too, as on the device.
        for applet in ("awk", "cat", "cp", "dirname", "find", "grep", "mkdir",
                       "mv", "rm", "rmdir", "tar"):
            (wrapper.parent / applet).symlink_to(busybox)
        wrapper.write_text(
            f'#!/bin/sh\nPATH="{wrapper.parent}:$PATH" exec {busybox} sh "$@"\n'
        )
        wrapper.chmod(0o755)
        shells.append(str(wrapper))
    return shells


@unittest.skipUnless(SH, "A POSIX shell (or Git Bash) is required")
class KOReaderUserDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rmtool-koreader-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)

    def sandbox(self, shell):
        root = Path(tempfile.mkdtemp(dir=self.temporary))
        ssh = SandboxSSH(root, shell)
        target = ssh.home / TARGET_REL
        return ssh, target, ssh.home / PRESERVED_REL

    def install_old(self, target, *, user=True):
        _write_tree(target, OLD_OFFICIAL)
        _write_tree(target, {"ota/package.index": _index(OLD_OFFICIAL)})
        (target / ".rmtool-install.json").write_bytes(b"old marker\n")
        if user:
            _write_tree(target, USER_FILES)
            _write_tree(target, REGENERABLE)

    def payload(self):
        app = self.temporary / "payload" / "koreader"
        if app.exists():
            shutil.rmtree(app)
        _write_tree(app, NEW_OFFICIAL)
        _write_tree(app, {"ota/package.index": _index(NEW_OFFICIAL)})
        (app / ".rmtool-install.json").write_bytes(b"new marker\n")
        archive = self.temporary / "payload.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(app, arcname="koreader")
        return app, archive

    def run_install(self, ssh, state, *, enable_error=None, migrate=False):
        identity = SimpleNamespace(architecture="aarch64")
        asset = _appload.OfficialAsset(
            "KOReader", "v1", "aarch64", "koreader.zip", 1, "a" * 64, "url"
        )
        current = _koreader.ManagedStatus(state, identity, asset, "v0")
        app, archive = self.payload()
        modes = {"koreader/koreader.sh": 0o755}
        with (
            mock.patch.object(_koreader.tap, "get_device_identity", return_value=identity),
            mock.patch.object(_koreader._appload, "koreader_asset", return_value=asset),
            mock.patch.object(
                _koreader._appload,
                "get_status",
                return_value=SimpleNamespace(state=_appload.AppLoadState.ENABLED),
            ),
            mock.patch.object(
                _koreader, "get_managed_status", side_effect=(current, "done")
            ),
            mock.patch.object(_koreader, "_test_path", return_value=False),
            mock.patch.object(_koreader._appload, "verify_official_asset", return_value=archive),
            mock.patch.object(_koreader.tap, "_preflight_device"),
            mock.patch.object(
                _koreader._appload, "extract_official_zip", return_value=app.parent
            ),
            mock.patch.object(_koreader, "_validate_koreader_tree", return_value=app),
            mock.patch.object(_koreader, "_official_zip_modes", return_value=modes),
            mock.patch.object(_koreader, "_make_payload_archive", return_value=archive),
            mock.patch.object(_koreader, "_prepare_bridge_root", return_value=app.parent),
            mock.patch.object(_koreader._appload, "ensure_shim_links"),
            mock.patch.object(
                _koreader.tap,
                "_trusted_shared_context",
                return_value=(object(), {_appload.KOREADER_FEATURE_ID: object()}, ()),
            ),
            mock.patch.object(
                _koreader.shared, "enable_shared", side_effect=enable_error
            ),
        ):
            return _koreader.install_managed(
                ssh, archive, "unused", migrate_existing=migrate
            )

    def run_uninstall(self, ssh, *, remove_error=None):
        identity = SimpleNamespace(architecture="aarch64")
        installed = _koreader.ManagedStatus(
            _koreader.ManagedState.INSTALLED, identity, None, "v0"
        )
        with (
            mock.patch.object(
                _koreader, "get_managed_status", side_effect=(installed, "removed")
            ),
            mock.patch.object(_koreader, "_koreader_running", return_value=False),
            mock.patch.object(
                _koreader.tap,
                "_trusted_shared_context",
                return_value=(object(), {}, ()),
            ),
            mock.patch.object(
                _koreader.shared, "remove_shared_features", side_effect=remove_error
            ),
        ):
            return _koreader.uninstall_managed(ssh)

    def assert_user_files(self, root):
        tree = _read_tree(root)
        for name, data in USER_FILES.items():
            self.assertEqual(tree.get(name), data, name)

    def assert_no_leftovers(self, ssh):
        siblings = sorted(p.name for p in (ssh.home / TARGET_REL).parent.iterdir())
        self.assertEqual(siblings, ["koreader"])
        cache = ssh.home / ".cache/rmtool"
        self.assertEqual(sorted(p.name for p in cache.iterdir()) if cache.exists() else [], [])

    def test_reinstall_keeps_all_user_data(self):
        for shell in _shells():
            with self.subTest(shell=shell):
                ssh, target, _preserved = self.sandbox(shell)
                self.install_old(target)
                self.run_install(ssh, _koreader.ManagedState.INSTALLED)
                tree = _read_tree(target)
                self.assert_user_files(target)
                for name, data in NEW_OFFICIAL.items():
                    self.assertEqual(tree[name], data, name)
                self.assertEqual(tree[".rmtool-install.json"], b"new marker\n")
                self.assertNotIn("frontend/removed-in-new-release.lua", tree)
                for name in REGENERABLE:
                    self.assertNotIn(name, tree)
                self.assert_no_leftovers(ssh)

    def test_failed_reinstall_restores_old_install(self):
        for shell in _shells():
            with self.subTest(shell=shell):
                ssh, target, _preserved = self.sandbox(shell)
                self.install_old(target)
                before = _read_tree(target)
                with self.assertRaisesRegex(RuntimeError, "bridge"):
                    self.run_install(
                        ssh,
                        _koreader.ManagedState.REPAIRABLE,
                        enable_error=RuntimeError("bridge failed"),
                    )
                self.assertEqual(_read_tree(target), before)

    def test_uninstall_preserves_all_user_data_and_reinstall_restores_it(self):
        for shell in _shells():
            with self.subTest(shell=shell):
                ssh, target, preserved = self.sandbox(shell)
                self.install_old(target)
                self.run_uninstall(ssh)
                self.assertFalse(target.exists())
                kept = _read_tree(preserved)
                self.assertEqual(kept, USER_FILES)
                self.assertEqual(
                    sorted(p.name for p in target.parent.iterdir()), []
                )

                self.run_install(ssh, _koreader.ManagedState.NOT_INSTALLED)
                self.assert_user_files(target)
                self.assertEqual(_read_tree(target)["reader.lua"], b"new reader\n")
                self.assertFalse(preserved.exists())

    def test_uninstall_without_official_index_keeps_allowlist(self):
        for shell in _shells():
            with self.subTest(shell=shell):
                ssh, target, preserved = self.sandbox(shell)
                self.install_old(target)
                (target / _koreader.OFFICIAL_INDEX).unlink()
                self.run_uninstall(ssh)
                kept = _read_tree(preserved)
                self.assertEqual(kept["settings.reader.lua"], USER_FILES["settings.reader.lua"])
                self.assertEqual(
                    kept["settings/statistics.sqlite3"], USER_FILES["settings/statistics.sqlite3"]
                )
                self.assertNotIn("reader.lua", kept)

    def test_failed_uninstall_restores_install_with_user_data(self):
        for shell in _shells():
            with self.subTest(shell=shell):
                ssh, target, preserved = self.sandbox(shell)
                self.install_old(target)
                before = _read_tree(target)
                with self.assertRaisesRegex(RuntimeError, "bridge"):
                    self.run_uninstall(ssh, remove_error=RuntimeError("bridge"))
                self.assertEqual(_read_tree(target), before)
                self.assertFalse(preserved.exists())

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs POSIX permissions")
    def test_interrupted_uninstall_moves_user_data_back(self):
        for shell in _shells():
            with self.subTest(shell=shell):
                ssh, target, preserved = self.sandbox(shell)
                self.install_old(target)
                before = _read_tree(target)
                # A file that cannot be moved stops the transaction after
                # other user files were already moved to the staging area.
                locked = target / "patches"
                locked.chmod(0o555)
                self.addCleanup(locked.chmod, 0o755)
                with self.assertRaises(RuntimeError):
                    self.run_uninstall(ssh)
                locked.chmod(0o755)
                self.assertEqual(_read_tree(target), before)
                self.assertFalse(preserved.exists())
                self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["koreader"])

    def test_uninstall_refuses_user_data_symlink_without_changes(self):
        for shell in _shells():
            with self.subTest(shell=shell):
                ssh, target, preserved = self.sandbox(shell)
                self.install_old(target)
                outside = ssh.home / "secret.txt"
                outside.write_bytes(b"secret\n")
                (target / "patches/link.lua").symlink_to(outside)
                before = _read_tree(target)
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    self.run_uninstall(ssh)
                self.assertEqual(_read_tree(target), before)
                self.assertFalse(preserved.exists())

    def test_legacy_migration_keeps_allowlist_only(self):
        for shell in _shells():
            with self.subTest(shell=shell):
                ssh, target, _preserved = self.sandbox(shell)
                self.install_old(target)
                self.run_install(ssh, _koreader.ManagedState.EXTERNAL, migrate=True)
                tree = _read_tree(target)
                self.assertEqual(tree["settings.reader.lua"], USER_FILES["settings.reader.lua"])
                self.assertNotIn("plugins/my.koplugin/main.lua", tree)
                legacy = ssh.home / _koreader.LEGACY_BACKUP_DIR[len(HOME) + 1:]
                self.assertEqual(
                    _read_tree(legacy)["plugins/my.koplugin/main.lua"],
                    USER_FILES["plugins/my.koplugin/main.lua"],
                )


if __name__ == "__main__":
    unittest.main()
