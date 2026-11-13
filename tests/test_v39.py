import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace as NS
from datetime import datetime, timezone
from telethon import errors
from engine import Engine, Stopped


class QueueReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_finite_pass_over_235_groups_with_progress_and_no_duplicates(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            engine.account = AsyncMock()
            engine.history = AsyncMock(return_value=[])
            engine.slowmode = AsyncMock(return_value=0)
            events = []
            engine.progress = events.append
            chats = [dict(id=-i, title=f'Group {i}') for i in range(1,236)]
            try:
                result = await engine.queues(chats + chats[:1])
                self.assertEqual(len(result['chat_reports']), 235)
                self.assertEqual(engine.history.await_count, 235)
                self.assertEqual(engine.slowmode.await_count, 235)
                self.assertEqual((events[-1]['completed'], events[-1]['total']), (235,235))
                self.assertEqual(events[-1]['unit'], 'групп обработано')
                self.assertTrue(any(e.get('title') == 'Group 1' for e in events))
            finally:
                engine.store.db.close()

    async def test_empty_selection_performs_no_requests(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _:None, threading.Event())
            engine.account = AsyncMock()
            try:
                self.assertEqual((await engine.queues([]))['queue'], [])
                engine.account.assert_not_awaited()
            finally:
                engine.store.db.close()

    async def test_error_counts_and_cancel_stops_next_chat(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _:None, threading.Event())
            engine.account = AsyncMock()
            engine.history = AsyncMock(side_effect=errors.ChatAdminRequiredError(request=None))
            try:
                result = await engine.queues([dict(id=-1,title='One')])
                self.assertTrue(result['chat_reports'][0]['error'])
                self.assertEqual(engine.completed, 1)
                engine.cancel.set()
                with self.assertRaises(Stopped):
                    await engine.queues([dict(id=-2,title='Two')])
                self.assertEqual(engine.history.await_count, 1)
            finally:
                engine.store.db.close()


class QueueGuiTests(unittest.TestCase):
    def test_loading_saved_groups_does_not_start_queue_requests(self):
        from PySide6.QtWidgets import QApplication
        from app import Window
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                window.loaded = True
                dialogs = [dict(id=-i,title=f'Group {i}') for i in range(1,236)]
                for selected in (False, True):
                    window.config['chats'] = {str(r['id']): {'selected': selected} for r in dialogs}
                    with patch.object(window, 'run') as run:
                        window.result({'dialogs': dialogs})
                        app.processEvents()
                        run.assert_not_called()
            finally:
                window.close()
                app.processEvents()

    def test_date_sort_keeps_server_id_attached_to_selected_message(self):
        from PySide6.QtWidgets import QApplication
        from app import Window
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                rows = [dict(chat=-1,title='Test',id=i,stamp=t,repeat=86400,text=str(i),template_id=None)
                        for i,t in [(69,3000),(91,2000),(90,1000)]]
                window.result({'queue':rows})
                self.assertEqual([r['id'] for r in window.queue_rows], [90,91,69])
                window.queue.selectRow(0)
                self.assertEqual(window.selected_rows()[0]['id'], 90)
                with patch.object(window, 'confirm', return_value=True), patch.object(window, 'run') as run:
                    window.mutate('delete')
                    self.assertEqual(run.call_args.args[1][0]['id'], 90)
                window.result({'queue': rows})
                self.assertEqual(window.selected_rows(), [])
            finally:
                window.close()
                app.processEvents()
