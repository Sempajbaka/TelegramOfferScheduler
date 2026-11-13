import json
import threading
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telethon import types, functions
from campaigns import within_window
from planner import make_slots, distance
from engine import Engine
from profiles import Profiles
from storage import save_json
from reports import overview, export_report
from installer import install_archive, VERSION
from test_v2 import Server


class WindowsTests(unittest.TestCase):
    def test_daytime_maximum_and_wrap(self):
        chat = {'window_start': 9*60, 'window_end': 21*60}
        now = 86400*20000
        slots = make_slots(now+60, now, 960, allowed=lambda t: within_window(t, chat))
        self.assertEqual(len(slots), 45)
        self.assertTrue(all(within_window(t, chat) for t in slots))
        self.assertTrue(all(distance(a,b)>=960 for i,a in enumerate(slots) for b in slots[i+1:]))

    def test_overnight_and_existing(self):
        chat = {'window_start': 22*60, 'window_end': 2*60}
        now = 86400*20000
        occupied = [now+20*3600]
        slots = make_slots(now+60, now, 960, occupied, allowed=lambda t: within_window(t, chat))
        self.assertTrue(slots)
        self.assertTrue(all(within_window(t, chat) and distance(t, occupied[0])>=960 for t in slots))

    def test_empty_window_in_remaining_day(self):
        now = 86400*20000
        slots = make_slots(now+23*3600, now, 960, allowed=lambda t: within_window(t, {'window_start':540,'window_end':600}))
        self.assertEqual(slots, [])


class InstallTests(unittest.TestCase):
    def test_install_preserves_older_version_and_refuses_overwrite(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            old = root/'installed'/'3.7'
            old.mkdir(parents=True)
            (old/'keep.txt').write_text('original')
            archive = root/'payload.zip'
            with zipfile.ZipFile(archive, 'w') as out:
                out.writestr('TelegramOfferScheduler/TelegramOfferScheduler.exe', b'fake exe')
                out.writestr('TelegramOfferScheduler/_internal/test.dll', b'data')
            executable = install_archive(archive, root/'installed')
            self.assertEqual(executable.read_bytes(), b'fake exe')
            self.assertEqual((old/'keep.txt').read_text(), 'original')
            with self.assertRaises(ValueError):
                install_archive(archive, root/'installed')

    def test_archive_traversal_fails_without_partial_install(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root/'payload.zip'
            with zipfile.ZipFile(archive, 'w') as out:
                out.writestr('TelegramOfferScheduler/../outside.txt', 'bad')
            with self.assertRaises(ValueError):
                install_archive(archive, root/'installed')
            self.assertFalse((root/'installed'/VERSION).exists())
            self.assertFalse(list((root/'installed').iterdir()))


class RotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.engine = Engine(self.directory, lambda _:None, threading.Event())
        self.engine.client = Server()
        self.engine.sleep = AsyncMock()
        self.engine.entities = {-1: types.InputPeerChat(1)}
        self.chats = [{'id':-1, 'title':'Test', 'allowed':True, 'manual':15, 'rules':'yes', 'template_id':'A', 'rotation':['A','B']}]
    async def asyncTearDown(self):
        self.engine.store.db.close()
        self.temp.cleanup()

    async def test_rotation_persisted_verified_and_idempotent(self):
        templates = {'A':'first', 'B': {'text':'<b>second</b>', 'format':'html'}}
        await self.engine.prepare(self.chats, templates, int(time.time())+600, 86400, 4, 1)
        self.engine.store.add_plan(self.engine.plan)
        jobs = self.engine.store.jobs(-1)
        self.assertEqual([j['template_id'] for j in jobs], ['A','B','A','B'])
        await self.engine.resume(self.chats)
        self.assertEqual([m.message for m in self.engine.client.queues[-1]], ['first','second','first','second'])
        self.assertIsInstance(self.engine.client.queues[-1][1].entities[0], types.MessageEntityBold)
        await self.engine.resume(self.chats)
        self.assertEqual(len(self.engine.client.queues[-1]), 4)

    async def test_preflight_is_read_only_and_clears_plan(self):
        self.engine.write_access = AsyncMock(return_value='права проверены')
        result = await self.engine.preflight(self.chats, {'A':'first','B':'second'}, int(time.time())+600, 86400, 4, 1)
        self.assertIn('можно подготовить 4', result['preflight'][0])
        self.assertEqual(self.engine.plan, [])
        self.assertEqual(self.engine.store.jobs(-1), [])
        self.assertFalse(any(isinstance(r, functions.messages.SendMessageRequest) for r in self.engine.client.requests))

    async def test_personal_text_restriction_preflight(self):
        self.engine.entities[-1] = types.InputPeerChannel(1, 2)
        self.engine.rpc = AsyncMock(side_effect=[NS(chats=[NS(id=1)]), NS(participant=NS(banned_rights=NS(send_plain=True)))])
        with self.assertRaisesRegex(ValueError, 'запрещена'):
            await self.engine.write_access(-1)

    async def test_overview_timestamp_and_csv_actions(self):
        Profiles(self.directory)
        save_json(self.directory/'settings.json', {'templates':{'A':'text'}, 'chats':{'-1':{}}})
        await self.engine.prepare(self.chats, {'A':'=unsafe','B':'second'}, int(time.time())+600, 86400, 2, 1)
        await self.engine.execute()
        rows = (await self.engine.queues(self.chats))['queue']
        await self.engine.mutate([rows[0]], 'delete')
        report = overview(self.directory)[0]
        self.assertEqual(report['queued'], 1)
        self.assertIsNotNone(report['checked'])
        target = self.directory/'report.csv'
        self.assertEqual(export_report(self.directory, target), 3)
        text = target.read_text(encoding='utf-8-sig')
        self.assertIn('delete', text)
        self.assertIn("'=unsafe", text)
