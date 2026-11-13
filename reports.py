"""Read-only local overview and CSV audit export; never contacts Telegram."""
import csv
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from contextlib import closing
from pathlib import Path
from profiles import Profiles, load_settings


def overview(root):
    registry = Profiles(root)
    rows = []
    for profile in registry.active_profiles():
        path = registry.path(profile['id'])
        row = dict(name=profile['name'], chats=0, queued=None, errors=0, next=None, checked=None, note='')
        try:
            row['chats'] = len(load_settings(path / 'settings.json')['chats'])
            cache = path / 'queue_snapshot.json'
            if cache.exists():
                snapshots = list(json.loads(cache.read_text(encoding='utf-8')).values())
                messages = [r for item in snapshots for r in item['rows']]
                row['queued'] = len(messages)
                row['checked'] = min((item['checked'] for item in snapshots), default=None)
                row['next'] = min((r['stamp'] for r in messages), default=None)
                if row['next'] and row['next'] <= time.time():
                    row['note'] = 'Дата по снимку уже прошла; обновите очередь этого аккаунта'
            database = path / 'progress.sqlite3'
            if database.exists():
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                    row['errors'] = db.execute("SELECT COUNT(*) FROM operations WHERE state NOT IN ('done','cancelled')").fetchone()[0]
                    row['errors'] += db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('sending','unverified') OR error IS NOT NULL").fetchone()[0]
        except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
            row['note'] = 'Не удалось прочитать часть локальных данных'
        rows.append(row)
    return rows


def export_report(directory, target):
    database = Path(directory) / 'progress.sqlite3'
    records = []
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute('SELECT * FROM jobs ORDER BY chat,stamp'):
                records.append(['создание/текущее состояние', row['chat'], row['message_id'], row['stamp'], row['state'], row['template_id'], row['text'], row['error'] or ''])
            for operation in db.execute('SELECT * FROM operations ORDER BY id'):
                payload = json.loads(operation['payload'])
                row = payload.get('row', {})
                records.append([payload.get('action'), row.get('chat'), row.get('id'), payload.get('target_stamp', row.get('stamp')), operation['state'], row.get('template_id'), payload.get('text', row.get('text')), operation['error'] or ''])
    def safe(value):
        text = '' if value is None else str(value)
        return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text
    with Path(target).open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file, delimiter=';')
        writer.writerow(['Операция', 'Чат', 'ID сообщения', 'Время слота (Москва UTC+3)', 'Состояние', 'Шаблон', 'Текст', 'Ошибка'])
        for row in records:
            if row[3]:
                row[3] = datetime.fromtimestamp(row[3], timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M:%S')
        writer.writerows([[safe(v) for v in row] for row in records])
    return len(records)
