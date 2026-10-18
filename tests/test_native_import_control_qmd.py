"""Native and QR controllers must have distinct QML types and compose with peers."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

import _native_import_control as control

ROOT = Path(__file__).resolve().parents[1]


class NativeControlQmdTests(unittest.TestCase):
    def test_standalone_type_cannot_shadow_existing_qr_type(self):
        qmd = (ROOT / "native-import/native/native-import.qmd").read_text()
        main = (ROOT / "native-import/native/main.cpp").read_text()
        build = (ROOT / "native-import/build.py").read_text()
        common = (ROOT / "qr-upload/native/ImportControl.cpp").read_text()
        self.assertIn("IMPORT cn.rmtool.NativeImport 1.0", qmd)
        self.assertIn("NativeImportControl { settings: Settings }", qmd)
        self.assertNotIn("INSERT { ImportControl", qmd)
        self.assertIn('"NativeImportControl"', main)
        self.assertEqual(build.count('"-DImportControl=NativeImportControl"'), 2)
        self.assertIn("RTLD_NOW | RTLD_NOLOAD", common)
        self.assertIn('dlsym(qr, "_ZN13ImportControl16staticMetaObjectE")', common)
        self.assertIn("dlclose(qr)", common)
        self.assertNotIn('dlsym(RTLD_DEFAULT, "_ZN13ImportControl16staticMetaObjectE")', common)
        self.assertLess(common.index("#ifdef RMTOOL_NATIVE_IMPORT_STANDALONE", common.index("void ImportControl::setSettings")),
                        common.index("m_settings = settings"))

    @unittest.skipUnless(os.environ.get("RMTOOL_NATIVE_IMPORT_RESOURCE_TARGETS"), "exact native-import resources not configured")
    def test_all_exact_targets_replay_with_peers_in_both_orders(self):
        targets = json.loads(os.environ["RMTOOL_NATIVE_IMPORT_RESOURCE_TARGETS"])
        tool = os.environ["RMTOOL_QMLDIFF"]
        for package in control._trusted_catalog():
            with self.subTest(platform=package.platform, release=package.release_version), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                qmds = {}
                for feature in ("native-import", "native-chinese", "pinyin-input", "reading-enhancements",
                                "note-enhancements", "tap-page-turn", "weread-launcher", "qr-upload"):
                    manifest = json.loads((ROOT / feature / "manifest.json").read_text())
                    entry = next((p for p in manifest["packages"] if p["xochitl_sha256"] == package.xochitl_sha256), None)
                    if entry is None:
                        continue
                    # Reading enhancements already contain tap-to-turn handling.
                    if feature == "tap-page-turn" and "reading-enhancements" in qmds:
                        continue
                    archive = ROOT / ".rmtool/cache" / feature / package.firmware / entry["asset"]
                    with tarfile.open(archive) as bundle:
                        member = next(m for m in bundle.getmembers() if m.name.endswith(f"/{feature}.qmd"))
                        qmd = work / f"{feature}.qmd"
                        qmd.write_bytes(bundle.extractfile(member).read())
                    qmds[feature] = qmd
                orders = [("native-import",), tuple(qmds), tuple(reversed(qmds))]
                for peer in qmds:
                    if peer != "native-import":
                        orders.extend((("native-import", peer), (peer, "native-import")))
                resource = Path(targets[f"{package.platform}/{package.release_version}"])
                for order in orders:
                    with self.subTest(order=order), tempfile.TemporaryDirectory(dir=work) as output:
                        result = subprocess.run(
                            [tool, "apply-diffs", "--hashtab", str(resource / "hashtab"), "-c",
                             str(resource / "qrex-out"), output, *(str(qmds[f]) for f in order)],
                            capture_output=True, timeout=30,
                        )
                        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))
                        view = (Path(output) / "qml/device/view/main/MainView.qml").read_text(encoding="utf-8")
                        self.assertEqual(view.count("NativeImportControl {"), 1)
                        self.assertIn("import cn.rmtool.NativeImport 1.0", view)
                        if "qr-upload" in order:
                            self.assertEqual(view.count("ImportControl {"), 2)
                            self.assertIn("import cn.rmtool.QrUpload 1.0", view)
