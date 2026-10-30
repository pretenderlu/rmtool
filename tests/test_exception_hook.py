"""Uncaught slot exceptions must be reported, not abort the process."""

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, sys.argv[1])
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt5 import QtCore, QtWidgets
    import rmtool, _dialogs
    app = QtWidgets.QApplication([])
    if sys.argv[2] == "hook":
        rmtool._install_exception_hook(app)
    shown = []
    _dialogs._show_message = lambda *args, **kwargs: shown.append(args[2]) or 0

    def boom():
        raise RuntimeError("Could not save configuration")

    QtCore.QTimer.singleShot(0, boom)
    QtCore.QTimer.singleShot(50, app.quit)
    app.exec_()
    print("ALIVE", len(shown), shown[0] if shown else "")
    """
)


class ExceptionHookTests(unittest.TestCase):
    def run_app(self, mode):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        return subprocess.run(
            [sys.executable, "-c", SCRIPT, str(ROOT), mode],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(ROOT),
        )

    def test_slot_exception_is_reported_and_app_keeps_running(self):
        result = self.run_app("hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALIVE 1", result.stdout)
        self.assertIn("界面操作出错", result.stdout)

    def test_without_hook_pyqt_aborts(self):
        # Documents why the hook exists; PyQt5 >= 5.5 calls qFatal().
        result = self.run_app("nohook")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ALIVE", result.stdout)


if __name__ == "__main__":
    unittest.main()
