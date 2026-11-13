"""Render the application's own UI with synthetic data; no Telegram connection."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, Qt
from app import Window, configure_app

app = QApplication([])
configure_app(app)
temp = TemporaryDirectory()
window = Window(Path(temp.name))
window.show()
destination = Path(__file__).resolve().parents[1] / 'screenshots'
destination.mkdir(exist_ok=True)

def render():
    window.result({'dialogs': [{'id': -123, 'title': 'Тестовая группа — ДЕМО', 'participants_count': 1250}]})
    window.chats.item(0, 0).setCheckState(Qt.CheckState.Checked)
    window.chats.item(0, 2).setCheckState(Qt.CheckState.Checked)
    window.chats.item(0, 4).setText('Разрешение администратора; интервал не менее 15 минут')
    window.text.setPlainText('Демонстрационный текст оффера.\nЭто локальный макет, он не отправляется в Telegram.')
    window.test_mode.setChecked(False)
    start = int(time.time()) + 600
    window.result({'plan': [{'title': 'Тестовая группа — ДЕМО', 'participants_count': 1250, 'slow': 900, 'interval': 960,
                            'existing': 0, 'slots': [start + i*960 for i in range(90)], 'repeat': 86400,
                            'rules': 'Разрешение администратора; интервал не менее 15 минут',
                            'text': window.text.toPlainText()}]})
    window.status.setText('ДЕМО • синтетические данные • подключения к Telegram нет')
    for index, name in [(0, 'account'), (1, 'groups'), (3, 'schedule'), (2, 'templates'), (6, 'diagnostics'), (7, 'settings')]:
        window.tabs.setCurrentIndex(index)
        app.processEvents()
        assert window.grab().save(str(destination / f'{name}.png'))
    window.close()

QTimer.singleShot(1000, render)
app.exec()
temp.cleanup()
