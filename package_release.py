"""Create clean release archives from explicit source allowlists; never include user data."""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import shutil
import sys
import zipfile

root = Path(__file__).resolve().parent
out = root.parent / 'outputs' / 'telegram-scheduler-v4.2'
out.mkdir(parents=True, exist_ok=True)
binary = root / 'dist' / 'TelegramOfferScheduler'
assert (binary / 'TelegramOfferScheduler.exe').is_file(), 'Build the application first'

licenses = root / 'licenses'
licenses.mkdir(exist_ok=True)
for package in ['Telethon', 'PySide6-Essentials', 'shiboken6', 'pyaes', 'rsa', 'pyasn1', 'qrcode', 'pyinstaller']:
    dist = importlib.metadata.distribution(package)
    for rel in dist.files or []:
        if '.dist-info' in str(rel) and (any(s in str(rel).lower() for s in ['license', 'copying', 'notice']) or rel.name == 'METADATA'):
            source = Path(dist.locate_file(rel))
            if source.is_file():
                target = licenses / package / Path(*rel.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.exists():
    shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')

docs = ['README.md', 'GUIDE_V3.md', 'AUDIT.md', 'REAL_TELEGRAM_TEST.md', 'VALIDATION.md', 'THIRD_PARTY_NOTICES.md']
for doc in docs:
    shutil.copy2(root / doc, binary / doc)
shutil.copytree(licenses, binary / 'licenses', dirs_exist_ok=True)

source_files = [root / name for name in [
    'campaigns.py', 'reports.py', 'dialog_tools.py', 'installer.py', 'Installer.spec', 'verify_installer.py', 'queue_tools.py', 'backups.py', 'desktop_shortcut.py', 'app.py', 'engine.py', 'planner.py', 'storage.py', 'profiles.py', 'synchronization.py', 'application_settings.py', 'formatting.py', 'diagnostics.py', 'requirements.txt',
    'requirements-build.txt', 'requirements-lock.txt', 'run.cmd', 'build.cmd',
    'package_release.py', 'verify_binary.py', 'TelegramOfferScheduler.spec', 'TelegramOfferScheduler-macOS.spec',
    '.gitignore', '.github/workflows/build-release.yml', *docs]]
source_files += list((root / 'tests').glob('*.py'))
source_files += list((root / 'screenshots').glob('*.png'))
source_files += [p for p in licenses.rglob('*') if p.is_file()]
source_files += [root / 'validation-results.json']

source_zip = out / 'TelegramOfferScheduler-v4.2-Source.zip'
with zipfile.ZipFile(source_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in source_files:
        archive.write(path, 'telegram-scheduler/' + path.relative_to(root).as_posix())

binary_zip = out / 'TelegramOfferScheduler-v4.2-Windows-x64.zip'
with zipfile.ZipFile(binary_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for path in binary.rglob('*'):
        if path.is_file():
            archive.write(path, 'TelegramOfferScheduler/' + path.relative_to(binary).as_posix())

checksums = {}
for path in (source_zip, binary_zip):
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert not any(n.endswith(('.session', '.sqlite3', '.dpapi', '.keychain', '.bak', 'settings.json', 'profiles.json')) for n in archive.namelist())
    checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f'{path.name}: {path.stat().st_size:,} bytes; SHA256 {checksums[path.name]}')
(out / 'SHA256SUMS.txt').write_text('\n'.join(f'{v}  {k}' for k, v in checksums.items()) + '\n', encoding='utf-8')
shutil.copy2(root / 'README.md', out / 'README.md')
shutil.copy2(root / 'VALIDATION.md', out / 'VALIDATION.md')
shutil.copy2(root / 'GUIDE_V3.md', out / 'GUIDE_V3.md')
shutil.copy2(root / 'AUDIT.md', out / 'AUDIT.md')

shutil.copy2(root / 'REAL_TELEGRAM_TEST.md', out / 'REAL_TELEGRAM_TEST.md')
