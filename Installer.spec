from pathlib import Path
a = Analysis(['installer.py'], pathex=[], binaries=[], datas=[('payload.zip', '.')], hiddenimports=[],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['PySide6.QtWebEngineCore', 'PySide6.QtQml'], noarchive=False)
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in {'icuuc.dll', 'icudt78.dll'}]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='TelegramOfferScheduler-4.1-Setup',
          debug=False, strip=False, upx=True, console=False, disable_windowed_traceback=False)
