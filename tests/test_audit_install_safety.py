import hashlib
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _appload
import _koreader as koreader
import _xovi_standalone as shared


SH = shutil.which("sh")


@unittest.skipUnless(SH, "A POSIX shell is required for remote-script regressions")
class InstallSafetyTests(unittest.TestCase):
    def run_shell(self, command, root):
        self.assertNotIn("\0", command)
        syntax = subprocess.run(
            [SH, "-n", "-c", command], cwd=root, capture_output=True,
            text=True, timeout=20,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        return subprocess.run(
            [SH, "-c", command], cwd=root, capture_output=True, text=True,
            timeout=20,
        )

    def test_process_detection_uses_executable_or_interpreter_script(self):
        target = koreader.APPLOAD_INSTALL_DIR
        capture = mock.Mock()
        capture.exec_command.return_value = ("", "", 1)
        koreader._koreader_running(capture)
        detector = capture.exec_command.call_args.args[0]
        cases = (
            ("detector itself", "/bin/sh", ["sh", "-c", detector], "/", False),
            ("parent shell", "/bin/sh", ["sh", "-c", "sh -c " + detector], "/", False),
            ("unrelated argument", "/bin/cat", ["cat", target + "/koreader.sh"], "/", False),
            ("shell command", "/bin/sh", ["sh", "-c", target + "/koreader.sh"], "/", False),
            ("native process", target + "/luajit", ["./luajit", "reader.lua"], target, True),
            ("absolute script", "/bin/sh", ["sh", target + "/koreader.sh"], "/", True),
            ("shell option", "/bin/sh", ["sh", "-e", target + "/koreader.sh"], "/", True),
            ("relative script", "/bin/sh", ["sh", "./koreader.sh"], target, True),
            ("system luajit", "/usr/bin/luajit", ["luajit", "reader.lua"], target, True),
            ("empty cmdline", "/bin/sh", [], "/", False),
        )
        for label, exe, argv, cwd, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                proc = root / "proc" / "123"
                proc.mkdir(parents=True)
                (proc / "exe").write_text(exe)
                (proc / "cwd").write_text(cwd)
                (proc / "cmdline").write_bytes(
                    b"\0".join(arg.encode() for arg in argv) + b"\0"
                )
                # Only /proc and readlink are simulated; execute the generated
                # detector unchanged against NUL-delimited process arguments.
                command = 'readlink() { cat "$1"; };\n' + detector.replace(
                    "/proc/[0-9]*", "./proc/[0-9]*"
                )
                ssh = mock.Mock()

                def execute(_command):
                    result = self.run_shell(command, root)
                    return result.stdout, result.stderr, result.returncode

                ssh.exec_command.side_effect = execute
                self.assertEqual(koreader._koreader_running(ssh), expected)

    def exercise_install(self, *, failure="", existing=True, running=False):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            device = root / "device"
            device.mkdir()
            target = device / koreader.APPLOAD_INSTALL_DIR.removeprefix("/home/root/")
            if existing:
                target.mkdir(parents=True)
                (target / "old-payload").write_bytes(b"healthy old payload")
                (target / "settings.reader.lua").write_bytes(b"user settings")
            app = root / "official" / "koreader"
            app.mkdir(parents=True)
            for name, content in {
                "koreader.sh": b"#!/bin/sh\n", "icon.png": b"icon",
                "git-rev": b"v1", "external.manifest.json": b"{}",
            }.items():
                (app / name).write_bytes(content)
            asset = _appload.OfficialAsset(
                "KOReader", "v1", "aarch64", "koreader.zip", 1, "a" * 64, "url"
            )
            identity = SimpleNamespace(architecture="aarch64")
            status = koreader.ManagedStatus(
                koreader.ManagedState.INSTALLED if existing else koreader.ManagedState.NOT_INSTALLED,
                identity, asset,
            )
            patches = (
                (koreader.tap, "get_device_identity", identity),
                (_appload, "koreader_asset", asset),
                (_appload, "get_status", SimpleNamespace(state=_appload.AppLoadState.ENABLED)),
                (koreader, "get_managed_status", status),
                (koreader, "_koreader_running", running),
                (_appload, "verify_official_asset", root / "unused.zip"),
                (koreader.tap, "_preflight_device", None),
                (_appload, "extract_official_zip", app.parent),
                (koreader, "_validate_koreader_tree", app),
                (koreader, "_official_zip_modes", {}),
                (koreader, "_prepare_bridge_root", root),
                (_appload, "ensure_shim_links", None),
                (koreader.tap, "_trusted_shared_context", (
                    object(), {_appload.KOREADER_FEATURE_ID: object()}, (),
                )),
            )
            for module, name, value in patches:
                stack.enter_context(mock.patch.object(module, name, return_value=value))
            enable = stack.enter_context(mock.patch.object(shared, "enable_shared"))
            if failure == "bridge":
                enable.side_effect = RuntimeError("injected bridge failure")
            ssh = mock.Mock()
            scripts = []

            def execute(command):
                if "tar -xzf" in command:
                    scripts.append(command)
                    # Inject failures in actual shell operations, including a
                    # signal after backup creation, before Python sees success.
                    command = f'''
move_count=0
mv() {{
    move_count=$((move_count + 1))
    if [ "$move_count" = "{failure}" ]; then return 71; fi
    command mv "$@" || return $?
    if [ "{failure}" = signal ] && [ "$move_count" = 1 ]; then kill -TERM $$; fi
}}
rmdir() {{
    if [ "{failure}" = rmdir ]; then return 72; fi
    command rmdir "$@"
}}
''' + command
                command = 'chown() { :; };\n' + command.replace("/home/root", "./device")
                result = self.run_shell(command, root)
                if result.returncode:
                    raise RuntimeError(f"injected shell failure: {result.returncode}: {result.stderr}")
                return result.stdout

            ssh.exec_checked.side_effect = execute
            ssh.transfer_file.side_effect = lambda local, remote: shutil.copyfile(
                local, device / remote.removeprefix("/home/root/")
            )
            if failure or running:
                with self.assertRaisesRegex(RuntimeError, "injected|正在运行"):
                    koreader.install_managed(ssh, root / "unused.zip", str(root))
                if existing:
                    self.assertEqual((target / "old-payload").read_bytes(), b"healthy old payload")
                    self.assertEqual((target / "settings.reader.lua").read_bytes(), b"user settings")
                    self.assertFalse((target / "git-rev").exists())
                else:
                    self.assertFalse(target.exists())
            else:
                koreader.install_managed(ssh, root / "unused.zip", str(root))
                self.assertEqual((target / "git-rev").read_bytes(), b"v1")
                if existing:
                    self.assertEqual((target / "settings.reader.lua").read_bytes(), b"user settings")
                self.assertFalse((target / "old-payload").exists())
            self.assertFalse(list(device.rglob("*.rmtool-backup-*")))
            self.assertFalse(list(device.rglob("koreader-stage-*")))
            if running:
                ssh.transfer_file.assert_not_called()
                ssh.exec_checked.assert_not_called()
            else:
                self.assertEqual(len(scripts), 1)

    def test_swap_failures_restore_old_payload(self):
        for failure in ("1", "2", "signal", "rmdir", "bridge"):
            with self.subTest(failure=failure):
                self.exercise_install(failure=failure)

    def test_failed_fresh_install_leaves_no_target(self):
        for failure in ("1", "rmdir", "bridge"):
            with self.subTest(failure=failure):
                self.exercise_install(failure=failure, existing=False)

    def test_success_preserves_user_data(self):
        self.exercise_install()
        self.exercise_install(existing=False)

    def test_running_reader_refuses_before_transfer(self):
        self.exercise_install(running=True)


class SharedMigrationSafetyTests(unittest.TestCase):
    def test_migration_preserves_enabled_state(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), ExitStack() as stack:
                runtime = shared.SharedRuntimeSpec("new", "chiappa", "aarch64", "a" * 64, ())
                feature = shared.SharedFeatureSpec(
                    "tap-page-turn", "test", "tap.qmd", "tap.qmd", "b" * 64, 1, 0o644,
                )
                trusted = {feature.feature_id: feature}
                residue = shared.SharedInspection({
                    feature.feature_id: shared.SharedFeatureState(feature, enabled, "old-token")
                }, False, enabled)
                uploaded = {}
                stack.enter_context(mock.patch.object(
                    shared, "_upload_bytes",
                    side_effect=lambda ssh, data, path, mode: uploaded.update({path: data}),
                ))
                stack.enter_context(mock.patch.object(
                    shared, "_remote_sha256",
                    side_effect=lambda ssh, path: hashlib.sha256(uploaded[path]).hexdigest(),
                ))
                for name in ("assert_feature_layout", "_assert_managed_dropins"):
                    stack.enter_context(mock.patch.object(shared, name))
                stack.enter_context(mock.patch.object(shared, "inspect_shared_firmware_residue", return_value=residue))
                stack.enter_context(mock.patch.object(shared, "_process_token", return_value="new-token"))
                stack.enter_context(mock.patch.object(shared, "inspect_shared", return_value=residue))
                real_stage = shared._stage_shared
                stage = stack.enter_context(mock.patch.object(
                    shared, "_stage_shared", side_effect=real_stage if not enabled else None,
                ))
                transaction = stack.enter_context(mock.patch.object(
                    shared, "shared_transaction_script", wraps=shared.shared_transaction_script,
                ))
                shared.migrate_shared(
                    mock.Mock(), runtime, trusted, runtime, trusted,
                    {feature.feature_id: Path("verified")} if enabled else {},
                )
                state = stage.call_args.args[2][feature.feature_id]
                self.assertEqual(state.enabled, enabled)
                self.assertEqual(state.process_token, "new-token" if enabled else "old-token")
                self.assertEqual(transaction.call_args.kwargs["enable_dropin"], enabled)
                if not enabled:
                    staged_files = [path for path in uploaded if ".staging-" in path]
                    self.assertEqual(staged_files, [stage.call_args.args[-1] + "/package.json"])
                    script = next(data for path, data in uploaded.items() if path.endswith(".sh"))
                    self.assertNotIn((shared.SHARED_LAYOUT.remote_base + "/systemd/").encode(), script)


if __name__ == "__main__":
    unittest.main()
