# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files


qtawesome_datas = collect_data_files('qtawesome')


a = Analysis(
    ['bot2_v5_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('tessdata', 'tessdata'),
        ('logo.png', '.'),
        ('img32.png', '.'),
        ('img255.png', '.'),
        ('launcher_windows.ico', '.'),
    ] + qtawesome_datas,
    hiddenimports=['pytesseract', 'qtawesome'],
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
    name='Fishing Bot',
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
    version='file_version_info.txt',
    icon=['launcher_windows.ico'],
)
