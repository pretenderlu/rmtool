"""Executable native identity tests; no device or ARM emulation required."""

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import native_build, validate_build_matrix as matrix

ROOT = native_build.ROOT
ZIG = Path("E:/remarkable/tools/zig-0.16.0/zig.exe")
GO = Path(shutil.which("go") or "C:/Program Files/Go/bin/go.exe")


class NativeBuildTests(unittest.TestCase):
    def test_hashtab_guard_matches_exact_carrier_before_qmd_compilation(self):
        data = b"verified table"
        from types import SimpleNamespace
        expected = SimpleNamespace(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        package = SimpleNamespace(file=lambda _path: expected)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hashtab"
            path.write_bytes(data)
            native_build.verify_carrier_hashtab(path, package)
            path.write_bytes(b"different table")
            with self.assertRaisesRegex(RuntimeError, "hashtab"):
                native_build.verify_carrier_hashtab(path, package)

    def test_manifest_identities_execute_in_go(self):
        if not GO.is_file():
            self.skipTest("Go must be installed for executable native identity checks")
        matrix.validate_native_identities(GO)

    def test_tool_lock_refuses_modified_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            tool = Path(temporary) / "zig.exe"
            tool.write_bytes(b"unverified")
            with self.assertRaisesRegex(RuntimeError, "Unverified build tool"):
                native_build.verify_tool("zig", tool)

    def test_toolchain_tree_lock_includes_names_and_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "header.h").write_bytes(b"pinned")
            expected = hashlib.sha256(b"header.h\0" + hashlib.sha256(b"pinned").digest()).hexdigest()
            with tempfile.TemporaryDirectory() as lock_dir:
                lock = Path(lock_dir) / "lock.json"
                lock.write_text('{"trees":{"fixture":"' + expected + '"}}', encoding="utf-8")
                with patch.object(native_build, "LOCK", lock):
                    self.assertEqual(native_build.verify_tree("fixture", root), expected)
                    (root / "header.h").write_bytes(b"modified")
                    with self.assertRaisesRegex(RuntimeError, "Unverified toolchain"):
                        native_build.verify_tree("fixture", root)

    def test_go_environment_ignores_host_build_overrides(self):
        with patch.dict(os.environ, GOEXPERIMENT="unexpected", GOARCH="arm64", GOFLAGS="-race", GOTOOLCHAIN="auto"):
            env = native_build.go_environment()
        self.assertNotIn("GOEXPERIMENT", env)
        self.assertNotIn("GOARCH", env)
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOENV"], "off")
        self.assertEqual(env["GOFLAGS"], "")

    def test_native_path_check_refuses_both_path_encodings(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "bridge.so"
            for spelling in (str(ROOT), ROOT.as_posix()):
                for encoding in ("utf-8", "utf-16le"):
                    binary.write_bytes(spelling.encode(encoding))
                    with self.assertRaisesRegex(RuntimeError, "Build-machine path"):
                        native_build.check_no_build_paths(binary)

    def test_binary_cross_check_rejects_old_helper_and_wrong_elf(self):
        package = next(p for p in matrix._manifest("native-import")["packages"] if p["release_version"] == "3.29.1.0")
        prefix = b"\x7fELF" + bytes((2 if package["architecture"] == "aarch64" else 1, 1)) + b"\0" * 12
        prefix += (183 if package["architecture"] == "aarch64" else 40).to_bytes(2, "little")
        good = prefix + package["firmware"].encode() + package["xochitl_sha256"].encode()
        matrix.validate_native_payload(package, "helper", good)
        for bad in (good.replace(package["firmware"].encode(), b"20261009073202"), good.replace(package["xochitl_sha256"].encode(), b"0" * 64), b"not ELF"):
            with self.assertRaisesRegex(RuntimeError, "identity|architecture"):
                matrix.validate_native_payload(package, "helper", bad)

    @unittest.skipUnless(ZIG.is_file(), "Pinned Windows Zig is not installed")
    def test_highlighter_identity_gate_executes_all_releases(self):
        releases = {
            "3.28.0.172": ("20260827113527", matrix.OFFICIAL_172, []),
            "3.29.0.152": ("20261009073202", matrix.OFFICIAL_329, ["-DRMTOOL_HIGHLIGHT_BETA_329"]),
            "3.29.0.153": ("20261009073202", matrix.OFFICIAL_329_153, ["-DRMTOOL_HIGHLIGHT_329_153"]),
            "3.29.1.0": ("20261028104944", matrix.OFFICIAL_32910, ["-DRMTOOL_HIGHLIGHT_32910"]),
            "3.30.0.108": ("20261028121323", matrix.OFFICIAL_330, ["-DRMTOOL_HIGHLIGHT_330"]),
        }
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "identity.exe"
            for release, (build, targets, defines) in releases.items():
                subprocess.run([str(ZIG), "cc", "-Wall", "-Wextra", "-Werror", *defines,
                                str(ROOT / "reading-enhancements/native/test_highlight_identity.c"), "-o", str(executable)],
                               check=True, capture_output=True, timeout=120)
                for candidate_release, (candidate_build, candidates, _) in releases.items():
                    for platform, candidate in candidates.items():
                        for expected in (targets["ferrari"]["xochitl_sha256"], targets["chiappa"]["xochitl_sha256"]):
                            result = subprocess.run([str(executable), candidate_build, expected, candidate["xochitl_sha256"]], timeout=10)
                            accepted = release == candidate_release and platform in ("ferrari", "chiappa") and expected == candidate["xochitl_sha256"]
                            self.assertEqual(result.returncode, 0 if accepted else 1, (release, candidate_release, platform))
                self.assertEqual(subprocess.run([str(executable), "unknown", targets["chiappa"]["xochitl_sha256"], targets["chiappa"]["xochitl_sha256"]], timeout=10).returncode, 1)

    @unittest.skipUnless(ZIG.is_file(), "Pinned Windows Zig is not installed")
    def test_aarch64_translator_rebuild_is_repeatable(self):
        spec = importlib.util.spec_from_file_location("aarch64_translator", ROOT / "native-chinese/build_translator_aarch64.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            first = builder.build_translator(ZIG, Path(temporary) / "one.so")
            second = builder.build_translator(ZIG, Path(temporary) / "two.so")
            self.assertEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())

    @unittest.skipUnless(ZIG.is_file(), "Pinned Windows Zig is not installed")
    def test_weread_file_identity_cache_invalidates_and_force_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "cache.exe"
            subprocess.run([str(ZIG), "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            str(ROOT / "weread-launcher/native/test_trusted_file_cache.cpp"), "-o", str(executable)],
                           check=True, capture_output=True, timeout=120)
            subprocess.run([str(executable)], check=True, capture_output=True, timeout=10)
            # Windows may briefly retain an executed image after process exit.
            for attempt in range(20):
                try:
                    executable.unlink()
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)

    def test_tapping_last_page_repeatedly_never_adds_a_page(self):
        from PyQt5 import QtQml
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        reading_spec = importlib.util.spec_from_file_location("tap_adapter", ROOT / "reading-enhancements/build_assets.py")
        adapter = importlib.util.module_from_spec(reading_spec)
        reading_spec.loader.exec_module(adapter)
        for feature in ("tap-page-turn", "reading-enhancements"):
            original = (ROOT / feature / f"qmd-src/{feature}-3.28.qmd").read_text(encoding="utf-8")
            for release in ("3.28.0.172", "3.29.0.153", "3.29.1.0"):
                source = adapter._tap_source_for_release(original, release)
                branch = re.search(r"const rmtoolDirection =.*?(?=\n\s*}\s*\n\s*END REBUILD)", source, re.S).group()
                engine = QtQml.QJSEngine()
                result = engine.evaluate('''
                    var newPageVisible=true, added=0, page=4, count=5, direction=1, linked=false;
                    var console={warn:function(){}}, linkPressed=false;
                    function next(){if(page<count-1)page++;else if(newPageVisible)added++;else newPageVisible=true;}
                    function previous(){if(page>0)page--;}
                    function touchClicked(){newPageVisible=false;}
                    function reset(){}
                    var touchArea={rmtoolTapPageDirection:function(){return direction;}};
                    var view={moveForward:next,moveBackward:previous,rightPageRequested:next,leftPageRequested:previous};
                    function click(pos){''' + branch + '''}
                    for(var i=0;i<10;i++)click({});
                    added;
                ''')
                self.assertFalse(result.isError(), result.toString())
                self.assertEqual(result.toInt(), 0, (feature, release))
                self.assertEqual(engine.evaluate("page=2;click({});page;").toInt(), 3)
                self.assertEqual(engine.evaluate("direction=-1;click({});page;").toInt(), 2)
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
