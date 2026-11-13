"""Check frozen wizard and installed application only in a temporary directory."""
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

root = Path(__file__).resolve().parent
setup = root / 'dist' / 'TelegramOfferScheduler-4.2-Setup.exe'
with TemporaryDirectory(prefix='tgos-install-test-') as folder:
    temporary = Path(folder)
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([str(setup), '--smoke-test'], env=env, timeout=60, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    base = temporary / 'install'
    (base / '3.9').mkdir(parents=True)
    (base / '3.9' / 'keep.txt').write_text('preserved')
    result = subprocess.run([str(setup), '--verify-package', str(base)], env=env, timeout=120, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert (base / '3.9' / 'keep.txt').read_text() == 'preserved'
    application = base / '4.2' / 'TelegramOfferScheduler.exe'
    assert application.exists()
    result = subprocess.run([str(application), '--data-dir', str(temporary/'data'), '--smoke-test'],
                            env=env, timeout=60, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert not list((temporary/'data').glob('*.session*'))
    report = dict(wizard_startup=True, temporary_installation=True, previous_version_preserved=True,
                  installed_application_startup=True, live_telegram_test=False, desktop_shortcut_created=False)
    (root/'installer-validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
