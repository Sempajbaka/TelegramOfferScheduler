import json
import os
import secrets
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


def data_dir(os_name=None, platform=None, home=None, environ=None):
    """Return the application data directory, with injectable platform values for tests."""
    os_name = os.name if os_name is None else os_name
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    if os_name == 'nt':
        path = Path(environ.get('LOCALAPPDATA', str(home))) / 'TelegramOfferScheduler'
    elif platform == 'darwin':
        path = home / 'Library' / 'Application Support' / 'TelegramOfferScheduler'
    else:
        path = Path(environ.get('XDG_DATA_HOME', str(home / '.local' / 'share'))) / 'TelegramOfferScheduler'
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        existing = self.db.execute("SELECT name FROM sqlite_master WHERE name='jobs'").fetchone()
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > 3:
            self.db.close()
            raise ValueError('База создана более новой версией приложения')
        if existing and version < 3:
            backup = path.with_name(path.name + f'.before-v3-{time.time_ns()}.bak')
            with closing(sqlite3.connect(backup)) as destination:
                self.db.backup(destination)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS jobs (
            chat INTEGER, stamp INTEGER, text TEXT, random_id INTEGER,
            state TEXT, message_id INTEGER, repeat INTEGER, error TEXT,
            PRIMARY KEY(chat, stamp));
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.db.execute('BEGIN IMMEDIATE')
        with self.db:
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}
            for name, kind in [('template_id', 'TEXT'), ('server_stamp', 'INTEGER'),
                               ('pending_text', 'TEXT'), ('sync_state', 'TEXT'), ('entities', 'TEXT'), ('pending_entities', 'TEXT')]:
                if name not in columns:
                    self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
            self.db.execute('''CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
                error TEXT, created INTEGER NOT NULL)''')
            self.db.execute('PRAGMA user_version=3')

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def jobs(self, chat):
        return [dict(r) for r in self.db.execute('SELECT * FROM jobs WHERE chat=? ORDER BY stamp', (chat,))]

    def add_plan(self, plan):
        with self.db:
            for p in plan:
                for stamp in p['slots']:
                    content = p.get('slot_contents', {}).get(str(stamp), p)
                    self.db.execute('''INSERT OR IGNORE INTO jobs
                                    (chat,stamp,text,random_id,state,message_id,repeat,error,template_id,entities)
                                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                                    (p['chat'], stamp, content['text'], secrets.randbits(63) or 1,
                                     'pending', None, p['repeat'], None, content.get('template_id'), content.get('entities', '[]')))

    def update(self, chat, stamp, **fields):
        assert set(fields) <= {'state', 'message_id', 'text', 'error', 'repeat',
                              'template_id', 'server_stamp', 'pending_text', 'sync_state', 'entities', 'pending_entities'}
        with self.db:
            self.db.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in fields) +
                            ' WHERE chat=? AND stamp=?', (*fields.values(), chat, stamp))

    def clear(self, chat):
        with self.db:
            self.db.execute('DELETE FROM jobs WHERE chat=?', (chat,))

    def new_operation(self, payload):
        with self.db:
            cursor = self.db.execute('INSERT INTO operations(payload,state,created) VALUES (?,?,?)',
                                     (json.dumps(payload, ensure_ascii=False), 'pending', int(time.time())))
        return cursor.lastrowid

    def operation_state(self, ident, state, error=None):
        with self.db:
            self.db.execute('UPDATE operations SET state=?,error=? WHERE id=?', (state, error, ident))

    def operations(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM operations WHERE state NOT IN ('done','cancelled') ORDER BY id")]

    def pending_creation(self, chat):
        return any(j['state'] in ('pending', 'sending', 'unverified') for j in self.jobs(chat))
