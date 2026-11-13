"""Offline startup check of the bundled executable, no credentials or Telegram network calls."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time

root = Path(__file__).resolve().parent
exe = root / 'dist' / 'TelegramOfferScheduler' / 'TelegramOfferScheduler.exe'
with TemporaryDirectory(prefix='telegram-scheduler-smoke-') as directory:
    original = '🔥 Сотрудничество\r\nУсловия\u2028Связаться'
    entities = json.dumps([{'_': 'MessageEntityCustomEmoji', 'offset': 0, 'length': 2,
                           'document_id': 1234567890123456789}])
    settings = {'templates': {'Проверка импорта': original},
                'template_formats': {'Проверка импорта': 'telegram'},
                'template_entities': {'Проверка импорта': entities}}
    (Path(directory) / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
    environment = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    started = time.monotonic()
    completed = subprocess.run([str(exe), '--data-dir', directory, '--smoke-test'],
        env=environment, timeout=40, capture_output=True)
    assert completed.returncode == 0, completed.stderr.decode(errors='replace')
    log = (Path(directory) / 'activity.log').read_text(encoding='utf-8')
    assert 'repeat API: True' in log
    assert (Path(directory) / 'settings.json').exists()
    saved = json.loads((Path(directory) / 'settings.json').read_text(encoding='utf-8'))
    assert saved['templates'] == settings['templates']
    assert saved['template_entities'] == settings['template_entities']
    backups = list((Path(directory) / 'backups').glob('*-before-v4.0.json'))
    assert len(backups) == 1
    backup = json.loads(backups[0].read_text(encoding='utf-8'))
    assert backup['settings']['templates'] == settings['templates']
    assert not list(Path(directory).glob('*.session*'))
    result = {'binary_exit_code': completed.returncode, 'startup_seconds': round(time.monotonic() - started, 2),
              'repeat_api_in_binary': True, 'telegram_session_created': False,
              'live_telegram_test': False, 'imported_template_preserved': True,
              'automatic_settings_backup': True, 'startup_log': log.strip()}
    (root / 'validation-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))
