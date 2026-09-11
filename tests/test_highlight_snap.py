import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reading-enhancements/native/highlight_snap.c"
BUILDER = ROOT / "reading-enhancements/native/build_highlight_snap.py"
ZIG = Path(r"E:\remarkable\tools\zig-0.16.0\zig.exe")
COMMITTED = ROOT / "reading-enhancements/native/rmtool-highlight-snap.so"
XOCHITL_TARGETS = {
    "chiappa": (
        Path(r"E:\remarkable\firmware-cache\official\3.28.0.172\chiappa\extracted\selected-rootfs\usr\bin\xochitl"),
        "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4",
        11548272,
    ),
    "ferrari": (
        Path(r"E:\remarkable\firmware-cache\official\3.28.0.172\ferrari\extracted\selected-rootfs\usr\bin\xochitl"),
        "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021",
        11825296,
    ),
}
TARGET_PATTERN = bytes.fromhex(
    "3f2303d5fd7bbda9fd030091f51300f9f50300aa200040f9"
    "f35301a9f40301aa800000b4010040b9"
)


class HighlightSnapTests(unittest.TestCase):
    def test_source_fails_closed_and_has_exact_verified_identity(self):
        source = SOURCE.read_text(encoding="utf-8")
        for marker in (
            'EXPECTED_FIRMWARE "20260827113527"',
            'MOVE_XOCHITL_SHA256 "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4"',
            'PAPER_PRO_XOCHITL_SHA256 "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021"',
            'strcmp(buf,"remarkable chiappa")==0',
            'strcmp(buf,"remarkable paper pro move")==0',
            'strcmp(buf,"remarkable ferrari")==0',
            'strcmp(buf,"remarkable paper pro")==0',
            'strcmp(path,"/usr/bin/xochitl")!=0',
            "count==1?found:0",
            'strcmp(key,"masterEnabled")',
            'strcmp(key,"hlSnapCjk")',
            "seen_master==1&&seen_snap==1&&master&&snap",
            "ch>=0x4e00&&ch<=0x9fff",
            "ch>=0x3000&&ch<=0x303f",
        ):
            self.assertIn(marker, source)
        self.assertIn(
            'strcmp(buf,"remarkable paper pro move")==0)return MOVE_XOCHITL_SHA256',
            source,
        )
        self.assertIn(
            'strcmp(buf,"remarkable paper pro")==0)return PAPER_PRO_XOCHITL_SHA256',
            source,
        )
        self.assertIn("xochitl_hash_matches(hash)", source)
        self.assertIn("if(!_xovi_shouldLoad())return", source)

    def test_verified_xochitls_have_exact_hash_and_unique_signature(self):
        for platform, (path, expected_hash, expected_offset) in XOCHITL_TARGETS.items():
            with self.subTest(platform=platform):
                if not path.is_file():
                    self.skipTest(f"verified {platform} xochitl is not cached")
                data = path.read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected_hash)
                self.assertEqual(data.count(TARGET_PATTERN), 1)
                self.assertEqual(data.index(TARGET_PATTERN), expected_offset)

    @unittest.skipUnless(ZIG.is_file(), "AArch64 Zig compiler is not configured")
    def test_builds_aarch64_xovi_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "highlight.so"
            result = subprocess.run(
                [sys.executable, str(BUILDER), "--zig", str(ZIG), "--output", str(output)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = output.read_bytes()
            self.assertEqual(data[:4], b"\x7fELF")
            self.assertEqual(int.from_bytes(data[18:20], "little"), 183)
            self.assertEqual(data, COMMITTED.read_bytes())


if __name__ == "__main__":
    unittest.main()
