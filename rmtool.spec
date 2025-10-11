# -*- mode: python ; coding: utf-8 -*-

import pathlib
import sys

block_cipher = None

try:
    base_path = pathlib.Path(__file__).parent
except NameError:  # __file__ may be missing when the spec is exec'd directly
    spec_candidate = pathlib.Path(sys.argv[0]) if sys.argv else None
    if spec_candidate and spec_candidate.suffix == '.spec' and spec_candidate.exists():
        base_path = spec_candidate.resolve().parent
    else:
        base_path = pathlib.Path.cwd()
web_assets = [
    (str(base_path / 'web' / 'dashboard.html'), 'web'),
    (str(base_path / 'web' / 'dashboard.css'), 'web'),
    (str(base_path / 'web' / 'dashboard.js'), 'web'),
]

a = Analysis(
    ['rmtool.py'],
    pathex=[str(base_path.resolve())],
    binaries=[],
    datas=web_assets,
    hiddenimports=['PyQt5.QtWebEngineWidgets', 'PyQt5.QtWebEngineCore', 'PyQt5.QtWebEngine'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='rmtool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
)
