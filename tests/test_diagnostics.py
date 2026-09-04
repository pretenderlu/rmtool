import os
import unittest
import zipfile
from unittest import mock
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import rmtool  # noqa: F401  (must load before tab modules)
import _diagnostics


class FakeDiagnosticsSsh:
    """Records commands and returns canned outputs, like SSHClientWrapper."""

    def __init__(self, outputs=None, fail_names=()):
        self.commands = []
        self.outputs = dict(outputs or {})
        self.fail_names = set(fail_names)

    def exec_command(self, command):
        self.commands.append(command)
        for name, output in self.outputs.items():
            if name in command:
                if name in self.fail_names:
                    raise OSError("connection lost")
                return output, "", 0
        return "", "", 0


class DiagnosticsTests(unittest.TestCase):
    def test_device_commands_are_read_only_whitelisted(self):
        self.assertGreater(len(_diagnostics.DEVICE_ITEMS), 8)
        names = [item.name for item in _diagnostics.DEVICE_ITEMS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(item.name.startswith("device/") for item in _diagnostics.DEVICE_ITEMS))
        for item in _diagnostics.DEVICE_ITEMS:
            for fragment in _diagnostics.FORBIDDEN_FRAGMENTS:
                self.assertNotIn(
                    fragment,
                    item.command,
                    f"{item.name} contains forbidden fragment {fragment!r}",
                )
            self.assertRegex(item.command, r"^(cat|ls|journalctl|systemctl show|"
                            r"sha256sum|df|uname|uptime|for)")
        # The device journal that may contain document names is opt-out.
        optional = [item for item in _diagnostics.DEVICE_ITEMS if item.optional]
        self.assertEqual(
            [item.name for item in optional], ["device/journal-xochitl.txt"]
        )
        resources = next(
            item for item in _diagnostics.DEVICE_ITEMS
            if item.name == "device/resources.txt"
        )
        self.assertIn("head -n 5", resources.command)
        self.assertNotIn("head -5", resources.command)

    def test_collect_caps_remote_output_and_records_failures(self):
        big = "x" * (_diagnostics.ITEM_CAP_BYTES + 4096)
        ssh = FakeDiagnosticsSsh(
            outputs={
                "/etc/version": "3.28.0.169\n",
                "journalctl -u xochitl": big,
            },
            fail_names=("/etc/version",),
        )
        collected = _diagnostics.collect(ssh)
        by_name = {result.item.name: result for result in collected}
        self.assertIn("采集失败", by_name["device/system-identity.txt"].error)
        journal = by_name["device/journal-xochitl.txt"]
        self.assertLessEqual(len(journal.text.encode()), _diagnostics.ITEM_CAP_BYTES)
        self.assertTrue(journal.truncated)
        # Every remote command is wrapped with the read-only tail cap
        # (device BusyBox head has no -c).
        for command in ssh.commands:
            self.assertIn("| tail -c 65536", command)

    def test_pc_log_tail_is_capped(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "remarkable_tool.log"
            log.write_bytes(b"a" * (_diagnostics.PC_LOG_TAIL_BYTES + 2048))
            collected = _diagnostics.collect(
                FakeDiagnosticsSsh(), pc_log_path=log
            )
        tail = next(
            result
            for result in collected
            if result.item.name == "pc/rmtool-log-tail.txt"
        )
        self.assertEqual(
            len(tail.text.encode()), _diagnostics.PC_LOG_TAIL_BYTES
        )
        self.assertTrue(tail.truncated)
        self.assertTrue(tail.item.optional)
        self.assertIn("本地路径", tail.item.description)

        missing = _diagnostics.collect(
            FakeDiagnosticsSsh(), pc_log_path=Path(folder) / "absent.log"
        )
        absent = next(
            result
            for result in missing
            if result.item.name == "pc/rmtool-log-tail.txt"
        )
        self.assertIn("不存在", absent.error)

    def test_platform_label_uses_soc0_machine(self):
        identity = (
            "20260806095513\n---\nLinux remarkable 6.6 aarch64\n---\n"
            "reMarkable Ferrari\n---\n"
            "8726b4fce55a  /usr/bin/xochitl\n"
        )
        ssh = FakeDiagnosticsSsh(outputs={"/etc/version": identity})
        collected = _diagnostics.collect(ssh)
        self.assertEqual(_diagnostics.platform_label(collected), "ferrari")
        self.assertRegex(
            _diagnostics.bundle_name("ferrari"),
            r"^rmtool-diag-ferrari-\d{8}-\d{6}-[0-9a-f]{8}\.zip$",
        )
        self.assertEqual(
            _diagnostics.platform_label(
                [_diagnostics.CollectedItem(_diagnostics.DiagItem("x", "x"))]
            ),
            "device",
        )

    def test_write_bundle_creates_zip_with_manifest_and_exclusions(self):
        import tempfile
        from pathlib import Path

        identity = (
            "20260612085811\n---\nLinux remarkable 6.6 aarch64\n---\n"
            "reMarkable Chiappa\n---\n"
            "227a9bfe928e  /usr/bin/xochitl\n"
        )
        ssh = FakeDiagnosticsSsh(
            outputs={
                "/etc/version": identity,
                "package.json": '{"schema_version": 1}',
            }
        )
        collected = _diagnostics.collect(ssh)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "diag.zip"
            included = [
                result
                for result in collected
                if result.item.name
                not in {
                    "pc/rmtool-log-tail.txt",
                    "device/journal-xochitl.txt",
                }
            ]
            saved = _diagnostics.write_bundle(target, collected, included)
            self.assertEqual(saved, target)
            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                manifest = archive.read("MANIFEST.txt").decode("utf-8")
                marker = archive.read("device/shared-marker.txt").decode("utf-8")
        self.assertIn("MANIFEST.txt", names)
        self.assertIn("pc/environment.txt", names)
        self.assertNotIn("pc/rmtool-log-tail.txt", names)
        self.assertNotIn("device/journal-xochitl.txt", names)
        self.assertIn('{"schema_version": 1}', marker)
        self.assertIn("device platform: chiappa", manifest)
        self.assertIn("device/journal-xochitl.txt: excluded", manifest)
        self.assertIn("device/shared-marker.txt: ok (", manifest)
        self.assertIn("device/system-identity.txt: ok (", manifest)
        self.assertIn("pc/rmtool-log-tail.txt: excluded", manifest)

    def test_write_bundle_rejects_oversized_content(self):
        huge = [
            _diagnostics.CollectedItem(
                _diagnostics.DiagItem("big.txt", "big"),
                "y" * (_diagnostics.TOTAL_BUDGET_BYTES + 1),
            )
        ]
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "大小预算"):
                _diagnostics.write_bundle(Path(folder) / "big.zip", huge)


if __name__ == "__main__":
    import unittest

    unittest.main()
