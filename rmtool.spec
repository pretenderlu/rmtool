# -*- mode: python ; coding: utf-8 -*-

import os
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

icon_env = os.environ.get('RMTOOL_BUILD_ICON', '').strip()
build_icon = None
if icon_env:
    icon_candidate = pathlib.Path(icon_env).expanduser()
    if not icon_candidate.is_absolute():
        icon_candidate = (base_path / icon_candidate).resolve()
    if icon_candidate.exists():
        build_icon = str(icon_candidate)

web_assets = [
    (str(base_path / 'web' / 'dashboard.html'), 'web'),
    (str(base_path / 'web' / 'dashboard.css'), 'web'),
    (str(base_path / 'web' / 'dashboard.js'), 'web'),
    (str(base_path / 'rmrl' / '__init__.py'), 'rmrl'),
    (str(base_path / 'rmrl' / '__main__.py'), 'rmrl'),
]

a = Analysis(
    ['rmtool.py'],
    pathex=[str(base_path.resolve())],
    binaries=[],
    datas=web_assets,
    hiddenimports=['PyQt5.QtWebEngineWidgets', 'PyQt5.QtWebEngineCore', 'PyQt5.QtWebEngine', 'rmscene', 'rmscene.scene_items', '_styles', '_ssh', '_tab_connection', '_tab_documents', '_tab_wallpaper', '_tab_toolbox'],
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
    icon=build_icon,
)
