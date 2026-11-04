"""Invoke PyInstaller with the shared Windows/macOS resource inventory."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _bundle_resources import pyinstaller_data_args


def main():
    import PyInstaller.__main__
    PyInstaller.__main__.run([*sys.argv[1:], *pyinstaller_data_args(ROOT)])


if __name__ == "__main__":
    main()
