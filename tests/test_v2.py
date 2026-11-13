import asyncio
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from telethon import functions, types, errors, utils
from engine import Engine, Stopped
from storage import Store, save_json
from profiles import Profiles, Vault, load_settings
from planner import make_slots, distance, DAY
from synchronization import reconcile


class PersistenceTests(unittest.TestCase):
    def test_migration_backup_keeps_old_jobs(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'progress.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''CREATE TABLE jobs (chat INTEGER, stamp INTEGER, text TEXT,
                    random_id INTEGER, state TEXT, message_id INTEGER, repeat INTEGER, error TEXT,
                    PRIMARY KEY(chat,stamp));
                    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                    INSERT INTO jobs VALUES(-1,123456,'Old offer',77,'verified',5,86400,NULL);
                    INSERT INTO meta VALUES('account','123');''')
            store = Store(path)
            self.assertEqual(store.jobs(-1)[0]['random_id'], 77)
            self.assertEqual(store.meta('account'), '123')
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 3)
            store.db.close()
            backup = next(Path(folder).glob('*.bak'))
            with closing(sqlite3.connect(backup)) as db:
                self.assertEqual(db.execute('SELECT text FROM jobs').fetchone()[0], 'Old offer')
                self.assertNotIn('template_id', {r[1] for r in db.execute('PRAGMA table_info(jobs)')})
            reopened = Store(path)
            reopened.db.close()
            self.assertEqual(len(list(Path(folder).glob('*.bak'))), 1)

    def test_profiles_keep_legacy_and_isolate_settings(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = {'templates': {'Старый': 'Исходный текст'}, 'api_id': '123'}
            save_json(root / 'settings.json', legacy)
            (root / 'account.session').write_bytes(b'untouched legacy session')
            profiles = Profiles(root)
            second = profiles.add('Другой аккаунт')
            save_json(profiles.path(second) / 'settings.json', {'templates': {'Другой': 'Иной текст'}})
            profiles.select(second)
            reloaded = Profiles(root)
            self.assertEqual(reloaded.data['active'], second)
            self.assertEqual(json.loads((root / 'settings.json').read_text(encoding='utf-8')), legacy)
            self.assertEqual((root / 'account.session').read_bytes(), b'untouched legacy session')
            self.assertIn('Другой', load_settings(reloaded.path(second) / 'settings.json')['templates'])
            profiles.remove(second)
            self.assertTrue((profiles.path(second) / 'settings.json').exists())
            profiles.restore(second)
            self.assertEqual(len(profiles.active_profiles()), 2)

    def test_same_telegram_account_cannot_have_two_profiles(self):
        with TemporaryDirectory() as folder:
            profiles = Profiles(Path(folder))
            other = profiles.add('Other')
            profiles.claim(Path(folder), 900)
            with self.assertRaisesRegex(ValueError, 'уже принадлежит'):
                Profiles(Path(folder)).claim(profiles.path(other), 900)

    def test_duplicate_account_detected_from_legacy_database(self):
        with TemporaryDirectory() as folder:
            profiles = Profiles(Path(folder))
            store = Store(Path(folder) / 'progress.sqlite3')
            store.meta('account', 900)
            store.db.close()
            other = profiles.add('Other')
            with self.assertRaisesRegex(ValueError, 'уже принадлежит'):
                profiles.claim(profiles.path(other), 900)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI test')
    def test_dpapi_roundtrip_and_no_plaintext(self):
        with TemporaryDirectory() as folder:
            vault = Vault(folder)
            value = 'local-test-secret-Это не Telegram-секрет'
            vault.write('session', value)
            self.assertEqual(Vault(folder).read('session'), value)
            self.assertNotIn(value.encode(), (Path(folder) / 'session.dpapi').read_bytes())
            with self.assertRaises(OSError):
                (Path(folder) / 'bad.dpapi').write_bytes(b'not encrypted')
                vault.read('bad')

    def test_gap_packing_recovers_all_available_slots(self):
        now, start = 2_000_000_000, 2_000_000_120
        occupied = [start + 400]
        slots = make_slots(start, now, 960, occupied)
        self.assertEqual(len(slots), 89)
        points = sorted([*slots, *occupied])
        self.assertTrue(all(distance(a, b) >= 960 for i, a in enumerate(points) for b in points[i+1:]))


class Server:
    """Mock with per-chat queues, ID rollover, RPC failures, and lost edit acknowledgments."""
    def __init__(self):
        self.queues = {}
        self.requests = []
        self.read_error_chat = None
        self.lose_edit_ack = False
        self.wait_once = None
        self.user_id = 123

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.users.GetUsersRequest):
            return [NS(id=self.user_id, premium=True, bot=False)]
        chat = utils.get_peer_id(request.peer)
        queue = self.queues.setdefault(chat, [])
        if isinstance(request, functions.messages.GetScheduledHistoryRequest):
            if chat == self.read_error_chat:
                raise errors.ChatAdminRequiredError(request=None)
            return NS(messages=list(queue))
        if isinstance(request, functions.messages.SearchRequest):
            return NS(messages=[])
        if isinstance(request, functions.messages.SendMessageRequest):
            if self.wait_once:
                exc, self.wait_once = self.wait_once, None
                raise exc
            msg = NS(id=len(queue)+1, date=request.schedule_date, message=request.message,
                     schedule_repeat_period=request.schedule_repeat_period, media=None, reply_to=None, entities=request.entities)
            queue.append(msg)
            return NS()
        if isinstance(request, functions.messages.EditMessageRequest):
            current = next(m for m in queue if m.id == request.id)
            current.entities = request.entities
            current.message = request.message
            current.schedule_repeat_period = request.schedule_repeat_period
            current.date = request.schedule_date
            if self.lose_edit_ack:
                self.lose_edit_ack = False
                raise ConnectionError('simulated lost acknowledgment')
            return NS()
        if isinstance(request, functions.messages.DeleteScheduledMessagesRequest):
            self.queues[chat] = [m for m in queue if m.id not in request.id]
            return NS()
        raise AssertionError(type(request))

    async def disconnect(self):
        pass


class UpgradeEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.engine = Engine(self.path, lambda x: None, threading.Event())
        self.engine.client = Server()
        self.engine.sleep = AsyncMock()
        self.engine.pause = self.engine.create_pause = 0
        self.chats = [
            {'id': -1, 'title': 'One', 'allowed': True, 'manual': 15, 'rules': 'Permission', 'template_id': 'A'},
            {'id': -2, 'title': 'Two', 'allowed': True, 'manual': 15, 'rules': 'Permission', 'template_id': 'B'}]
        self.engine.entities = {-1: types.InputPeerChat(1), -2: types.InputPeerChat(2)}

    async def asyncTearDown(self):
        self.engine.store.db.close()
        self.temp.cleanup()

    async def create(self, chats=None, count=1):
        chats = chats or self.chats
        await self.engine.prepare(chats, {'A': 'Offer A', 'B': 'Offer B'}, int(time.time())+600, DAY, count, 1)
        await self.engine.execute()

    async def test_distinct_chat_templates_and_verified_repeat(self):
        await self.create()
        self.assertEqual(self.engine.client.queues[-1][0].message, 'Offer A')
        self.assertEqual(self.engine.client.queues[-2][0].message, 'Offer B')
        self.assertEqual(self.engine.store.jobs(-2)[0]['template_id'], 'B')
        self.assertTrue(all(j['state'] == 'verified' for c in (-1,-2) for j in self.engine.store.jobs(c)))

    async def test_creation_across_chats_follows_earliest_dates(self):
        await self.create(count=3)
        sends = [r for r in self.engine.client.requests if isinstance(r, functions.messages.SendMessageRequest)]
        dates = [r.schedule_date for r in sends]
        self.assertEqual(dates, sorted(dates))
        self.assertNotEqual(utils.get_peer_id(sends[0].peer), utils.get_peer_id(sends[1].peer))

    async def test_id_rollover_and_template_update(self):
        await self.create()
        msg = self.engine.client.queues[-1][0]
        msg.id = 120
        msg.date += timedelta(days=4, seconds=20)
        preview = await self.engine.prepare_updates(self.chats, {'A': 'Updated A', 'B': 'Offer B'})
        self.assertEqual(len(preview['changes']), 1)
        self.assertEqual(preview['changes'][0]['row']['id'], 120)
        self.assertEqual(msg.message, 'Offer A')
        await self.engine.apply_prepared_changes()
        self.assertEqual(msg.message, 'Updated A')
        self.assertEqual(msg.schedule_repeat_period, DAY)
        self.assertEqual(self.engine.client.queues[-2][0].message, 'Offer B')
        self.assertFalse(self.engine.store.operations())

    async def test_foreign_similar_messages_not_automatically_bound(self):
        await self.create([self.chats[0]])
        foreign = NS(**vars(self.engine.client.queues[-1][0]))
        foreign.id = 90
        self.engine.client.queues[-1].append(foreign)
        preview = await self.engine.prepare_updates([self.chats[0]], {'A': 'New'})
        self.assertEqual(preview['changes'], [])
        self.assertEqual(self.engine.store.jobs(-1)[0]['sync_state'], 'неоднозначно')

    async def test_drift_outside_tolerance_requires_binding(self):
        await self.create([self.chats[0]])
        msg = self.engine.client.queues[-1][0]
        msg.id = 99
        msg.date += timedelta(days=2, seconds=300)
        rows = (await self.engine.queues([self.chats[0]]))['queue']
        self.assertIsNone(rows[0]['template_id'])
        self.assertEqual((await self.engine.prepare_updates([self.chats[0]], {'A': 'New'}))['changes'], [])

    async def test_lost_edit_ack_recovered_after_restart_without_second_edit(self):
        await self.create([self.chats[0]])
        await self.engine.prepare_updates([self.chats[0]], {'A': 'Updated'})
        self.engine.client.lose_edit_ack = True
        with self.assertRaises(ConnectionError):
            await self.engine.apply_prepared_changes()
        server = self.engine.client
        self.engine.store.db.close()
        self.engine = Engine(self.path, lambda x: None, threading.Event())
        self.engine.client, self.engine.entities = server, {-1: types.InputPeerChat(1)}
        self.engine.sleep = AsyncMock()
        pending = await self.engine.pending_changes([self.chats[0]])
        self.assertEqual(len(pending['changes']), 1)
        await self.engine.apply_prepared_changes()
        edits = [r for r in server.requests if isinstance(r, functions.messages.EditMessageRequest)]
        self.assertEqual(len(edits), 1)
        self.assertFalse(self.engine.store.operations())

    async def test_change_results_report_errors_per_chat(self):
        await self.create()
        await self.engine.prepare_updates(self.chats, {'A': 'New A', 'B': 'New B'})
        self.engine.client.read_error_chat = -1
        result = await self.engine.apply_prepared_changes()
        self.assertTrue(any(r.get('error') for r in result['chat_reports']))
        self.assertEqual(self.engine.client.queues[-2][0].message, 'New B')
        self.assertEqual(self.engine.client.queues[-1][0].message, 'Offer A')
        self.assertEqual(len(self.engine.store.operations()), 1)

    async def test_test_mode_one_change_only(self):
        self.engine.test_mode = True
        with self.assertRaisesRegex(ValueError, 'Тестовый режим'):
            await self.create()
        self.assertFalse(any(isinstance(r, functions.messages.SendMessageRequest) for r in self.engine.client.requests))
        await self.create([self.chats[0]], 90)
        self.assertEqual(len(self.engine.client.queues[-1]), 1)

    async def test_wait_cancel_keeps_unsent_pending_and_resumes(self):
        self.engine.sleep = Engine.sleep.__get__(self.engine)
        self.engine.client.wait_once = errors.SlowModeWaitError(request=None, capture=2)
        self.engine.store.add_plan([{'chat': -1, 'slots': [int(time.time())+600], 'text': 'Offer A', 'repeat': DAY}])
        task = asyncio.create_task(self.engine.resume([self.chats[0]]))
        await asyncio.sleep(.05)
        self.engine.cancel.set()
        with self.assertRaises(Stopped):
            await task
        self.assertEqual(self.engine.store.jobs(-1)[0]['state'], 'pending')
        self.engine.cancel.clear()
        self.engine.sleep = AsyncMock()  # Skip elapsed wait in the mock test only.
        await self.engine.resume([self.chats[0]])
        self.assertEqual(len(self.engine.client.queues[-1]), 1)

    async def test_change_repeat_on_and_off(self):
        await self.create([self.chats[0]])
        rows = (await self.engine.queues([self.chats[0]]))['queue']
        await self.engine.mutate(rows, 'repeat_once')
        self.assertEqual(self.engine.client.queues[-1][0].schedule_repeat_period, 0)
        rows = (await self.engine.queues([self.chats[0]]))['queue']
        await self.engine.mutate(rows, 'repeat_daily')
        self.assertEqual(self.engine.client.queues[-1][0].schedule_repeat_period, DAY)

    async def test_read_only_binding_legacy_messages(self):
        now = int(time.time())+600
        self.engine.client.queues[-1] = [NS(id=1, date=datetime.fromtimestamp(now, timezone.utc),
            message='External message', schedule_repeat_period=DAY, media=None, reply_to=None)]
        rows = (await self.engine.queues([self.chats[0]]))['queue']
        await self.engine.bind_rows(rows, 'A')
        self.assertEqual(self.engine.store.jobs(-1)[0]['template_id'], 'A')
        self.assertFalse(any(isinstance(r, functions.messages.EditMessageRequest) for r in self.engine.client.requests))

    async def test_queue_refill_preserves_existing_messages(self):
        await self.create([self.chats[0]])
        old = self.engine.client.queues[-1][0]
        await self.engine.prepare([self.chats[0]], {'A': 'Offer A'}, int(time.time())+600, DAY, 90, 1)
        self.assertEqual(sum(len(p['slots']) for p in self.engine.plan), 89)
        self.assertEqual(self.engine.client.queues[-1], [old])

    async def test_full_queue_after_successful_single_test(self):
        self.engine.test_mode = True
        await self.create([self.chats[0]], 100)
        old = self.engine.client.queues[-1][0]
        self.assertEqual(len(self.engine.client.queues[-1]), 1)
        self.engine.test_mode = False
        await self.engine.prepare([self.chats[0]], {'A': 'Offer A'}, int(time.time())+600, DAY, 100, 1)
        self.assertEqual(len(self.engine.plan[0]['slots']), 89)
        await self.engine.execute()
        queue = self.engine.client.queues[-1]
        self.assertEqual(len(queue), 90)
        self.assertIs(queue[0], old)
        self.assertTrue(all(m.schedule_repeat_period == DAY for m in queue))
        stamps = sorted(int(m.date.timestamp()) for m in queue)
        self.assertGreaterEqual(min(b-a for a,b in zip(stamps, stamps[1:]+[stamps[0]+DAY])), 960)
        await self.engine.resume([self.chats[0]])
        self.assertEqual(len(self.engine.client.queues[-1]), 90)


class AuthClient:
    def __init__(self, *args, **kwargs):
        self.authorized = False
        self.api_hash = args[2]
        self.session_argument = args[0]
        self.code_requests = 0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        self.code_requests += 1
        return NS(phone_code_hash='local-only')

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is None:
            if code != '12345':
                raise errors.PhoneCodeInvalidError(request=None)
            raise errors.SessionPasswordNeededError(request=None)
        if password != 'local-test-password':
            raise errors.PasswordHashInvalidError(request=None)
        self.authorized = True

    async def __call__(self, request):
        return [NS(id=123, premium=True, bot=False)]


class AuthTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    async def test_session_saved_encrypted_and_restored(self):
        from telethon.sessions import MemorySession, StringSession
        from telethon.crypto import AuthKey
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda x: None, threading.Event())
            with closing(engine.store.db):
                session = MemorySession()
                session.set_dc(2, '149.154.167.50', 443)
                session.auth_key = AuthKey(bytes(range(256)))
                engine.client = NS(session=session)
                engine.save_session()
                saved = Vault(folder).read('session')
                restored = StringSession(saved)
                self.assertEqual(restored.auth_key.key, session.auth_key.key)
                self.assertNotIn(saved.encode(), (Path(folder) / 'session.dpapi').read_bytes())
                self.assertFalse((Path(folder) / 'account.session').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    async def test_phone_code_2fa_and_protected_api_setting(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda x: None, threading.Event())
            engine.sleep = AsyncMock()
            with closing(engine.store.db), patch('engine.TelegramClient', AuthClient):
                result = await engine.connect('123', 'a'*32, '+70000000000', 1)
                self.assertEqual(result['auth'], 'code')
                with self.assertRaises(errors.PhoneCodeInvalidError):
                    await engine.login('wrong')
                result = await engine.login('12345')
                self.assertEqual(result['auth'], 'password')
                result = await engine.login(password='local-test-password')
                self.assertEqual(result['auth'], 'ready')
                self.assertEqual(Vault(folder).read('api_hash'), 'a'*32)
                await engine.connect('123', '', '+70000000000', 1)
                self.assertEqual(engine.client.api_hash, 'a'*32)
            engine.store.db.close()
