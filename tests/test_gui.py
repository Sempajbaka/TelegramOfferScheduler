"""Qt integration checks on synthetic profiles only; no Telegram connection."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from PySide6.QtWidgets import QApplication
from app import Window, configure_app


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def wait_for(self, condition):
        deadline = time.monotonic() + 6
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(condition())

    def test_switch_profiles_preserves_independent_settings(self):
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            window.show()
            try:
                self.wait_for(lambda: window.worker.initialized.is_set())
                window.text.setPlainText('Мой оффер о сотрудничестве')
                window.save_template()
                second = window.profiles.add('Second')
                window.refresh_profiles(second)
                window.switch_profile()
                self.wait_for(lambda: not window.busy and window.profiles.data['active'] == second)
                self.assertEqual(window.text.toPlainText(), '')
                self.assertFalse(window.loaded)
                self.assertEqual(window.queue_rows, [])
                window.text.setPlainText('Другой оффер')
                window.zone.setCurrentIndex(window.zone.findData('Europe/Moscow'))
                instant = window.start.dateTime().toSecsSinceEpoch()
                window.zone.setCurrentIndex(window.zone.findData('UTC'))
                self.assertEqual(window.start.dateTime().toSecsSinceEpoch(), instant)
                window.save_template()
                window.refresh_profiles('legacy')
                window.switch_profile()
                self.wait_for(lambda: not window.busy and window.profiles.data['active'] == 'legacy')
                self.assertEqual(window.text.toPlainText(), 'Мой оффер о сотрудничестве')
                self.assertTrue((window.profiles.path(second) / 'settings.json').exists())
            finally:
                window.close()
                self.app.processEvents()

    def test_new_template_assignment_survives_reload(self):
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                self.wait_for(lambda: window.worker.initialized.is_set())
                window.config['templates']['Другой'] = 'Другие условия'
                window.result({'dialogs': [{'id': -1, 'title': 'Test'}]})
                window.chats.cellWidget(0, 5).setCurrentText('Другой')
                window.save()
                window.result({'dialogs': [{'id': -1, 'title': 'Test'}]})
                self.assertEqual(window.chats.cellWidget(0, 5).currentText(), 'Другой')
            finally:
                window.close()
                self.app.processEvents()

    def test_chat_search_and_participant_counts(self):
        from PySide6.QtCore import Qt
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                self.wait_for(lambda: window.worker.initialized.is_set())
                dialogs = [{'id': -1, 'title': 'Работа Москва', 'participants_count': 1200},
                           {'id': -2, 'title': 'Вакансии СПб', 'participants_count': None},
                           {'id': -3, 'title': 'Пустая группа', 'participants_count': 0}]
                window.result({'dialogs': dialogs})
                window.chats.item(1, 0).setCheckState(Qt.CheckState.Checked)
                window.chat_search.setText('  МОСКВА  ')
                self.assertFalse(window.chats.isRowHidden(0))
                self.assertTrue(window.chats.isRowHidden(1))
                self.assertEqual(window.chats.item(0, 9).text(), '1200')
                self.assertEqual(window.chats.item(1, 9).text(), 'Недоступно')
                self.assertEqual(window.chats.item(2, 9).text(), '0')
                self.assertEqual(window.selected_chats()[0]['id'], -2)
                self.assertIn('скрыто поиском: 1', window.chat_filter_status.text())
                window.save()
                window.result({'dialogs': dialogs})
                self.assertTrue(window.chats.isRowHidden(1))
                window.chat_search.clear()
                self.assertFalse(any(window.chats.isRowHidden(i) for i in range(3)))
            finally:
                window.close()
                self.app.processEvents()

    def test_full_queue_button_prepares_without_publishing(self):
        from unittest.mock import Mock
        from PySide6.QtCore import Qt
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                self.wait_for(lambda: window.worker.initialized.is_set())
                window.result({'dialogs': [{'id': -1, 'title': 'Test'}]})
                window.chats.item(0, 0).setCheckState(Qt.CheckState.Checked)
                window.loaded = True
                window.test_mode.setChecked(True)
                window.cap.setValue(1)
                window.mode.setCurrentIndex(1)
                window.run = Mock()
                window.prepare_full_queue()
                self.assertFalse(window.test_mode.isChecked())
                self.assertEqual(window.cap.value(), 100)
                self.assertEqual(window.mode.currentIndex(), 0)
                window.run.assert_called_once()
                self.assertEqual(window.run.call_args.args[0], 'prepare')
                self.assertEqual(window.run.call_args.args[4], 86400)
                self.assertEqual(window.run.call_args.args[5], 100)
            finally:
                window.loaded = False
                window.close()
                self.app.processEvents()

    def test_global_api_and_html_survive_profile_switch(self):
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                self.wait_for(lambda: window.worker.initialized.is_set())
                window.api_id.setText('123')
                window.api_hash.setText('a' * 32)
                window.text.setPlainText('<b>Сотрудничество</b>')
                window.template_format.setCurrentIndex(1)
                window.save_template()
                self.assertEqual(window.api_hash.text(), '')
                self.assertEqual(window.api_config['api_id'], 123)
                other = window.profiles.add('Second')
                window.refresh_profiles(other)
                window.switch_profile()
                self.wait_for(lambda: not window.busy and window.profiles.data['active'] == other)
                self.assertEqual(window.api_id.text(), '123')
                self.assertEqual(window.template_format.currentData(), 'plain')
                window.refresh_profiles('legacy')
                window.switch_profile()
                self.wait_for(lambda: not window.busy and window.profiles.data['active'] == 'legacy')
                self.assertEqual(window.template_format.currentData(), 'html')
                self.assertEqual(window.text.toPlainText(), '<b>Сотрудничество</b>')
            finally:
                window.close()
                self.app.processEvents()
