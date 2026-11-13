import json
import os
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from contextlib import closing
from telethon import types, functions
from application_settings import ApplicationSettings, remember_versions
from profiles import Profiles, Vault
from storage import Store, save_json
from planner import DAY, make_slots, interval_seconds
from formatting import parse_offer, encode, decode
from diagnostics import diagnose
from engine import Engine
from test_v2 import AuthClient, Server


class V3Tests(unittest.TestCase):
    def test_v2_database_migration_keeps_pending_work_and_backup(self):
        import sqlite3
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'progress.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''CREATE TABLE jobs(chat INTEGER, stamp INTEGER, text TEXT, random_id INTEGER,
                    state TEXT, message_id INTEGER, repeat INTEGER, error TEXT, template_id TEXT,
                    server_stamp INTEGER, pending_text TEXT, sync_state TEXT, PRIMARY KEY(chat,stamp));
                    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE operations(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL,error TEXT,created INTEGER NOT NULL);
                    INSERT INTO jobs VALUES(-1,123,'text',99,'sending',NULL,86400,NULL,'A',NULL,NULL,NULL);
                    INSERT INTO operations VALUES(1,'{}','pending',NULL,100);
                    PRAGMA user_version=2;''')
            store = Store(path)
            with closing(store.db):
                self.assertEqual(store.jobs(-1)[0]['random_id'], 99)
                self.assertEqual(store.jobs(-1)[0]['state'], 'sending')
                self.assertEqual(len(store.operations()), 1)
                self.assertIsNone(store.jobs(-1)[0]['entities'])
            with closing(sqlite3.connect(next(Path(folder).glob('*.bak')))) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI migration')
    def test_global_migration_preserves_originals(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            Profiles(root)
            save_json(root / 'settings.json', {'api_id': '456', 'templates': {'old': 'unchanged'}})
            Vault(root).write('api_hash', 'b' * 32)
            before = (root / 'settings.json').read_bytes()
            app = ApplicationSettings(root)
            app.migrate()
            self.assertEqual(app.credentials(), {'api_id': 456, 'api_hash': 'b' * 32})
            self.assertEqual((root / 'settings.json').read_bytes(), before)
            self.assertNotIn(b'b' * 32, (root / 'application/api.dpapi').read_bytes())
            app.migrate()
            self.assertEqual(Vault(root).read('api_hash'), 'b' * 32)

    def test_version_history_preserves_plain_and_format_only_changes(self):
        config = {'templates': {'A': 'offer'}}
        remember_versions(config)
        remember_versions(config)
        config['template_formats'] = {'A': 'html'}
        remember_versions(config)
        self.assertEqual(len(config['template_versions']['A']), 2)
        self.assertEqual(config['template_versions']['A'][0]['format'], 'plain')

    def test_cycles_16_31_61_minutes_with_integer_wrap(self):
        for minutes, expected in [(16, 90), (31, 46), (61, 23)]:
            slots = make_slots(120, 0, minutes * 60, limit=100)
            self.assertEqual(len(slots), expected)
            gaps = [b-a for a,b in zip(slots, slots[1:])] + [slots[0]+DAY-slots[-1]]
            self.assertGreaterEqual(min(gaps), minutes*60)
            self.assertEqual(sum(gaps), DAY)
            self.assertTrue(all(isinstance(x, int) for x in slots))

    def test_margin_default_and_explicit(self):
        self.assertEqual(interval_seconds(900, 15), 960)
        self.assertEqual(interval_seconds(900, 15, 75), 975)
        with self.assertRaises(ValueError):
            interval_seconds(900, 15, 59)

    def test_html_utf16_entities_and_plain_compatibility(self):
        text, entities = parse_offer({'text': '😀 <b>Оффер</b>', 'format': 'html'})
        self.assertEqual(text, '😀 Оффер')
        self.assertEqual(entities[0].offset, 3)
        self.assertEqual(encode(decode(encode(entities))), encode(entities))
        self.assertEqual(parse_offer('<b>literal</b>'), ('<b>literal</b>', []))

    def test_diagnostics_drift_missing_and_conflicts(self):
        origin = 100000
        jobs = [dict(stamp=origin, server_stamp=origin, text='A', repeat=DAY, state='verified'),
                dict(stamp=origin+960, text='B', repeat=DAY, state='verified')]
        msg = NS(id=90, date=datetime.fromtimestamp(origin+DAY+80, timezone.utc), message='A', schedule_repeat_period=DAY)
        other = NS(id=91, date=datetime.fromtimestamp(origin+DAY+960, timezone.utc), message='foreign', schedule_repeat_period=DAY)
        report = diagnose(jobs, [msg, other], 900, 15)
        self.assertEqual(report['missing_or_changed'], 1)
        self.assertEqual(report['extra'], 1)
        self.assertEqual(report['conflicts'], 1)
        self.assertIn('+80 с', '\n'.join(report['lines']))
        self.assertIn('НЕ ПОДТВЕРЖДЕНЫ', '\n'.join(report['lines']))

    def test_purge_only_removed_profile_and_keep_global_other(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            profiles = Profiles(root)
            second = profiles.add('Second')
            save_json(root / 'settings.json', {'old': True})
            other = profiles.path(second)
            (other / 'settings.json').write_text('{}')
            (root / 'application').mkdir()
            (root / 'application/api.dpapi').write_bytes(b'preserve')
            with self.assertRaises(ValueError):
                profiles.purge('legacy')
            profiles.select(second)
            profiles.remove('legacy')
            profiles.purge('legacy')
            self.assertFalse((root / 'settings.json').exists())
            self.assertTrue((other / 'settings.json').exists())
            self.assertEqual((root / 'application/api.dpapi').read_bytes(), b'preserve')


class V3AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_accounts_share_credentials_and_restore_without_code(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            profiles = Profiles(root)
            second = profiles.add('Second')
            ApplicationSettings(root).save_credentials(456, 'c'*32)
            instances = []
            class Client(AuthClient):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.authorized = True
                    self.identity = len(instances) + 10
                    self.api_id = args[1]
                    instances.append(self)
                async def __call__(self, request):
                    return [NS(id=self.identity, premium=True, bot=False)]
            with patch('engine.TelegramClient', Client):
                for path in [root, profiles.path(second)]:
                    engine = Engine(path, lambda _: None, threading.Event(), root)
                    engine.sleep = AsyncMock()
                    with closing(engine.store.db):
                        result = await engine.connect('', '', '', 1, True)
                        self.assertEqual(result['auth'], 'ready')
                        self.assertEqual(engine.client.code_requests, 0)
                        self.assertEqual(engine.client.api_id, 456)
                        self.assertEqual(engine.client.api_hash, 'c'*32)
            self.assertNotEqual(instances[0].session_argument, instances[1].session_argument)
            self.assertFalse((profiles.path(second) / 'api_hash.dpapi').exists())

    async def test_expired_restore_does_not_request_code(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            Profiles(root)
            ApplicationSettings(root).save_credentials(123, 'a'*32)
            engine = Engine(root, lambda _: None, threading.Event(), root)
            with closing(engine.store.db), patch('engine.TelegramClient', AuthClient):
                self.assertEqual(await engine.connect('', '', '', 1, True), {'session_expired': True})
                self.assertEqual(engine.client.code_requests, 0)

    async def test_html_create_and_format_only_edit_verified(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            engine.client = Server()
            engine.sleep = AsyncMock()
            engine.entities = {-1: types.InputPeerChat(1)}
            chats = [{'id': -1, 'title': 'Test', 'allowed': True, 'manual': 15, 'rules': 'yes', 'template_id': 'A'}]
            with closing(engine.store.db):
                await engine.prepare(chats, {'A': {'text': '<b>offer</b>', 'format': 'html'}}, int(time.time())+600, DAY, 1, 1)
                await engine.execute()
                msg = engine.client.queues[-1][0]
                self.assertIsInstance(msg.entities[0], types.MessageEntityBold)
                msg.id = 77
                msg.date += timedelta(days=1, seconds=7)
                result = await engine.prepare_updates(chats, {'A': {'text': '<i>offer</i>', 'format': 'html'}})
                self.assertEqual(len(result['changes']), 1)
                await engine.apply_prepared_changes()
                self.assertIsInstance(msg.entities[0], types.MessageEntityItalic)
                self.assertEqual(msg.schedule_repeat_period, DAY)
                self.assertEqual(engine.store.jobs(-1)[0]['state'], 'verified')

    async def test_delivery_events_deduplicate_and_require_identity(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            update = types.UpdateDeleteScheduledMessages(peer=types.PeerChat(1), messages=[4], sent_messages=[99])
            with closing(engine.store.db):
                await engine.schedule_update(update)
                self.assertIsNone(engine.store.meta('delivery:-1:99'))
                engine.identity_verified = True
                await engine.schedule_update(update)
                await engine.schedule_update(update)
                self.assertEqual(engine.store.db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'delivery:%'").fetchone()[0], 1)
                self.assertEqual(json.loads(engine.store.meta('delivery:-1:99'))['scheduled_id'], 4)
