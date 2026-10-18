"""Replay sidebar order against exact firmware resources and installed peer QMDs."""
import itertools
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

import _qr_upload as qr


ROOT = Path(__file__).resolve().parents[1]


class QrSidebarTests(unittest.TestCase):
    def test_sidebar_anchor_precedes_appended_peer_entries(self):
        source = (ROOT / "qr-upload/native/qr-upload-3.28.qmd").read_text(encoding="utf-8")
        sidebar = source.split("TRAVERSE ?#settingsSidebar > ColumnLayout#settingsColumn", 1)[1].split("END TRAVERSE", 1)[0]
        self.assertIn("LOCATE AFTER Item", sidebar)
        self.assertNotIn("LOCATE AFTER ALL", sidebar)

    @unittest.skipUnless(os.environ.get("RMTOOL_QR_RESOURCE_TARGETS"), "exact QR resource paths not configured")
    def test_exact_firmware_sidebar_order_with_peer_subsets_and_permutations(self):
        targets = json.loads(os.environ["RMTOOL_QR_RESOURCE_TARGETS"])
        tool = os.environ["RMTOOL_QMLDIFF"]
        for package in qr._trusted_catalog():
            with self.subTest(release=package.release_version), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                qmds = {}
                for feature, expected_id in (
                    ("qr-upload", "rmtoolQrSidebarItem"),
                    ("reading-enhancements", "rmtoolReadingEnhancementsSidebarItem"),
                    ("note-enhancements", "rmtoolNoteEnhancementsSidebarItem"),
                    ("native-chinese", None), ("pinyin-input", None),
                ):
                    manifest = json.loads((ROOT / feature / "manifest.json").read_text())
                    entry = next(p for p in manifest["packages"] if p["xochitl_sha256"] == package.xochitl_sha256)
                    archive = ROOT / ".rmtool/cache" / feature / package.firmware / entry["asset"]
                    with tarfile.open(archive) as bundle:
                        member = next(m for m in bundle.getmembers() if m.name.endswith(f"/{feature}.qmd"))
                        qmd = work / f"{feature}.qmd"
                        qmd.write_bytes(bundle.extractfile(member).read())
                    qmds[feature] = (qmd, expected_id)
                groups = [("qr-upload",), ("qr-upload", "reading-enhancements"),
                          ("qr-upload", "note-enhancements"),
                          ("qr-upload", "reading-enhancements", "note-enhancements")]
                orders = [order for group in groups for order in itertools.permutations(group)]
                orders.extend((tuple(qmds), tuple(reversed(qmds))))
                resource = Path(targets[package.release_version])
                for index, order in enumerate(orders):
                    with self.subTest(order=order):
                        output = work / f"replay-{index}"
                        result = subprocess.run(
                            [tool, "apply-diffs", "--hashtab", str(resource / "hashtab"), "-c",
                             str(resource / "qrex-out"), str(output), *(str(qmds[f][0]) for f in order)],
                            capture_output=True, timeout=30,
                        )
                        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))
                        settings = (output / "qml/device/view/settings/Settings.qml").read_text(encoding="utf-8")
                        main_view = (output / "qml/device/view/main/MainView.qml").read_text(encoding="utf-8")
                        self.assertIn("ImportControl", main_view)
                        self.assertIn("settings: Settings", main_view)
                        qr_position = settings.index("id: rmtoolQrSidebarItem")
                        for feature in order:
                            if feature != "qr-upload" and qmds[feature][1]:
                                self.assertLess(qr_position, settings.index("id: " + qmds[feature][1]))
