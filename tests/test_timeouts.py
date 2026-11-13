import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import AsyncMock
from telethon import functions, types, errors
from engine import Engine, Stopped
from test_engine import FakeClient


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.engine = Engine(Path(self.temp.name), lambda _: None, threading.Event())
        self.engine.request_timeout = .05
        self.engine.sleep = AsyncMock()

    async def asyncTearDown(self):
        self.engine.store.db.close()
        self.temp.cleanup()

    async def test_hung_request_times_out_and_can_retry_read(self):
        self.engine.client = AsyncMock(side_effect=lambda _: None)
        async def hang(request):
            await asyncio.Event().wait()
        self.engine.client = hang
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.engine.rpc(object()), 1)
        self.engine.client = AsyncMock(return_value=42)
        self.assertEqual(await self.engine.rpc(object()), 42)

    async def test_stop_interrupts_in_flight_request(self):
        self.engine.request_timeout = 40
        async def hang():
            await asyncio.Event().wait()
        task = asyncio.create_task(self.engine.network(hang))
        await asyncio.sleep(.01)
        self.engine.cancel.set()
        with self.assertRaises(Stopped):
            await asyncio.wait_for(task, 1)

    async def test_repeated_flood_wait_is_bounded_and_preserved(self):
        self.engine.client = AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=12))
        with self.assertRaises(Stopped):
            await self.engine.rpc(object())
        self.assertEqual(self.engine.client.await_count, 3)
        self.assertGreater(float(self.engine.store.meta('wait_until')), time.time())

    async def test_cancel_resistant_task_does_not_freeze_ui(self):
        release = asyncio.Event()
        async def stubborn():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
        try:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(self.engine.network(stubborn), 2)
            self.assertTrue(self.engine.network_stalled)
            with self.assertRaises(Stopped):
                await self.engine.network(lambda: asyncio.sleep(0))
        finally:
            release.set()
            await asyncio.sleep(.01)

    async def test_timeout_after_server_creation_reconciles_without_duplicate(self):
        class HungAck(FakeClient):
            async def __call__(self, request):
                result = await super().__call__(request)
                if isinstance(request, functions.messages.SendMessageRequest):
                    await asyncio.Event().wait()
                return result
        self.engine.client = HungAck()
        self.engine.entities[-1] = types.InputPeerChat(1)
        chat = {'id': -1, 'title': 'Test', 'allowed': True, 'manual': 15, 'rules': 'Permission'}
        self.engine.store.add_plan([{'chat': -1, 'slots': [int(time.time())+600], 'text': 'Offer', 'repeat': 86400}])
        with self.assertRaises(TimeoutError):
            await self.engine.resume([chat])
        self.assertEqual(self.engine.store.jobs(-1)[0]['state'], 'sending')
        await self.engine.resume([chat])
        self.assertEqual(self.engine.store.jobs(-1)[0]['state'], 'verified')
        self.assertEqual(len(self.engine.client.sent), 1)
