"""Run the seven audit findings' offline regression checks.

Replaces the original buggy-behavior reproducer after implementing the fixes.
No device connection, installation, or firmware operation is performed.
"""

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_audit_regressions")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
