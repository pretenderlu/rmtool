"""One resource inventory for both desktop bundles and their runtime check."""

import json
import os
from pathlib import Path


MANIFESTS = tuple(f"{name}/manifest.json" for name in (
    "translations", "tap-page-turn", "fast-mono-reading", "native-chinese",
    "pinyin-input", "reading-enhancements", "note-enhancements", "weread-app",
    "weread-launcher", "qr-upload", "native-import",
)) + ("assets/published-plugin-features.json",)
DATA_PATHS = ("assets/fonts", "assets/device_frames", "assets/rmtool-icon.ico", *MANIFESTS)


def pyinstaller_data_args(root):
    arguments = []
    for relative in DATA_PATHS:
        source = Path(root) / relative
        if not source.exists():
            raise RuntimeError(f"Missing bundle input: {relative}")
        destination = relative if source.is_dir() else str(Path(relative).parent)
        arguments.extend(("--add-data", str(source) + os.pathsep + destination))
    return arguments


def self_test(root=None):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtGui, QtSvg, QtWidgets
    import _https

    root = Path(root) if root is not None else Path(__file__).resolve().parent
    pyinstaller_data_args(root)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["rmtool-bundle-check"])
    for relative in MANIFESTS:
        document = json.loads((root / relative).read_bytes())
        if not isinstance(document, dict) or not (document.get("schema") == 1 or document.get("schema_version") == 1):
            raise RuntimeError(f"Invalid bundled manifest: {relative}")
    for font in ("NotoSansCJKsc-Regular.otf", "NotoSansCJKsc-Common.otf"):
        identifier = QtGui.QFontDatabase.addApplicationFont(str(root / "assets/fonts" / font))
        if identifier < 0 or not QtGui.QFontDatabase.applicationFontFamilies(identifier):
            raise RuntimeError(f"Cannot load bundled font: {font}")
        QtGui.QFontDatabase.removeApplicationFont(identifier)
    for frame in ("paper-pro", "paper-pro-move", "paper-pure", "remarkable-1", "remarkable-2"):
        if QtGui.QImage(str(root / "assets/device_frames" / (frame + ".png"))).isNull():
            raise RuntimeError(f"Cannot load device frame: {frame}")
    if QtGui.QIcon(str(root / "assets/rmtool-icon.ico")).isNull():
        raise RuntimeError("Cannot load bundled application icon")
    if not QtSvg.QSvgRenderer(b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8"/></svg>').isValid():
        raise RuntimeError("Cannot load Qt SVG support")
    if not _https.ssl_context().get_ca_certs():
        raise RuntimeError("Cannot load bundled CA certificates")
    app.processEvents()


if __name__ == "__main__":
    self_test()
