# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtQml'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TelegramOfferScheduler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='TelegramOfferScheduler',
)
app = BUNDLE(
    coll,
    name='TelegramOfferScheduler.app',
    icon=None,
    bundle_identifier='com.telegramofferscheduler.desktop',
    info_plist={
        'CFBundleDisplayName': 'Telegram Offer Scheduler',
        'CFBundleShortVersionString': '4.3',
        'CFBundleVersion': '4.3',
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
    },
)
