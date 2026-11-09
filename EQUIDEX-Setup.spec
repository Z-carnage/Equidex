# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['setup_wizard.py'],
    pathex=[],
    binaries=[],
    datas=[('dist/EQUIDEX.exe', '.'), ('equidex-light.ico', '.'), ('equidex_hdr.png', '.'), ('SETUP.md', '.'), ('TERMS.md', '.'), ('THIRD-PARTY-LICENSES.txt', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='EQUIDEX-Setup',
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
