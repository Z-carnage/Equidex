# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from pathlib import Path
import ssl

datas = [('executive-oge-index.json', '.'), ('govcon-recipients.json', '.'),('Equidex.html', '.'), ('equidex-inconsolata-dark.png', '.'), ('equidex-inconsolata-light.png', '.'), ('equidex-light.ico', '.'), ('equidex-dark.ico', '.'), ('THIRD-PARTY-LICENSES.txt', '.')]
# This managed Python runtime loads its certificate policy beside ssl.py.
# Preserve that policy in frozen builds; stock Python does not need this file.
ssl_policy = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
if ssl_policy.is_file():
    datas.append((str(ssl_policy), '.'))
binaries = []
hiddenimports = []
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['cefpython3', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qtpy', 'cryptography', 'webview.platforms.cef', 'webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.android', 'webview.platforms.cocoa',
              # pypdf imports Pillow only for image extraction (guarded by try/except); the app reads PDF text only.
              # Without these excludes the build pulls in Pillow + NumPy (~15 MB packed) via PIL._typing -> numpy.typing.
              'PIL', 'numpy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='EQUIDEX',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['equidex-light.ico'],
)
