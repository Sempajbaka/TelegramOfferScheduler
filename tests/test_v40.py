import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from telethon import functions, types

from app import Window
from dialog_tools import dialog_matches, sort_dialogs
from engine import Engine
from profiles import Vault
from storage import data_dir


ROWS = [
    dict(id=-10, title='Бета', kind='group', last_activity=200, participants_count=5,
         folders=['main'], folder_names=['Основная']),
    dict(id=-20, title='Альфа', kind='channel', last_activity=100, participants_count=500,
         folders=['archive', 'custom:2'], folder_names=['Архив', 'Работа']),
    dict(id=30, title='Личный', kind='private', last_activity=0, participants_count=None,
         folders=['main'], folder_names=['Основная']),
]


class DialogToolsTests(unittest.TestCase):
    def test_sort_search_members_and_missing_values(self):
        self.assertEqual([row['id'] for row in sort_dialogs(ROWS)], [-10, -20, 30])
        self.assertEqual([row['id'] for row in sort_dialogs(ROWS, 'activity_asc')], [-20, -10, 30])
        self.assertEqual([row['id'] for row in sort_dialogs(ROWS, 'members_desc')], [-20, -10, 30])
        self.assertEqual([row['id'] for row in sort_dialogs(ROWS, 'title_asc')], [-20, -10, 30])
        self.assertTrue(dialog_matches(ROWS[1], '  АЛЬФА ', 'channel', 'custom:2'))
        self.assertFalse(dialog_matches(ROWS[1], '', 'group', 'all'))


class CrossPlatformTests(unittest.TestCase):
    def test_macos_data_directory(self):
        with TemporaryDirectory() as folder, \
                patch('storage.os.name', 'posix'), patch('storage.sys.platform', 'darwin'), \
                patch('storage.Path.home', return_value=Path(folder)):
            self.assertEqual(data_dir(), Path(folder) / 'Library' / 'Application Support' / 'TelegramOfferScheduler')

    def test_macos_keychain_roundtrip_uses_secretless_marker(self):
        with TemporaryDirectory() as folder, \
                patch('profiles.os.name', 'posix'), patch('profiles.sys.platform', 'darwin'):
            vault = Vault(folder)
            completed = [NS(returncode=0, stdout='', stderr=''),
                         NS(returncode=0, stdout='secret-value\n', stderr='')]
            with patch('profiles.subprocess.run', side_effect=completed) as run:
                vault.write('session', 'secret-value')
                self.assertTrue(vault.exists('session'))
                self.assertEqual(vault.read('session'), 'secret-value')
            self.assertEqual((Path(folder) / 'session.keychain').read_text(), 'macOS Keychain\n')
            self.assertNotIn('secret-value', (Path(folder) / 'session.keychain').read_text())
            self.assertEqual(run.call_count, 2)


class CleanupGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_marks_survive_search_sort_and_folder_changes(self):
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                window.result({'account_dialogs': ROWS,
                               'dialog_folders': [{'id': 'main', 'title': 'Основная'},
                                                  {'id': 'archive', 'title': 'Архив'},
                                                  {'id': 'custom:2', 'title': 'Работа'}]})
                window.cleanup_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
                marked = window.cleanup_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
                window.cleanup_search.setText('нет такого')
                window.cleanup_sort.setCurrentIndex(window.cleanup_sort.findData('title_desc'))
                window.cleanup_folder.setCurrentIndex(window.cleanup_folder.findData('archive'))
                self.assertEqual(window.cleanup_selected_ids, {marked})
                self.assertIn('скрыто фильтрами: 1', window.cleanup_filter_status.text())
                window.cleanup_search.clear()
                window.cleanup_folder.setCurrentIndex(0)
                checked = {window.cleanup_table.item(i, 0).data(Qt.ItemDataRole.UserRole)
                           for i in range(window.cleanup_table.rowCount())
                           if window.cleanup_table.item(i, 0).checkState() == Qt.CheckState.Checked}
                self.assertEqual(checked, {marked})
            finally:
                window.close()
                self.app.processEvents()

    def test_confirmation_uses_durable_ids_not_visual_order(self):
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                window.result({'account_dialogs': ROWS, 'dialog_folders': []})
                window.cleanup_selected_ids = {-20, 30}
                window.cleanup_sort.setCurrentIndex(window.cleanup_sort.findData('members_desc'))
                with patch.object(window, 'confirm', return_value=True), patch.object(window, 'run') as run:
                    window.prepare_dialog_cleanup()
                self.assertEqual(run.call_args.args[0], 'cleanup_dialogs')
                self.assertEqual({row['id'] for row in run.call_args.args[1]}, {-20, 30})
            finally:
                window.close()
                self.app.processEvents()


class CleanupEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_dialog_history_is_deleted_until_offset_zero(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            engine.account = AsyncMock()
            user = types.User(id=30, access_hash=123, first_name='Личный')
            engine.dialog_entities = {30: user}
            engine.rpc = AsyncMock(side_effect=[NS(offset=1), NS(offset=0)])
            try:
                result = await engine.cleanup_dialogs([dict(id=30, title='Личный')])
                self.assertTrue(result['cleanup_reports'][0]['ok'])
                self.assertEqual(engine.rpc.await_count, 2)
                self.assertTrue(all(isinstance(call.args[0], functions.messages.DeleteHistoryRequest)
                                    for call in engine.rpc.await_args_list))
                self.assertNotIn(30, engine.dialog_entities)
            finally:
                engine.store.db.close()

    async def test_stale_row_is_not_sent_to_telegram(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            engine.account = AsyncMock()
            engine.rpc = AsyncMock()
            try:
                result = await engine.cleanup_dialogs([dict(id=999, title='Устарел')])
                self.assertFalse(result['cleanup_reports'][0]['ok'])
                engine.rpc.assert_not_awaited()
            finally:
                engine.store.db.close()

    def test_custom_folder_rule_and_archive_membership(self):
        entity = types.User(id=30, access_hash=123, first_name='Личный')
        dialog = NS(peer=types.PeerUser(30), folder_id=1, unread_count=1,
                    unread_mark=False, notify_settings=None)
        custom = NS(id=2, title='Работа', pinned_peers=[], include_peers=[types.InputPeerUser(30, 123)],
                    exclude_peers=[], bots=False, contacts=False, non_contacts=False, groups=False,
                    broadcasts=False, exclude_archived=False, exclude_read=False, exclude_muted=False)
        ids, names = Engine._dialog_folders(dialog, entity, [custom])
        self.assertEqual(ids, ['archive', 'custom:2'])
        self.assertEqual(names, ['Архив', 'Работа'])


if __name__ == '__main__':
    unittest.main()
