@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean TelegramOfferScheduler.spec
if errorlevel 1 goto failed
.venv\Scripts\python.exe verify_binary.py
if errorlevel 1 goto failed
echo Build: dist\TelegramOfferScheduler\TelegramOfferScheduler.exe
pause
exit /b 0
:failed
echo Build failed. See the error above.
pause
exit /b 1
