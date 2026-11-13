import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
from unittest.mock import AsyncMock, patch
from telethon import types, functions
from formatting import encode, parse_offer
from queue_tools import edited_offer, queue_matches
from backups import create_backup, backup_before_upgrade, restore_backup
from storage import save_json
from engine import Engine
from test_v2 import Server


class EditorTests(unittest.TestCase):
    def test_utf16_emoji_shift_and_bold_replacement(self):
        original = '😀 offer 🔥'
        entities = encode([types.MessageEntityBold(3, 5), types.MessageEntityCustomEmoji(9, 2, 999)])
        updated = 'Hi 😀 changed offer 🔥!'
        offer = edited_offer(original, updated, entities)
        text, result = parse_offer(offer)
        emoji = next(e for e in result if isinstance(e, types.MessageEntityCustomEmoji))
        self.assertEqual(emoji.document_id, 999)
        self.assertEqual(text.encode('utf-16-le')[emoji.offset*2:(emoji.offset+emoji.length)*2].decode('utf-16-le'), '🔥')
        self.assertTrue(any(isinstance(e, types.MessageEntityBold) for e in result))

    def test_custom_emoji_cannot_be_removed(self):
        with self.assertRaisesRegex(ValueError, 'эмодзи'):
            edited_offer('🔥 text', 'text', encode([types.MessageEntityCustomEmoji(0, 2, 123)]))

    def test_replacement_inside_bold_preserves_full_span(self):
        offer = edited_offer('old', 'new', encode([types.MessageEntityBold(0, 3)]))
        _, entities = parse_offer(offer)
        self.assertEqual((entities[0].offset, entities[0].length), (0, 3))

    def test_ambiguous_cross_boundary_edit_rejected(self):
        with self.assertRaises(ValueError):
            edited_offer('abcdef', 'xyz', encode([types.MessageEntityBold(2, 2)]))

    def test_queue_filter(self):
        row = dict(title='Вакансии', text='Оффер', template_id='Партнёры', repeat=86400, issue='Ошибка')
        self.assertTrue(queue_matches(row, 'ПАРТН', 'daily'))
        self.assertTrue(queue_matches(row, '', 'error'))
        self.assertFalse(queue_matches(row, '', 'once'))


class BackupTests(unittest.TestCase):
    def test_upgrade_manual_restore_excludes_secrets_and_preserves_rollback(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            save_json(path / 'settings.json', {'templates': {'A': 'original'}, 'api_hash': 'SECRET', 'chats': {}})
            backup_before_upgrade(path)
            backup_before_upgrade(path)
            self.assertEqual(len(list((path/'backups').glob('*.json'))), 1)
            saved = create_backup(path)
            self.assertNotIn('SECRET', saved.read_text())
            save_json(path / 'settings.json', {'templates': {'A': 'changed'}, 'api_hash': 'KEEP'})
            restore_backup(path, saved)
            config = json.loads((path/'settings.json').read_text())
            self.assertEqual(config['templates']['A'], 'original')
            self.assertEqual(config['api_hash'], 'KEEP')
            self.assertEqual(len(list((path/'backups').glob('*before-restore.json'))), 1)

    def test_invalid_backup_does_not_change_settings(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            save_json(path/'settings.json', {'templates': {'A': 'old'}})
            saved = create_backup(path)
            saved.write_text('{"version":1,"settings":{"templates":[]}}')
            before = (path/'settings.json').read_bytes()
            with self.assertRaises(ValueError):
                restore_backup(path, saved)
            self.assertEqual((path/'settings.json').read_bytes(), before)


class ShiftTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.engine = Engine(Path(self.temp.name), lambda _: None, threading.Event())
        self.engine.client = Server()
        self.engine.sleep = AsyncMock()
        self.engine.entities = {-1: types.InputPeerChat(1)}
        self.chats = [{'id': -1, 'title': 'Test', 'allowed': True, 'manual': 15, 'rules': 'yes', 'template_id': 'A'}]
        await self.engine.prepare(self.chats, {'A': 'Original'}, int(time.time())+1200, 86400, 2, 1)
        await self.engine.execute()
        self.rows = (await self.engine.queues(self.chats))['queue']

    async def asyncTearDown(self):
        self.engine.store.db.close()
        self.temp.cleanup()

    async def test_batch_shift_preserves_repeat_and_order(self):
        result = await self.engine.prepare_shift(self.rows, 60)
        self.assertEqual(result['shift_changes'][0]['row']['id'], self.rows[-1]['id'])
        changed = await self.engine.apply_prepared_changes()
        for row in changed['queue']:
            original = next(r for r in self.rows if r['id'] == row['id'])
            self.assertEqual(row['stamp'], original['stamp']+60)
            self.assertEqual(row['repeat'], 86400)
            self.assertEqual(row['text'], original['text'])
            self.assertIsNotNone(row['job_stamp'])

    async def test_single_shift_conflict_blocked_before_write(self):
        before = len(self.engine.client.requests)
        with self.assertRaisesRegex(ValueError, 'интервал'):
            await self.engine.prepare_shift([self.rows[0]], 60)
        self.assertFalse(any(isinstance(r, functions.messages.EditMessageRequest) for r in self.engine.client.requests[before:]))

    async def test_stale_shift_rechecked_before_write(self):
        await self.engine.prepare_shift(self.rows, 60)
        self.engine.client.queues[-1][0].message = 'external change'
        with self.assertRaisesRegex(ValueError, 'изменилась'):
            await self.engine.apply_prepared_changes()

    async def test_shift_past_and_over_day_rejected(self):
        for delta in (-86400, 86400):
            with self.assertRaises(ValueError):
                await self.engine.prepare_shift(self.rows, delta)

    async def test_progress_reports_counts(self):
        events = []
        self.engine.progress = events.append
        await self.engine.mutate([self.rows[0]], 'edit', 'Changed')
        self.assertTrue(any(v['completed'] == 1 and v['total'] == 1 for v in events))

    async def test_lost_shift_ack_recovers_without_second_edit(self):
        await self.engine.prepare_shift([self.rows[-1]], 60)
        self.engine.client.lose_edit_ack = True
        with self.assertRaises(ConnectionError):
            await self.engine.apply_prepared_changes()
        await self.engine.pending_changes(self.chats)
        result = await self.engine.apply_prepared_changes()
        self.assertEqual(len([r for r in self.engine.client.requests if isinstance(r, functions.messages.EditMessageRequest)]), 1)
        moved = next(r for r in result['queue'] if r['id'] == self.rows[-1]['id'])
        self.assertEqual(moved['stamp'], self.rows[-1]['stamp'] + 60)

    async def test_backward_shift_and_midnight_ring_conflict(self):
        await self.engine.prepare_shift(self.rows, -60)
        result = await self.engine.apply_prepared_changes()
        self.assertEqual(min(r['stamp'] for r in result['queue']), self.rows[0]['stamp']-60)
        from datetime import datetime, timezone
        first = self.engine.client.queues[-1][0]
        second = self.engine.client.queues[-1][1]
        first.date = datetime.fromtimestamp(int(time.time())+120, timezone.utc)
        second.date = datetime.fromtimestamp(int(time.time())+86400-120, timezone.utc)
        self.rows = (await self.engine.queues(self.chats))['queue']
        with self.assertRaisesRegex(ValueError, 'интервал'):
            await self.engine.prepare_shift([self.rows[0]], 60)


class FilterGuiTests(unittest.TestCase):
    def test_hidden_selection_excluded_and_progress_visible(self):
        from PySide6.QtWidgets import QApplication
        from app import Window
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                rows = [dict(chat=-1, title='One', id=i, stamp=int(time.time())+600, repeat=86400,
                             text=text, template_id=None, entities='[]', media=False) for i,text in [(1,'first'), (2,'second')]]
                window.result({'queue': rows})
                window.queue.selectAll()
                window.queue_search.setText('first')
                self.assertEqual([r['id'] for r in window.selected_rows()], [1])
                window.busy = True
                window.receive_progress({'completed': 2, 'total': 4, 'title': 'One', 'phase': 'Ограничение Telegram', 'wait_until': time.time()+30})
                self.assertEqual(window.operation_progress.value(), 2)
                self.assertIn('ожидание', window.progress_label.text())
                window.busy = False
            finally:
                window.close()
                app.processEvents()
