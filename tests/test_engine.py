import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import threading
import time
import unittest
from unittest.mock import patch, AsyncMock

from telethon import functions, types, errors
from engine import Engine, Stopped, capability


class FakeClient:
    def __init__(self):
        self.messages = []
        self.sent = []
        self.fail = None
        self.strip_repeat = False
        self.last_post = None

    async def __call__(self, request):
        if isinstance(request, functions.users.GetUsersRequest):
            return [NS(id=123, bot=False, premium=True)]
        if isinstance(request, functions.messages.GetScheduledHistoryRequest):
            return NS(messages=list(self.messages))
        if isinstance(request, functions.messages.SearchRequest):
            return NS(messages=[NS(date=self.last_post)] if self.last_post else [])
        if isinstance(request, functions.messages.SendMessageRequest):
            self.sent.append(request)
            if self.fail:
                raise self.fail
            self.messages.append(NS(id=len(self.messages) + 1, date=request.schedule_date,
                message=request.message, schedule_repeat_period=None if self.strip_repeat else request.schedule_repeat_period,
                media=None))
            return NS()
        if isinstance(request, functions.messages.EditMessageRequest):
            msg = next(m for m in self.messages if m.id == request.id)
            msg.message = request.message
            msg.schedule_repeat_period = request.schedule_repeat_period
            return NS()
        if isinstance(request, functions.messages.DeleteScheduledMessagesRequest):
            self.messages = [m for m in self.messages if m.id not in request.id]
            return NS()
        raise AssertionError(type(request))


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.engine = Engine(Path(self.tmp.name), lambda x: None, threading.Event())
        self.engine.client = FakeClient()
        self.engine.entities[-123] = types.InputPeerChat(123)
        self.engine.pause = 0
        self.engine.create_pause = 0
        self.config = {'id': -123, 'title': 'Test', 'allowed': True, 'manual': 15, 'rules': 'Admin permission'}

    async def asyncTearDown(self):
        self.engine.store.db.close()
        self.tmp.cleanup()

    def plan(self, count=2, repeat=86400):
        start = int(time.time()) + 600
        p = {**self.config, 'chat': -123, 'slots': [start + 960*i for i in range(count)],
             'text': 'Offer', 'repeat': repeat}
        self.engine.store.add_plan([p])
        return p

    def test_pinned_schema_support(self):
        self.assertTrue(capability()['repeat'])

    async def test_full_preview_execute_flow(self):
        self.engine.sleep = AsyncMock()
        start = int(time.time()) + 600
        result = await self.engine.prepare([self.config], 'Offer', start, 86400, 2, 1)
        self.assertEqual(len(result['plan'][0]['slots']), 2)
        self.assertEqual(self.engine.client.sent, [])
        await self.engine.execute()
        self.assertEqual(len(self.engine.client.messages), 2)
        with self.assertRaisesRegex(ValueError, 'уже есть локальный'):
            await self.engine.prepare([self.config], 'Changed', start, 86400, 2, 1)

    async def test_preview_stale_queue_blocks_execution(self):
        self.engine.sleep = AsyncMock()
        start = int(time.time()) + 600
        await self.engine.prepare([self.config], 'Offer', start, 86400, 2, 1)
        self.engine.client.messages.append(NS(id=50, date=datetime.fromtimestamp(start + 50, timezone.utc),
                                             message='Other', schedule_repeat_period=86400))
        with self.assertRaisesRegex(ValueError, 'изменилась после просмотра'):
            await self.engine.execute()
        self.assertFalse(self.engine.client.sent)

    async def test_permission_required(self):
        self.engine.sleep = AsyncMock()
        with self.assertRaisesRegex(ValueError, 'разрешение'):
            await self.engine.prepare([{**self.config, 'allowed': False}], 'Offer', int(time.time()) + 600, 86400, 1, 1)
        self.assertFalse(self.engine.client.sent)

    async def test_plan_survives_database_reopen(self):
        from storage import Store
        self.plan(1)
        before = self.engine.store.jobs(-123)
        self.engine.store.db.close()
        self.engine.store = Store(Path(self.tmp.name) / 'progress.sqlite3')
        self.assertEqual(before, self.engine.store.jobs(-123))
        await self.engine.resume([self.config])
        self.assertEqual(self.engine.client.sent[0].random_id, before[0]['random_id'])

    async def test_resume_is_idempotent(self):
        self.plan()
        await self.engine.resume([self.config])
        await self.engine.resume([self.config])
        self.assertEqual(len(self.engine.client.sent), 2)
        self.assertTrue(all(j['state'] == 'verified' for j in self.engine.store.jobs(-123)))

    async def test_server_missing_repeat_stops(self):
        self.plan()
        self.engine.client.strip_repeat = True
        with self.assertRaisesRegex(ValueError, 'НЕ подтвержден'):
            await self.engine.resume([self.config])
        self.assertEqual(len(self.engine.client.sent), 1)
        self.assertEqual(self.engine.store.jobs(-123)[0]['state'], 'unverified')

    async def test_ambiguous_network_outcome_no_retry(self):
        self.plan()
        self.engine.client.fail = ConnectionError()
        with self.assertRaises(ConnectionError):
            await self.engine.resume([self.config])
        self.engine.client.fail = None
        with self.assertRaisesRegex(ValueError, 'Неопределённый'):
            await self.engine.resume([self.config])
        self.assertEqual(len(self.engine.client.sent), 1)

    async def test_lost_ack_reconciles_from_server(self):
        p = self.plan(1)
        job = self.engine.store.jobs(-123)[0]
        self.engine.store.update(-123, job['stamp'], state='sending')
        self.engine.client.messages = [NS(id=45, date=datetime.fromtimestamp(job['stamp'], timezone.utc),
            message='Offer', schedule_repeat_period=86400, media=None)]
        await self.engine.resume([self.config])
        self.assertFalse(self.engine.client.sent)
        self.assertEqual(self.engine.store.jobs(-123)[0]['message_id'], 45)

    async def test_cancel_no_sends(self):
        self.plan()
        self.engine.cancel.set()
        with self.assertRaises(Stopped):
            await self.engine.resume([self.config])
        self.assertFalse(self.engine.client.sent)

    async def test_existing_conflict_blocks(self):
        p = self.plan(1)
        self.engine.client.messages = [NS(id=44, date=datetime.fromtimestamp(p['slots'][0] + 100, timezone.utc),
                                        message='Other', schedule_repeat_period=86400)]
        with self.assertRaisesRegex(ValueError, 'Конфликт'):
            await self.engine.resume([self.config])
        self.assertFalse(self.engine.client.sent)

    async def test_recent_published_message_blocks(self):
        p = self.plan(1)
        self.engine.client.last_post = datetime.fromtimestamp(p['slots'][0] - 100, timezone.utc)
        with self.assertRaisesRegex(ValueError, 'после последнего'):
            await self.engine.resume([self.config])
        self.assertFalse(self.engine.client.sent)

    async def test_queue_full_persists_pending(self):
        self.plan(1)
        self.engine.client.fail = errors.ScheduleTooMuchError(request=None)
        with self.assertRaises(errors.ScheduleTooMuchError):
            await self.engine.resume([self.config])
        self.assertEqual(self.engine.store.jobs(-123)[0]['state'], 'pending')

    async def test_edit_preserves_repeat_and_delete(self):
        self.plan(1)
        await self.engine.resume([self.config])
        rows = (await self.engine.queues([self.config]))['queue']
        await self.engine.mutate(rows, 'edit', 'New offer')
        self.assertEqual(self.engine.client.messages[0].schedule_repeat_period, 86400)
        rows = (await self.engine.queues([self.config]))['queue']
        await self.engine.mutate(rows, 'delete')
        self.assertFalse(self.engine.client.messages)

    async def test_stale_edit_rejected(self):
        self.plan(1)
        await self.engine.resume([self.config])
        rows = (await self.engine.queues([self.config]))['queue']
        self.engine.client.messages[0].message = 'Changed elsewhere'
        with self.assertRaisesRegex(ValueError, 'Очередь изменилась'):
            await self.engine.mutate(rows, 'edit', 'New offer')

    async def test_no_premium_and_no_schema(self):
        self.engine.premium = False
        with self.assertRaisesRegex(ValueError, 'Premium'):
            self.engine.repeat_guard(86400)
        self.engine.repeat_guard(0)
        with patch('engine.capability', return_value={'repeat': False}):
            with self.assertRaisesRegex(ValueError, 'repeat API'):
                self.engine.repeat_guard(86400)

    async def test_past_slot_never_sent(self):
        p = self.plan(1)
        with patch('engine.time.time', return_value=p['slots'][0] + 1):
            with self.assertRaisesRegex(ValueError, 'прошёл'):
                await self.engine.resume([self.config])
        self.assertFalse(self.engine.client.sent)

    async def test_flood_wait_persisted_and_cancelled(self):
        self.plan(1)
        self.engine.client.fail = errors.FloodWaitError(request=None, capture=2)
        task = asyncio.create_task(self.engine.resume([self.config]))
        await asyncio.sleep(.05)
        self.engine.cancel.set()
        with self.assertRaises(Stopped):
            await task
        self.assertGreater(float(self.engine.store.meta('wait_until')), time.time())


if __name__ == '__main__':
    unittest.main()
