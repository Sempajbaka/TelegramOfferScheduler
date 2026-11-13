import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
from unittest.mock import patch, AsyncMock
from telethon import types
from engine import Engine
from test_v2 import Server


class QueueEditorTests(unittest.TestCase):
    def test_direct_edit_uses_selected_message_and_requires_confirmation(self):
        from PySide6.QtWidgets import QApplication
        from app import Window
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            row = {'chat': -1, 'id': 42, 'title': 'Test', 'text': 'Original', 'repeat': 86400,
                   'stamp': int(time.time())+600, 'entities': '[]', 'media': False}
            try:
                with patch.object(window, 'selected_rows', return_value=[row]), patch.object(window, 'run') as run, \
                     patch.object(window, 'confirm', return_value=False), \
                     patch('app.QInputDialog.getMultiLineText', return_value=('Changed', True)) as editor:
                    window.edit_scheduled()
                    self.assertEqual(editor.call_args.args[-1], 'Original')
                    run.assert_not_called()
                with patch.object(window, 'selected_rows', return_value=[row]), patch.object(window, 'run') as run, \
                     patch.object(window, 'confirm', return_value=True), \
                     patch('app.QInputDialog.getMultiLineText', return_value=('Changed', True)):
                    window.edit_scheduled()
                    run.assert_called_once_with('mutate', [row], 'edit', {'text': 'Changed', 'format': 'plain'})
                    self.assertNotIn('Changed', window.config['templates'].values())
                with patch.object(window, 'selected_rows', return_value=[row]), patch.object(window, 'run') as run, \
                     patch.object(window, 'confirm', return_value=True):
                    window.mutate('delete')
                    run.assert_called_once_with('mutate', [row], 'delete', '')
            finally:
                window.close()
                app.processEvents()


class QueueMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_and_delete_refresh_queue_keep_other_slot(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            engine.client = Server()
            engine.sleep = AsyncMock()
            engine.entities = {-1: types.InputPeerChat(1)}
            chats = [{'id': -1, 'title': 'Test', 'allowed': True, 'manual': 15, 'rules': 'yes', 'template_id': 'A'}]
            with closing(engine.store.db):
                await engine.prepare(chats, {'A': 'Original'}, int(time.time())+600, 86400, 2, 1)
                await engine.execute()
                before = (await engine.queues(chats))['queue']
                changed = await engine.mutate([before[0]], 'edit', 'Changed')
                rows = changed['queue']
                self.assertEqual(len(rows), 2)
                row = next(r for r in rows if r['id'] == before[0]['id'])
                self.assertEqual(row['text'], 'Changed')
                self.assertEqual(row['stamp'], before[0]['stamp'])
                self.assertEqual(row['repeat'], 86400)
                deleted = await engine.mutate([row], 'delete')
                self.assertEqual(len(deleted['queue']), 1)
                self.assertEqual(deleted['queue'][0]['text'], 'Original')
                await engine.resume(chats)
                self.assertEqual(len(engine.client.queues[-1]), 1)
