import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import _bundle_resources as bundle


ROOT = Path(__file__).resolve().parents[1]


class BundleResourceTests(unittest.TestCase):
    def test_inventory_generates_real_inputs_for_both_platforms(self):
        arguments = bundle.pyinstaller_data_args(ROOT)
        self.assertEqual(arguments[::2], ["--add-data"] * len(bundle.DATA_PATHS))
        for argument in arguments[1::2]:
            source, destination = argument.rsplit(os.pathsep, 1)
            self.assertTrue(Path(source).exists(), argument)
            self.assertFalse(Path(destination).is_absolute())

    def test_self_test_actually_loads_qt_fonts_images_manifests_and_certs(self):
        result = subprocess.run([sys.executable, str(ROOT / "rmtool.py"), "--bundle-self-test"],
                                cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=60,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_manifest_fails_instead_of_only_checking_file_existence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in bundle.DATA_PATHS:
                path = root / relative
                if relative in bundle.MANIFESTS:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"schema_version": 0}))
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"present")
            with self.assertRaisesRegex(RuntimeError, "Invalid bundled manifest"):
                bundle.self_test(root)


if __name__ == "__main__":
    unittest.main()
