"""Windows GUI; all Telegram and SQLite work stays on one background event loop."""
import asyncio
import json
import logging
import os
from logging.handlers import RotatingFileHandler
import sys
import threading
import time
import qrcode
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal, QDateTime, QLockFile, QTimeZone, QTimer, QTime
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QPushButton, QLabel, QTabWidget, QTableWidget, QTableWidgetItem,
    QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit, QCheckBox,
    QMessageBox, QInputDialog, QAbstractItemView, QHeaderView, QTextBrowser, QProgressBar, QDialog, QDialogButtonBox,
    QTimeEdit, QSystemTrayIcon, QStyle, QFileDialog)

from storage import data_dir, save_json
from engine import Engine, Stopped, capability, error_description
from profiles import Profiles, Vault, load_settings
from desktop_shortcut import create_desktop_shortcut
from application_settings import ApplicationSettings, remember_versions
from formatting import parse_offer, decode
from queue_tools import edited_offer, queue_matches
from backups import backup_before_upgrade, create_backup, restore_backup, read_backup
from reports import overview, export_report
from dialog_tools import KIND_NAMES, dialog_matches, sort_dialogs
from telethon.extensions import html as telegram_html


class Worker(QThread):
    result = Signal(object)
    log = Signal(str)
    ready = Signal()
    progress = Signal(object)

    def __init__(self, directory, registry_root=None):
        super().__init__()
        self.directory = directory
        self.registry_root = registry_root
        self.cancel = threading.Event()
        self.options = {}
        self.initialized = threading.Event()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.engine = Engine(self.directory, self.log.emit, self.cancel, self.registry_root)
        except Exception as exc:
            self.engine = None
            self.result.emit({'error': f'Не удалось открыть данные профиля ({type(exc).__name__}). '
                             'Исходные файлы сохранены. Можно переключиться на другой профиль.'})
        self.initialized.set()
        self.ready.emit()
        self.loop.run_forever()
        self.loop.close()

    def submit(self, method, *args):
        self.cancel.clear()
        async def job():
            try:
                if method == 'switch_profile':
                    replacement = Engine(Path(args[0]), self.log.emit, self.cancel, self.registry_root)
                    if self.engine:
                        await self.engine.close()
                    self.directory = Path(args[0])
                    self.engine = replacement
                    result = {'profile_switched': True}
                else:
                    if self.engine is None:
                        raise ValueError('Данные профиля не открыты. Переключитесь на другой аккаунт.')
                    self.engine.configure(**self.options)
                    self.engine.progress = self.progress.emit
                    self.engine.completed, self.engine.total = 0, 0
                    self.engine.progress_unit = 'подтверждено'
                    result = await getattr(self.engine, method)(*args)
                self.result.emit(result or {})
            except asyncio.CancelledError:
                self.result.emit({'error': 'Операция отменена. Начатый запрос мог выполниться на сервере; проверьте очередь перед продолжением.'})
            except (ValueError, Stopped) as exc:
                self.result.emit({'error': str(exc)})
            except Exception as exc:
                # Never log auth codes, passwords, API hashes, request bodies or session keys.
                self.result.emit({'error': error_description(exc)})
        asyncio.run_coroutine_threadsafe(job(), self.loop)

    def shutdown(self):
        if not self.initialized.wait(5):
            raise RuntimeError('Инициализация ещё не завершена')
        if self.engine:
            future = asyncio.run_coroutine_threadsafe(self.engine.close(), self.loop)
            future.result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.wait(12000)


class PersistentSelectionTable(QTableWidget):
    """Do not erase a large row selection by clicking the table's empty area."""
    def mousePressEvent(self, event):
        if not self.indexAt(event.position().toPoint()).isValid():
            event.accept()
            return
        super().mousePressEvent(event)


class Window(QMainWindow):
    def __init__(self, directory):
        super().__init__()
        self.root_directory = directory
        self.profiles = Profiles(directory)
        self.application_settings = ApplicationSettings(directory)
        self.settings_error = None
        try:
            self.application_settings.migrate()
            self.api_config = self.application_settings.credentials()
        except (OSError, ValueError):
            self.api_config = {}
            self.settings_error = 'Не удалось открыть общие API-реквизиты. Проверьте доступ к профилю Windows; исходные файлы сохранены.'
        self.directory = self.profiles.path(self.profiles.data['active'])
        self.config_path = self.directory / 'settings.json'
        backup_before_upgrade(self.directory)
        self.config = load_settings(self.config_path)
        remember_versions(self.config)
        self.busy = False
        self.loaded = False
        self.auth_stage = None
        self.qr_dialog = None
        self.queue_rows = []
        self.cleanup_rows = []
        self.cleanup_display_rows = []
        self.cleanup_selected_ids = set()
        self.plan = []
        self.changes = []
        self.setWindowTitle('Telegram • Предложение о сотрудничестве — 4.2')
        self.resize(1200, 850)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.status = QLabel('Локальное приложение • без OpenAI API • отправка только после подтверждения')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.operation_progress = QProgressBar()
        self.operation_progress.setRange(0, 1)
        self.operation_progress.setValue(0)
        layout.addWidget(self.operation_progress)
        self.progress_label = QLabel('Нет активной операции')
        layout.addWidget(self.progress_label)
        self.progress_state = {}
        self.progress_started = time.monotonic()
        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self.update_progress_text)
        self.progress_timer.start(500)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.controls = []
        self.auth_page()
        self.chats_page()
        self.template_page()
        self.schedule_page()
        self.queue_page()
        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(3000)
        self.tabs.addTab(self.journal, '6. Журнал')
        diagnostic_layout = self.page('7. Диагностика')
        self.button('Синхронизировать и проверить выбранные группы', lambda: self.run('queues', self.selected_chats()), diagnostic_layout)
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        diagnostic_layout.addWidget(self.diagnostics)
        settings_layout = self.page('8. Общие настройки')
        global_form = QFormLayout()
        global_form.addRow('API ID — для всех аккаунтов', self.api_id)
        global_form.addRow('API Hash — для всех аккаунтов', self.api_hash)
        settings_layout.addLayout(global_form)
        self.button('Сохранить общие API-реквизиты', self.save, settings_layout)
        self.button('Создать ярлык на рабочем столе', self.create_shortcut, settings_layout)
        self.button('Создать резервную копию настроек и шаблонов', self.backup_settings, settings_layout)
        self.button('Восстановить настройки из копии…', self.restore_settings, settings_layout)
        settings_layout.addWidget(QLabel('Папка данных: ' + str(self.root_directory)))
        settings_layout.addWidget(QLabel('API-реквизиты вводятся один раз. Они защищены Windows DPAPI и используются для всех профилей.'))
        settings_layout.addStretch()
        self.notifications = QCheckBox('Уведомления Windows о завершении, ошибках и ограничениях')
        self.notifications.setChecked(self.config.get('notifications', True))
        settings_layout.addWidget(self.notifications)
        self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation), self)
        self.tray.setToolTip('Telegram Offer Scheduler')
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.last_wait_notice = 0
        overview_layout = self.page('9. Все аккаунты')
        overview_layout.addWidget(QLabel('Локальный обзор по последней синхронизации. Это не текущая проверка Telegram. Для обновления подключите нужный аккаунт и перечитайте его очередь.'))
        self.button('Обновить обзор', self.refresh_overview, overview_layout)
        self.overview_table = QTableWidget(0, 7)
        self.overview_table.setHorizontalHeaderLabels(['Аккаунт', 'Группы', 'Отложки при проверке', 'Требуют проверки', 'Ближайшая по снимку', 'Проверено не позже', 'Примечание'])
        self.overview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.overview_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        overview_layout.addWidget(self.overview_table)
        self.button('Экспорт отчёта текущего аккаунта (CSV)…', self.export_csv, overview_layout)
        self.refresh_overview()
        self.account_cleanup_page()
        bottom = QHBoxLayout()
        self.stop = QPushButton('Остановить операцию')
        self.stop.clicked.connect(self.cancel)
        bottom.addWidget(self.stop)
        bottom.addWidget(QLabel('Отмена не удаляет уже созданные отложенные сообщения.'))
        layout.addLayout(bottom)
        for label in self.findChildren(QLabel):
            label.setWordWrap(True)
        self.worker = Worker(self.directory, self.root_directory)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self.receive_progress)
        self.worker.result.connect(self.result)
        self.worker.ready.connect(lambda: self.log(f"Telethon {capability()['version']}; repeat API: {capability()['repeat']}"))
        self.worker.ready.connect(self.auto_restore)
        self.worker.start()

    def receive_progress(self, value):
        if value.get('phase') == 'Ограничение Telegram' and time.monotonic() - self.last_wait_notice > 60:
            self.last_wait_notice = time.monotonic()
            self.notify_windows('Telegram ограничил запросы', 'Программа ожидает разрешённого времени. Ожидание можно отменить.')
        self.progress_state.update(value)
        total, done = value.get('total', 0), value.get('completed', 0)
        self.operation_progress.setRange(0, total if total else 0)
        self.operation_progress.setValue(done)
        if 'status' in value:
            for row in range(self.chats.rowCount()):
                if self.chats.item(row, 0).data(Qt.ItemDataRole.UserRole) == value.get('chat'):
                    item = QTableWidgetItem(value['status'])
                    item.setToolTip(value['status'])
                    self.chats.setItem(row, 7, item)
        self.update_progress_text()

    def update_progress_text(self):
        if not self.busy:
            return
        state = self.progress_state
        done, total = state.get('completed', 0), state.get('total', 0)
        elapsed = time.monotonic() - self.progress_started
        wait = max(0, int(state.get('wait_until', 0) - time.time()))
        estimate = f'примерно {int(elapsed / done * (total-done))} с осталось' if done and total > done else ('проверка завершена' if total and done == total else 'идёт проверка, оценка времени уточняется')
        self.progress_label.setText(f"{state.get('title', '')} • {state.get('phase', 'Подготовка')} • {done}/{total or '?'} {state.get('unit', 'подтверждено')} • прошло {int(elapsed)} с • " + (f'ожидание ещё {wait} с' if wait else estimate))

    def notify_windows(self, title, text):
        if self.notifications.isChecked() and self.tray.isVisible():
            self.tray.showMessage(title, text[:250], QSystemTrayIcon.MessageIcon.Information, 8000)

    def refresh_overview(self):
        rows = overview(self.root_directory)
        self.overview_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            values = [row['name'], row['chats'], row['queued'] if row['queued'] is not None else 'Не проверено', row['errors'],
                      self.format_time(row['next']) if row['next'] else '—', self.format_time(row['checked']) if row['checked'] else '—', row['note']]
            for j, value in enumerate(values):
                self.overview_table.setItem(i, j, QTableWidgetItem(str(value)))

    def export_csv(self):
        filename, ok = QFileDialog.getSaveFileName(self, 'Отчёт текущего аккаунта', str(self.root_directory / 'telegram-report.csv'), 'CSV (*.csv)')
        if filename:
            try:
                count = export_report(self.directory, filename)
                QMessageBox.information(self, 'Отчёт', f'Сохранено записей: {count}. Это журнал известных программе действий, а не подтверждение доставки всех сообщений.')
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, 'Отчёт', str(exc))

    def campaign_settings(self):
        chats = self.selected_chats()
        if not chats:
            QMessageBox.information(self, 'Группы', 'Выберите группы, для которых нужно изменить часы и чередование.')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Часы публикации и чередование офферов')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Время по Москве (UTC+3). Конец не включается. Одинаковое начало и конец — круглосуточно. Настройки применятся к выбранным группам при подготовке нового плана.'))
        start, end = QTimeEdit(), QTimeEdit()
        for field, key in [(start, 'window_start'), (end, 'window_end')]:
            minute = chats[0].get(key, 0)
            field.setTime(QTime(minute//60, minute%60))
            field.setDisplayFormat('HH:mm')
        form = QFormLayout()
        form.addRow('С', start)
        form.addRow('До', end)
        layout.addLayout(form)
        layout.addWidget(QLabel('Названия шаблонов в порядке чередования, по одному на строку. Пусто — только шаблон, выбранный в таблице групп.\nДоступны: ' + ', '.join(self.config['templates'])))
        rotation = QPlainTextEdit('\n'.join(chats[0].get('rotation', [])))
        layout.addWidget(rotation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        names = [n.strip() for n in rotation.toPlainText().splitlines() if n.strip()]
        if any(n not in self.config['templates'] for n in names):
            QMessageBox.warning(self, 'Шаблоны', 'Есть неизвестное название. Используйте точные названия сохранённых шаблонов.')
            return
        self.save()
        for chat in chats:
            self.config['chats'][str(chat['id'])].update(window_start=start.time().hour()*60+start.time().minute(),
                window_end=end.time().hour()*60+end.time().minute(), rotation=names)
        self.save()
        self.log('Часы публикации и чередование сохранены для выбранных групп. Существующая очередь не изменялась.')

    def backup_settings(self):
        if self.save():
            path = create_backup(self.directory)
            QMessageBox.information(self, 'Резервная копия', f'Настройки и шаблоны текущего профиля сохранены:\n{path}')

    def restore_settings(self):
        paths = sorted((self.directory / 'backups').glob('*.json'), reverse=True)
        if not paths:
            QMessageBox.information(self, 'Копии', 'У текущего профиля пока нет резервных копий.')
            return
        name, ok = QInputDialog.getItem(self, 'Восстановление', 'Копия текущего профиля:', [p.name for p in paths], 0, False)
        if not ok:
            return
        path = next(p for p in paths if p.name == name)
        try:
            config = read_backup(path)
            if self.confirm(f"Восстановить настройки, группы и шаблоны ({len(config['templates'])}) текущего профиля?\nОчередь Telegram, сессии и API-реквизиты не меняются. Текущие настройки будут сохранены отдельной копией."):
                if not self.save():
                    return
                restore_backup(self.directory, path)
                self.load_profile_view()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, 'Копия не восстановлена', str(exc))

    def create_shortcut(self):
        try:
            path = create_desktop_shortcut(self.root_directory)
        except OSError as exc:
            QMessageBox.warning(self, 'Ярлык', str(exc))
            return
        QMessageBox.information(self, 'Ярлык создан', f'Ярлык сохранён:\n{path}\n\nОставьте папку программы на текущем месте. После переноса создайте ярлык заново.')

    def page(self, title):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.tabs.addTab(page, title)
        return layout

    def timezone(self):
        name = self.zone.currentData()
        return QTimeZone.systemTimeZone() if name == 'system' else QTimeZone(str(name).encode())

    def change_timezone(self):
        if hasattr(self, 'start'):
            instant = self.start.dateTime().toSecsSinceEpoch()
            self.start.setTimeZone(self.timezone())
            self.start.setDateTime(QDateTime.fromSecsSinceEpoch(instant, self.timezone()))
        if hasattr(self, 'queue_rows'):
            for i, row in enumerate(self.queue_rows):
                if i < self.queue.rowCount():
                    self.queue.setItem(i, 2, QTableWidgetItem(self.format_time(row['stamp'])))

    def format_time(self, stamp):
        return QDateTime.fromSecsSinceEpoch(stamp, self.timezone()).toString('dd.MM.yyyy HH:mm:ss t')

    def refresh_profiles(self, ident=None):
        self.profile.clear()
        for p in self.profiles.active_profiles():
            self.profile.addItem(p['name'], p['id'])
        self.profile.setCurrentIndex(self.profile.findData(ident or self.profiles.data['active']))

    def add_profile(self):
        name, ok = QInputDialog.getText(self, 'Новый аккаунт', 'Название для списка аккаунтов:')
        if ok and name.strip():
            ident = self.profiles.add(name)
            self.refresh_profiles(ident)
            self.switch_profile()

    def switch_profile(self):
        ident = self.profile.currentData()
        if ident == self.profiles.data['active']:
            return
        self.save()
        self.pending_profile = ident
        self.run('switch_profile', str(self.profiles.path(ident)))

    def remove_profile(self):
        ident = self.profile.currentData()
        if len(self.profiles.active_profiles()) <= 1:
            QMessageBox.information(self, 'Аккаунты', 'Сначала добавьте другой аккаунт.')
            return
        if not self.confirm('Убрать выбранный аккаунт из программы? Его данные сохранятся для восстановления.\n'
                            'Серверные повторы продолжат работать. Чтобы остановить их, сначала удалите нужные отложки.'):
            return
        self.save()
        previous = self.profiles.data['active']
        self.profiles.remove(ident)
        replacement = self.profiles.data['active']
        # Keep the old active pointer until the worker has safely switched.
        self.profiles.data['active'] = previous
        self.refresh_profiles(replacement)
        self.switch_profile()

    def restore_profile(self):
        removed = [p for p in self.profiles.data['profiles'] if p.get('removed')]
        if not removed:
            QMessageBox.information(self, 'Аккаунты', 'Удалённых профилей нет.')
            return
        labels = [f"{p['name']} ({p['id'][:8]})" for p in removed]
        value, ok = QInputDialog.getItem(self, 'Восстановление', 'Выберите профиль:', labels, 0, False)
        if ok:
            ident = removed[labels.index(value)]['id']
            self.profiles.restore(ident)
            self.refresh_profiles(ident)
            self.switch_profile()

    def purge_profile(self):
        removed = [p for p in self.profiles.data['profiles'] if p.get('removed') and p['id'] != self.profiles.data['active']]
        if not removed:
            QMessageBox.information(self, 'Удаление данных', 'Сначала переключитесь на другой аккаунт и удалите нужный профиль из списка.')
            return
        labels = [f"{p['name']} ({p['id'][:8]})" for p in removed]
        selected, ok = QInputDialog.getItem(self, 'Удаление данных', 'Выберите ранее удалённый профиль:', labels, 0, False)
        if ok and self.confirm('Необратимо стереть локальную сессию, настройки, шаблоны, базу и её резервные копии выбранного профиля?\nСерверные повторы останутся в Telegram. Общие API-реквизиты сохранятся.'):
            try:
                self.profiles.purge(removed[labels.index(selected)]['id'])
                self.log('Локальные данные выбранного профиля удалены.')
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, 'Удаление данных', str(exc))

    def load_profile_view(self):
        backup_before_upgrade(self.profiles.path(self.profiles.data['active']))
        self.directory = self.profiles.path(self.profiles.data['active'])
        self.config_path = self.directory / 'settings.json'
        self.config = load_settings(self.config_path)
        remember_versions(self.config)
        self.loaded, self.auth_stage = False, None
        self.notifications.setChecked(self.config.get('notifications', True))
        self.plan, self.changes, self.queue_rows = [], [], []
        self.cleanup_rows, self.cleanup_display_rows = [], []
        self.cleanup_selected_ids.clear()
        self.chats.setRowCount(0)
        self.queue.setRowCount(0)
        self.cleanup_table.setRowCount(0)
        self.cleanup_folder.clear()
        self.cleanup_folder.addItem('Все папки', 'all')
        self.cleanup_filter_status.setText('Список ещё не загружен.')
        self.preview.clear()
        self.change_preview.clear()
        self.api_hash.clear()
        self.phone.clear()
        self.api_id.setText(str(self.api_config.get('api_id', '')))
        self.pause.setValue(self.config.get('pause', 2))
        self.create_pause.setValue(self.config.get('create_pause', 2))
        self.margin.setValue(self.config.get('margin', 60))
        self.test_mode.setChecked(self.config.get('test_mode', True))
        self.zone.setCurrentIndex(max(0, self.zone.findData(self.config.get('timezone', 'system'))))
        self.template.blockSignals(True)
        self.text.blockSignals(True)
        self.template_format.blockSignals(True)
        self.template.clear()
        self.template.addItems(self.config['templates'])
        self.text.setPlainText(self.config['templates'].get(self.template.currentText(), ''))
        self.template_format.setCurrentIndex(max(0, self.template_format.findData(self.config.get('template_formats', {}).get(self.template.currentText(), 'plain'))))
        self.template.blockSignals(False)
        self.text.blockSignals(False)
        self.template_format.blockSignals(False)
        self.select_template(self.template.currentText())
        self.status.setText('Аккаунт переключён. Подключите его, затем загрузите группы.')
        self.diagnostics.clear()
        self.tabs.setCurrentIndex(0)
        QTimer.singleShot(0, self.auto_restore)

    def auto_restore(self):
        if self.settings_error:
            self.log(self.settings_error)
            return
        if '--smoke-test' not in sys.argv and self.api_config and (Vault(self.directory).exists('session') or (self.directory / 'account.session').exists()):
            self.run('connect', '', '', '', self.pause.value(), True)

    def button(self, text, fn, layout):
        btn = QPushButton(text)
        btn.clicked.connect(fn)
        layout.addWidget(btn)
        self.controls.append(btn)
        return btn

    def auth_page(self):
        layout = self.page('1. Аккаунт')
        profile_row = QHBoxLayout()
        self.profile = QComboBox()
        for p in self.profiles.active_profiles():
            self.profile.addItem(p['name'], p['id'])
        self.profile.setCurrentIndex(self.profile.findData(self.profiles.data['active']))
        profile_row.addWidget(self.profile, 1)
        self.button('Переключить', self.switch_profile, profile_row)
        self.button('Добавить аккаунт', self.add_profile, profile_row)
        self.button('Удалить из списка…', self.remove_profile, profile_row)
        self.button('Вернуть удалённый…', self.restore_profile, profile_row)
        self.button('Стереть данные…', self.purge_profile, profile_row)
        layout.addLayout(profile_row)
        label = QLabel('API ID и API Hash вводятся один раз на вкладке 8 «Общие настройки».\n'
                       'Рекомендуется вход по QR-коду: он не требует SMS или кода в служебном чате. Сессия и API hash сохраняются через Windows DPAPI или macOS Keychain.\n'
                       'У каждого профиля свои чаты, шаблоны и планы. При переключении отправки не запускаются.')
        label.setWordWrap(True)
        layout.addWidget(label)
        form = QFormLayout()
        self.api_id = QLineEdit(str(self.api_config.get('api_id', '')))
        self.api_hash = QLineEdit()
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_hash.setPlaceholderText('Пусто — использовать защищённое сохранённое значение')
        self.phone = QLineEdit()
        self.phone.setPlaceholderText('+7…')
        self.pause = QDoubleSpinBox()
        self.pause.setRange(1, 60)
        self.pause.setValue(self.config.get('pause', 2))
        self.pause.setSuffix(' с')
        self.create_pause = QDoubleSpinBox()
        self.create_pause.setRange(1, 60)
        self.create_pause.setValue(self.config.get('create_pause', 2))
        self.create_pause.setSuffix(' с')
        self.margin = QSpinBox()
        self.margin.setRange(60, 86400)
        self.margin.setValue(self.config.get('margin', 60))
        self.margin.setSuffix(' с')
        self.zone = QComboBox()
        self.zone.addItem('Системный часовой пояс', 'system')
        self.zone.addItem('Москва (Europe/Moscow)', 'Europe/Moscow')
        self.zone.addItem('UTC', 'UTC')
        for zone in QTimeZone.availableTimeZoneIds():
            name = bytes(zone).decode()
            if name not in ('Europe/Moscow', 'UTC'):
                self.zone.addItem(name, name)
        self.zone.setCurrentIndex(max(0, self.zone.findData(self.config.get('timezone', 'system'))))
        self.zone.currentIndexChanged.connect(self.change_timezone)
        self.test_mode = QCheckBox('Тестовый режим: только одно изменение в одной группе за подтверждение')
        self.test_mode.setChecked(self.config.get('test_mode', True))
        for label, widget in [('Телефон', self.phone), ('Пауза обычных запросов', self.pause),
                              ('Пауза создания отложек', self.create_pause), ('Запас к кулдауну (минимум 60 с)', self.margin), ('Часовой пояс', self.zone)]:
            form.addRow(label, widget)
        layout.addLayout(form)
        layout.addWidget(self.test_mode)
        self.button('Сохранить настройки', self.save, layout)
        self.button('Войти по QR-коду (рекомендуется)', self.connect_qr_account, layout)
        self.button('Войти / подключить сохранённую сессию', self.connect_account, layout)
        self.button('Повторить ввод кода / пароля 2FA', self.auth_input, layout)
        layout.addWidget(QLabel('Повтор поддерживается только при совместимой библиотеке и Telegram Premium.\n'
                                'Ограничения сервера обязательны; пауза не гарантирует отсутствие FLOOD_WAIT.'))
        layout.addStretch()

    def chats_page(self):
        layout = self.page('2. Группы и разрешения')
        layout.addWidget(QLabel('Выберите группы галочками. Разрешение администратора подтверждаете вы; правила Telegram API не сообщает.\n'
                                'Интервал = max(15 мин, slow mode, ваше ограничение) + выбранный запас (по умолчанию 60 с). Форумы и каналы исключены из MVP.'))
        self.button('Загрузить / обновить группы Telegram', lambda: self.run('dialogs'), layout)
        self.chat_search = QLineEdit()
        self.chat_search.setPlaceholderText('Поиск по названию чата…')
        self.chat_search.setClearButtonEnabled(True)
        layout.addWidget(self.chat_search)
        self.chat_filter_status = QLabel()
        layout.addWidget(self.chat_filter_status)
        self.chats = QTableWidget(0, 10)
        self.chats.cellDoubleClicked.connect(self.chat_status_details)
        self.chats.setHorizontalHeaderLabels(['Выбрать', 'Группа', 'Разрешение', 'Мин. интервал, мин', 'Правила / основание', 'Шаблон', 'Slow mode, с', 'Статус', 'ID группы', 'Участников'])
        self.chats.horizontalHeader().moveSection(9, 2)
        self.chat_search.textChanged.connect(self.filter_chats)
        self.chats.itemChanged.connect(self.filter_chats)
        self.chats.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.chats.setColumnWidth(1, 230)
        self.chats.setColumnWidth(2, 110)
        self.chats.setColumnWidth(3, 150)
        layout.addWidget(self.chats)
        self.button('Сохранить список и правила', self.save, layout)
        self.button('Часы публикации и чередование для выбранных групп…', self.campaign_settings, layout)

    def filter_chats(self, *_):
        query = self.chat_search.text().strip().casefold()
        visible = selected = hidden_selected = 0
        for row in range(self.chats.rowCount()):
            title = self.chats.item(row, 1)
            match = bool(title) and query in title.text().casefold()
            self.chats.setRowHidden(row, not match)
            visible += int(match)
            checkbox = self.chats.item(row, 0)
            checked = bool(checkbox) and checkbox.checkState() == Qt.CheckState.Checked
            selected += int(checked)
            hidden_selected += int(checked and not match)
        self.chat_filter_status.setText(f'Показано: {visible} из {self.chats.rowCount()}. Выбрано: {selected}, из них скрыто поиском: {hidden_selected}. Поиск не снимает выбор групп.')

    def account_cleanup_page(self):
        layout = self.page('10. Очистка чатов')
        layout.addWidget(QLabel(
            'Здесь можно покинуть группы и каналы или удалить личные диалоги из текущего Telegram-аккаунта.\n'
            'Зажмите мышь или используйте Ctrl/Shift для быстрого выделения строк, затем «Отметить выделенные». '
            'Отметки не сбрасываются из-за поиска, сортировки, смены папки или клика вне списка.'))
        self.button('Загрузить / обновить все чаты аккаунта', lambda: self.run('account_dialogs'), layout)

        filters = QHBoxLayout()
        self.cleanup_search = QLineEdit()
        self.cleanup_search.setPlaceholderText('Поиск по названию…')
        self.cleanup_search.setClearButtonEnabled(True)
        filters.addWidget(self.cleanup_search, 2)
        self.cleanup_kind = QComboBox()
        for title, value in [('Все типы', 'all'), ('Группы', 'group'), ('Каналы', 'channel'),
                             ('Личные чаты', 'private'), ('Боты', 'bot')]:
            self.cleanup_kind.addItem(title, value)
        filters.addWidget(self.cleanup_kind)
        self.cleanup_folder = QComboBox()
        self.cleanup_folder.addItem('Все папки', 'all')
        filters.addWidget(self.cleanup_folder)
        self.cleanup_sort = QComboBox()
        for title, value in [('Недавняя активность сначала', 'activity_desc'),
                             ('Старая активность сначала', 'activity_asc'),
                             ('Название А–Я', 'title_asc'), ('Название Я–А', 'title_desc'),
                             ('Больше участников сначала', 'members_desc'),
                             ('Меньше участников сначала', 'members_asc'),
                             ('По папкам', 'folder_asc')]:
            self.cleanup_sort.addItem(title, value)
        filters.addWidget(self.cleanup_sort, 2)
        layout.addLayout(filters)

        actions = QHBoxLayout()
        self.button('Отметить выделенные', lambda: self.mark_cleanup_highlighted(True), actions)
        self.button('Снять с выделенных', lambda: self.mark_cleanup_highlighted(False), actions)
        self.button('Отметить все видимые', lambda: self.mark_cleanup_visible(True), actions)
        self.button('Снять все отметки', self.clear_cleanup_selection, actions)
        layout.addLayout(actions)
        self.cleanup_filter_status = QLabel('Список ещё не загружен.')
        layout.addWidget(self.cleanup_filter_status)

        self.cleanup_table = PersistentSelectionTable(0, 7)
        self.cleanup_table.setHorizontalHeaderLabels(['Отметить', 'Название', 'Тип', 'Последняя активность', 'Участников', 'Папки', 'ID Telegram'])
        self.cleanup_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cleanup_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.cleanup_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cleanup_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in [(0, 90), (2, 115), (3, 190), (4, 110), (5, 180), (6, 130)]:
            self.cleanup_table.setColumnWidth(column, width)
        layout.addWidget(self.cleanup_table)
        self.cleanup_search.textChanged.connect(self.filter_cleanup_dialogs)
        self.cleanup_kind.currentIndexChanged.connect(self.filter_cleanup_dialogs)
        self.cleanup_folder.currentIndexChanged.connect(self.filter_cleanup_dialogs)
        self.cleanup_sort.currentIndexChanged.connect(self.render_cleanup_dialogs)
        self.cleanup_table.itemChanged.connect(self.remember_cleanup_checkbox)
        self.button('Покинуть / удалить отмеченные диалоги…', self.prepare_dialog_cleanup, layout)

    def render_cleanup_dialogs(self, *_):
        self.cleanup_display_rows = sort_dialogs(self.cleanup_rows, self.cleanup_sort.currentData())
        self.cleanup_table.blockSignals(True)
        self.cleanup_table.setRowCount(len(self.cleanup_display_rows))
        for row_number, row in enumerate(self.cleanup_display_rows):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if row['id'] in self.cleanup_selected_ids else Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, row['id'])
            self.cleanup_table.setItem(row_number, 0, check)
            count = row.get('participants_count')
            values = [row['title'], KIND_NAMES.get(row.get('kind'), row.get('kind', '')),
                      self.format_time(row['last_activity']) if row.get('last_activity') else 'Нет даты',
                      str(count) if count is not None else '—', ', '.join(row.get('folder_names', [])), str(row['id'])]
            for column, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(value)
                self.cleanup_table.setItem(row_number, column, item)
        self.cleanup_table.blockSignals(False)
        self.filter_cleanup_dialogs()

    def remember_cleanup_checkbox(self, item):
        if item.column() != 0:
            return
        ident = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.cleanup_selected_ids.add(ident)
        else:
            self.cleanup_selected_ids.discard(ident)
        self.filter_cleanup_dialogs()

    def filter_cleanup_dialogs(self, *_):
        visible = 0
        visible_ids = set()
        for index, row in enumerate(self.cleanup_display_rows):
            show = dialog_matches(row, self.cleanup_search.text(), self.cleanup_kind.currentData(), self.cleanup_folder.currentData())
            self.cleanup_table.setRowHidden(index, not show)
            if show:
                visible += 1
                visible_ids.add(row['id'])
        hidden_selected = len(self.cleanup_selected_ids - visible_ids)
        self.cleanup_filter_status.setText(
            f'Показано: {visible} из {len(self.cleanup_rows)}. Отмечено к очистке: {len(self.cleanup_selected_ids)}, '
            f'из них скрыто фильтрами: {hidden_selected}.')

    def mark_cleanup_highlighted(self, checked):
        rows = [index.row() for index in self.cleanup_table.selectionModel().selectedRows()
                if not self.cleanup_table.isRowHidden(index.row())]
        identifiers = {self.cleanup_table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in rows}
        if checked:
            self.cleanup_selected_ids.update(identifiers)
        else:
            self.cleanup_selected_ids.difference_update(identifiers)
        self.cleanup_table.blockSignals(True)
        for row in rows:
            self.cleanup_table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.cleanup_table.blockSignals(False)
        self.filter_cleanup_dialogs()

    def mark_cleanup_visible(self, checked):
        rows = [row for row in range(self.cleanup_table.rowCount()) if not self.cleanup_table.isRowHidden(row)]
        identifiers = {self.cleanup_table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in rows}
        if checked:
            self.cleanup_selected_ids.update(identifiers)
        else:
            self.cleanup_selected_ids.difference_update(identifiers)
        self.cleanup_table.blockSignals(True)
        for row in rows:
            self.cleanup_table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.cleanup_table.blockSignals(False)
        self.filter_cleanup_dialogs()

    def clear_cleanup_selection(self):
        self.cleanup_selected_ids.clear()
        self.render_cleanup_dialogs()

    def prepare_dialog_cleanup(self):
        rows = [row for row in self.cleanup_rows if row['id'] in self.cleanup_selected_ids]
        if not rows:
            QMessageBox.information(self, 'Очистка', 'Сначала отметьте чаты. Можно быстро выделить строки мышью и нажать «Отметить выделенные».')
            return
        detail = '\n'.join(f"• {row['title']} — {KIND_NAMES.get(row['kind'], row['kind'])} — "
                           f"{self.format_time(row['last_activity']) if row.get('last_activity') else 'нет даты'}"
                           for row in sort_dialogs(rows, 'title_asc'))
        warning = (f'Очистить {len(rows)} отмеченных диалогов в ТЕКУЩЕМ Telegram-аккаунте?\n\n'
                   '• Из групп и каналов аккаунт выйдет.\n'
                   '• Личные диалоги и боты будут удалены из списка вместе с локальной историей; у собеседника история останется.\n'
                   '• Операция идёт последовательно; её можно остановить, но уже подтверждённые Telegram действия не откатываются.\n'
                   f'• Скрытые текущими фильтрами отмеченные строки тоже войдут в операцию.\n\n{detail}')
        if self.confirm(warning):
            self.run('cleanup_dialogs', rows)

    def template_page(self):
        layout = self.page('3. Шаблоны')
        row = QHBoxLayout()
        self.template = QComboBox()
        self.template.addItems(self.config['templates'])
        row.addWidget(self.template, 1)
        self.button('Новый шаблон', self.new_template, row)
        self.button('Сохранить шаблон', self.save_template, row)
        self.button('Удалить шаблон', self.delete_template, row)
        layout.addLayout(row)
        imports = QHBoxLayout()
        self.button('Импортировать из Telegram', lambda: self.run('saved_messages'), imports)
        self.button('Копировать в другой аккаунт', self.copy_template, imports)
        layout.addLayout(imports)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText('Ваше предложение о сотрудничестве. HTML действует только при выбранном формате HTML.')
        layout.addWidget(self.text)
        format_row = QHBoxLayout()
        self.template_format = QComboBox()
        self.template_format.addItem('Обычный текст', 'plain')
        self.template_format.addItem('Telegram HTML: <b>, <i>, <u>, <s>, <code>, <pre>, <a>', 'html')
        self.template_format.addItem('Оригинал из Telegram (защищён от правок)', 'telegram')
        self.template_format.setCurrentIndex(max(0, self.template_format.findData(self.config.get('template_formats', {}).get(self.template.currentText(), 'plain'))))
        format_row.addWidget(self.template_format)
        self.button('Предпросмотр текста', self.preview_template, format_row)
        self.button('История версий / восстановить', self.template_history, format_row)
        layout.addLayout(format_row)
        self.text.setPlainText(self.config['templates'].get(self.template.currentText(), ''))
        self.template.currentTextChanged.connect(self.select_template)
        self.text.textChanged.connect(self.remember_template)
        self.template_format.currentIndexChanged.connect(self.remember_template)
        self.template_info = QLabel()
        self.template_info.setWordWrap(True)
        layout.addWidget(self.template_info)
        self.select_template(self.template.currentText())
        layout.addWidget(QLabel('Сохранение шаблона не меняет Telegram. Для обновления очереди используйте вкладку 5.'))
        actions = QHBoxLayout()
        self.button('Предпросмотр обновления связанных сообщений', self.prepare_updates, actions)
        self.button('Предпросмотр незавершённых изменений', lambda: self.run('pending_changes', self.selected_chats()), actions)
        self.button('Применить просмотренные изменения…', self.apply_updates, actions)
        layout.addLayout(actions)
        self.change_preview = QPlainTextEdit()
        self.change_preview.setReadOnly(True)
        layout.addWidget(self.change_preview)

    def schedule_page(self):
        layout = self.page('4. Расписание')
        form = QFormLayout()
        self.start = QDateTimeEdit(QDateTime.currentDateTime().addSecs(600))
        self.start.setDisplayFormat('dd.MM.yyyy HH:mm:ss')
        self.start.setCalendarPopup(True)
        self.change_timezone()
        self.mode = QComboBox()
        self.mode.addItems(['Каждый день — серверный повтор, Premium', 'Один раз — только ближайшие 24 часа, БЕЗ повтора'])
        self.cap = QSpinBox()
        self.cap.setRange(1, 100)
        self.cap.setValue(100)
        form.addRow('Первый слот (выбранный часовой пояс)', self.start)
        form.addRow('Режим', self.mode)
        form.addRow('Собственный предел общей очереди в группе', self.cap)
        layout.addLayout(form)
        layout.addWidget(QLabel('100 — защитный предел приложения, не обещание квоты Telegram. Сервер может остановить заполнение раньше.\n'
                                'Кнопка заполнения подготовит максимум доступных ежедневных слотов, сохранив существующие отложки.'))
        self.fill_status = QLabel()
        layout.addWidget(self.fill_status)
        self.test_mode.toggled.connect(self.update_fill_status)
        self.cap.valueChanged.connect(self.update_fill_status)
        self.update_fill_status()
        self.button('Заполнить очередь на 24 часа — показать полный план', self.prepare_full_queue, layout)
        self.button('Проверить готовность без создания сообщений', self.preflight, layout)
        actions = QHBoxLayout()
        self.button('Подготовить и показать план', self.prepare, actions)
        self.execute_button = self.button('Подтвердить создание плана…', self.execute, actions)
        self.button('Продолжить сохранённый план…', self.resume, actions)
        layout.addLayout(actions)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview)
        self.button('Сбросить локальный план при пустой очереди…', self.clear_progress, layout)

    def queue_page(self):
        layout = self.page('5. Очередь Telegram')
        layout.addWidget(QLabel('Здесь реальные сообщения из выбранных групп, включая созданные вручную. Выделите строки (Ctrl/Shift).\n'
                                'Редактируйте одно сообщение или замените несколько выбранным шаблоном. Даты и повторы сохраняются.'))
        row = QHBoxLayout()
        self.button('Обновить очередь выбранных групп', lambda: self.run('queues', self.selected_chats()), row)
        self.button('Выделить все ежедневные', self.select_daily, row)
        layout.addLayout(row)
        edits = QHBoxLayout()
        self.button('Редактировать сообщение…', self.edit_scheduled, edits)
        self.button('Заменить выделенные шаблоном…', lambda: self.mutate('edit'), edits)
        self.button('Удалить выделенные отложенные…', lambda: self.mutate('delete'), edits)
        self.button('Перенести время…', self.shift_scheduled, edits)
        layout.addLayout(edits)
        more = QHBoxLayout()
        self.button('Привязать выделенные к открытому шаблону…', self.bind_rows, more)
        self.button('Включить ежедневный повтор…', lambda: self.mutate('repeat_daily'), more)
        self.button('Отключить повтор (разовые)…', lambda: self.mutate('repeat_once'), more)
        layout.addLayout(more)
        filters = QHBoxLayout()
        self.queue_search = QLineEdit()
        self.queue_search.setPlaceholderText('Поиск по группе, тексту, шаблону или ID')
        filters.addWidget(self.queue_search)
        self.queue_kind = QComboBox()
        for title, value in [('Все', 'all'), ('Ежедневные', 'daily'), ('Разовые', 'once'), ('Требуют проверки', 'error')]:
            self.queue_kind.addItem(title, value)
        filters.addWidget(self.queue_kind)
        layout.addLayout(filters)
        self.queue_filter_status = QLabel()
        layout.addWidget(self.queue_filter_status)
        self.queue = QTableWidget(0, 7)
        self.queue.setHorizontalHeaderLabels(['Группа', 'ID Telegram', 'Дата ↑', 'Повтор, с', 'Текст', 'Связанный шаблон', 'Статус'])
        self.queue.horizontalHeaderItem(1).setToolTip('Идентификатор Telegram, не порядковый номер. Очередь показана от ближайшей даты к дальней; ID могут идти не подряд.')
        self.queue.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue.cellDoubleClicked.connect(lambda *_: self.edit_scheduled())
        self.queue.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for column, width in [(0, 150), (1, 105), (2, 170), (3, 85), (5, 140), (6, 170)]:
            self.queue.setColumnWidth(column, width)
        layout.addWidget(self.queue)
        self.queue_search.textChanged.connect(self.filter_queue)
        self.queue_kind.currentIndexChanged.connect(self.filter_queue)

    def chat_status_details(self, row, column):
        if column == 7 and self.chats.item(row, column):
            QMessageBox.information(self, 'Статус группы', self.chats.item(row, column).text())

    def filter_queue(self):
        visible = 0
        for i, row in enumerate(self.queue_rows):
            show = queue_matches(row, self.queue_search.text(), self.queue_kind.currentData())
            self.queue.setRowHidden(i, not show)
            visible += int(show)
            if not show:
                from PySide6.QtCore import QItemSelectionModel
                self.queue.selectionModel().select(self.queue.model().index(i, 0), QItemSelectionModel.SelectionFlag.Deselect | QItemSelectionModel.SelectionFlag.Rows)
        self.queue_filter_status.setText(f'Показано {visible} из {len(self.queue_rows)}. Скрытые строки не участвуют в действиях.')

    def selected_chats(self):
        result = []
        for row in range(self.chats.rowCount()):
            item = self.chats.item(row, 0)
            if item.checkState() == Qt.CheckState.Checked:
                result.append({**self.config['chats'].get(str(item.data(Qt.ItemDataRole.UserRole)), {}), 'id': item.data(Qt.ItemDataRole.UserRole), 'title': self.chats.item(row, 1).text(),
                    'allowed': self.chats.item(row, 2).checkState() == Qt.CheckState.Checked,
                    'manual': self.chats.cellWidget(row, 3).value(), 'rules': self.chats.item(row, 4).text(),
                    'template_id': self.chats.cellWidget(row, 5).currentText()})
        return result

    def save(self):
        for row in range(self.chats.rowCount()):
            item = self.chats.item(row, 0)
            self.config['chats'][str(item.data(Qt.ItemDataRole.UserRole))] = {
                **self.config['chats'].get(str(item.data(Qt.ItemDataRole.UserRole)), {}),
                'selected': item.checkState() == Qt.CheckState.Checked,
                'allowed': self.chats.item(row, 2).checkState() == Qt.CheckState.Checked,
                'manual': self.chats.cellWidget(row, 3).value(), 'rules': self.chats.item(row, 4).text(),
                'template_id': self.chats.cellWidget(row, 5).currentText()}
        if self.api_hash.text().strip() or (self.api_config and self.api_id.text().strip() != str(self.api_config.get('api_id'))):
            try:
                self.application_settings.save_credentials(self.api_id.text().strip(), self.api_hash.text().strip())
                self.api_config = self.application_settings.credentials()
                self.api_hash.clear()
                self.settings_error = None
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, 'Общие настройки', str(exc) if isinstance(exc, ValueError) else 'Windows не смогла сохранить секреты. Исходные файлы сохранены.')
                return False
        remember_versions(self.config)
        self.config['margin'] = self.margin.value()
        self.config['pause'] = self.pause.value()
        self.config['create_pause'] = self.create_pause.value()
        self.config['timezone'] = self.zone.currentData()
        self.config['test_mode'] = self.test_mode.isChecked()
        self.config['notifications'] = self.notifications.isChecked()
        save_json(self.config_path, self.config)
        return True

    def select_template(self, name):
        self.text.blockSignals(True)
        self.template_format.blockSignals(True)
        self.text.setPlainText(self.config['templates'].get(name, ''))
        self.template_format.setCurrentIndex(max(0, self.template_format.findData(self.config.get('template_formats', {}).get(name, 'plain'))))
        self.text.blockSignals(False)
        self.template_format.blockSignals(False)
        self.template_lock()

    def offers(self):
        return {name: {'text': text, 'format': self.config.get('template_formats', {}).get(name, 'plain'),
                       'entities': self.config.get('template_entities', {}).get(name, '[]')}
                for name, text in self.config['templates'].items()}

    def template_lock(self):
        imported = self.template_format.currentData() == 'telegram'
        self.text.setReadOnly(imported)
        self.template_format.setEnabled(not imported and not self.busy)
        raw = self.config.get('template_entities', {}).get(self.template.currentText(), '[]')
        try:
            count = sum(e.get('_') == 'MessageEntityCustomEmoji' for e in json.loads(raw)) if imported else 0
        except (ValueError, TypeError):
            count = 0
        self.template_info.setText(
            f'Кастомных эмодзи: {count}. Анимация отображается в Telegram. Для правки измените сообщение в «Избранном» и импортируйте заново. Доступность эмодзи зависит от аккаунта и Premium.'
            if imported else 'Обычный редактор. Для сохранения кастомных эмодзи используйте импорт из Telegram.')

    def install_template(self, name, offer):
        parse_offer(offer)
        remember_versions(self.config)
        self.config['templates'][name] = offer['text']
        self.config.setdefault('template_formats', {})[name] = offer['format']
        self.config.setdefault('template_entities', {})[name] = offer.get('entities', '[]')
        if self.template.findText(name) < 0:
            self.template.addItem(name)
        self.template.setCurrentText(name)
        self.select_template(name)
        self.refresh_template_choices()
        self.save()

    def choose_saved_message(self, messages):
        if not messages:
            QMessageBox.information(self, 'Избранное', 'Среди последних 100 сообщений нет текстовых сообщений. Отправьте готовый текст в «Избранное» и повторите импорт. Фото, видео и файлы не импортируются.')
            return
        labels = [f"{m['id']} • {self.format_time(m['date'])} • {m['text'][:100].replace(chr(10), ' ')}" for m in messages]
        selected, ok = QInputDialog.getItem(self, 'Импортировать из Telegram', 'Текстовые сообщения из последних 100 сообщений «Избранного»:', labels, 0, False)
        if not ok:
            return
        offer = messages[labels.index(selected)]
        name, ok = QInputDialog.getText(self, 'Импорт шаблона', 'Название шаблона (существующее — обновить):', text=self.template.currentText())
        name = name.strip()
        if not ok or not name:
            return
        if name in self.config['templates'] and not self.confirm('Заменить локальный шаблон «' + name + '»? Очередь Telegram пока не изменится.'):
            return
        self.install_template(name, offer)
        self.log('Оригинальный текст и форматирование импортированы из «Избранного».')

    def copy_template(self):
        profiles = [p for p in self.profiles.active_profiles() if p['id'] != self.profiles.data['active']]
        if not profiles:
            QMessageBox.information(self, 'Аккаунты', 'Сначала добавьте второй профиль на вкладке 1.')
            return
        labels = [f"{p['name']} ({p['id']})" for p in profiles]
        selected, ok = QInputDialog.getItem(self, 'Копировать шаблон', 'Профиль получателя:', labels, 0, False)
        if not ok:
            return
        name = self.template.currentText()
        offer = self.offers()[name]
        parse_offer(offer)
        path = self.profiles.path(profiles[labels.index(selected)]['id']) / 'settings.json'
        config = load_settings(path)
        if name in config['templates'] and not self.confirm('Заменить одноимённый шаблон в выбранном профиле? Telegram не изменится.'):
            return
        remember_versions(config)
        config['templates'][name] = offer['text']
        config.setdefault('template_formats', {})[name] = offer['format']
        config.setdefault('template_entities', {})[name] = offer['entities']
        remember_versions(config)
        save_json(path, config)
        self.log('Шаблон с форматированием скопирован в выбранный профиль. Публикация не выполнялась.')

    def preview_template(self):
        try:
            text, entities = parse_offer(self.offers()[self.template.currentText()])
        except ValueError as exc:
            QMessageBox.warning(self, 'Шаблон', str(exc))
            return
        box = QMessageBox(self)
        box.setWindowTitle('Предпросмотр текста Telegram')
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(telegram_html.unparse(text, entities).replace('\n', '<br>'))
        box.exec()

    def template_history(self):
        name = self.template.currentText()
        history = self.config.get('template_versions', {}).get(name, [])
        if not history:
            return
        labels = [f"Версия {i+1}: {self.format_time(v['time'])} ({v.get('format', 'plain')})" for i,v in enumerate(history)]
        selected, ok = QInputDialog.getItem(self, 'История шаблона', 'Выберите версию для просмотра:', labels, len(labels)-1, False)
        if ok:
            value = history[labels.index(selected)]
            if self.confirm('Восстановить локальную версию? Telegram пока не изменится.\n\n' + value['text']):
                self.install_template(name, {**value, 'format': value.get('format', 'plain')})

    def remember_template(self):
        name = self.template.currentText()
        if name:
            if self.template_format.currentData() != 'telegram':
                self.config['templates'][name] = self.text.toPlainText()
            self.config.setdefault('template_formats', {})[name] = self.template_format.currentData()
            self.template_lock()

    def refresh_template_choices(self):
        for row in range(self.chats.rowCount()):
            combo = self.chats.cellWidget(row, 5)
            old = combo.currentText()
            combo.clear()
            combo.addItems(self.config['templates'])
            if old in self.config['templates']:
                combo.setCurrentText(old)

    def save_template(self):
        # Qt normalizes some line separators even in read-only editors.
        # The imported source must never be reconstructed from the preview.
        if self.template_format.currentData() != 'telegram':
            self.config['templates'][self.template.currentText()] = self.text.toPlainText()
        self.save()
        self.log('Шаблон сохранён локально')

    def new_template(self):
        name, ok = QInputDialog.getText(self, 'Шаблон', 'Название:')
        if ok and name.strip() and name not in self.config['templates']:
            self.config['templates'][name] = ''
            self.config.setdefault('template_formats', {})[name] = 'plain'
            self.config.setdefault('template_entities', {})[name] = '[]'
            self.template.addItem(name)
            self.template.setCurrentText(name)
            self.refresh_template_choices()
            self.save()

    def delete_template(self):
        if any(c.get('template_id') == self.template.currentText() or self.template.currentText() in c.get('rotation', []) for c in self.config['chats'].values()):
            QMessageBox.information(self, 'Шаблон используется', 'Сначала измените назначение шаблона в группах и сохраните список.')
            return
        if self.template.count() > 1 and self.confirm('Удалить локальный шаблон? Telegram не изменится.'):
            del self.config['templates'][self.template.currentText()]
            self.template.removeItem(self.template.currentIndex())
            self.refresh_template_choices()
            self.save()

    def run(self, method, *args):
        if self.busy:
            return
        if not self.worker.initialized.is_set():
            QMessageBox.information(self, 'Запуск', 'Дождитесь завершения запуска приложения.')
            return
        auth_methods = ('connect', 'connect_qr', 'wait_qr_login', 'login', 'switch_profile')
        if method not in auth_methods and not self.loaded:
            QMessageBox.information(self, 'Вход', 'Сначала подключите аккаунт на вкладке 1.')
            return
        if method in ('prepare', 'resume', 'queues', 'clear_progress', 'prepare_updates', 'pending_changes') and not args[0]:
            QMessageBox.information(self, 'Группы', 'Выберите хотя бы одну группу.')
            return
        self.busy = True
        self.progress_started = time.monotonic()
        self.progress_state = {}
        self.operation_progress.setRange(0, 0)
        if method in ('connect', 'connect_qr'):
            self.loaded = False
        self.enable_editing(False)
        self.status.setText('Выполняется… Прогресс во вкладке «Журнал». Можно остановить.')
        self.worker.options = {'pause': self.pause.value(), 'create_pause': self.create_pause.value(),
                               'test_mode': self.test_mode.isChecked(), 'margin': self.margin.value()}
        self.worker.submit(method, *args)

    def enable_editing(self, enabled):
        for button in self.controls:
            button.setEnabled(enabled)
        for widget in (self.api_id, self.api_hash, self.phone, self.pause, self.chats,
                       self.template, self.text, self.start, self.mode, self.cap, self.queue,
                       self.profile, self.create_pause, self.zone, self.test_mode, self.margin, self.template_format,
                       self.cleanup_table, self.cleanup_search, self.cleanup_kind, self.cleanup_folder, self.cleanup_sort):
            widget.setEnabled(enabled)
        self.template_lock()

    def connect_account(self):
        if not self.save():
            return
        if not self.api_config:
            QMessageBox.warning(self, 'API', 'Один раз сохраните API ID и API Hash во вкладке 8 «Общие настройки».')
            self.tabs.setCurrentIndex(7)
            return
        self.run('connect', '', '', self.phone.text().strip(), self.pause.value())

    def connect_qr_account(self):
        if not self.save():
            return
        if not self.api_config:
            QMessageBox.warning(self, 'API', 'Один раз сохраните API ID и API Hash во вкладке 8 «Общие настройки».')
            self.tabs.setCurrentIndex(7)
            return
        self.run('connect_qr', '', '', self.pause.value())

    @staticmethod
    def qr_pixmap(url, size=320):
        code = qrcode.QRCode(version=None, box_size=1, border=4)
        code.add_data(url)
        code.make(fit=True)
        matrix = code.get_matrix()
        width = len(matrix)
        image = QImage(width, width, QImage.Format.Format_RGB32)
        image.fill(0xffffffff)
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    image.setPixel(x, y, 0xff000000)
        return QPixmap.fromImage(image).scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)

    def show_qr_login(self, url):
        if self.qr_dialog:
            self.qr_dialog.close()
        dialog = QDialog(self)
        dialog.setWindowTitle('Вход в Telegram по QR-коду')
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        instruction = QLabel('На телефоне откройте Telegram → Настройки → Устройства → Подключить устройство, затем отсканируйте код.')
        instruction.setWordWrap(True)
        layout.addWidget(instruction)
        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setPixmap(self.qr_pixmap(url))
        layout.addWidget(image)
        layout.addWidget(QLabel('Не закрывайте это окно до завершения входа. QR-код действует недолго.'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.cancel)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        dialog.resize(420, 470)
        self.qr_dialog = dialog
        dialog.show()

    def auth_input(self):
        if self.auth_stage not in ('code', 'password'):
            QMessageBox.information(self, 'Вход', 'Сначала нажмите «Войти».')
            return
        password = self.auth_stage == 'password'
        value, ok = QInputDialog.getText(self, 'Локальная авторизация',
            'Пароль двухэтапной проверки:' if password else 'Код из Telegram / SMS:', QLineEdit.EchoMode.Password)
        if ok and value:
            self.run('login', None if password else value, value if password else None)

    def preflight(self):
        self.save()
        self.plan = []
        self.run('preflight', self.selected_chats(), self.offers(), self.start.dateTime().toSecsSinceEpoch(),
                 86400 if self.mode.currentIndex() == 0 else 0, self.cap.value(), self.pause.value())

    def prepare(self):
        self.save()
        self.plan = []
        self.run('prepare', self.selected_chats(), self.offers(),
                 self.start.dateTime().toSecsSinceEpoch(), 86400 if self.mode.currentIndex() == 0 else 0,
                 self.cap.value(), self.pause.value())

    def update_fill_status(self, *_):
        if self.test_mode.isChecked():
            message = 'Сейчас включён ТЕСТ: обычная подготовка создаст максимум 1 новый слот. Для полной очереди нажмите кнопку заполнения ниже.'
        elif self.cap.value() == 1:
            message = 'Предел очереди = 1: остальные слоты не будут созданы. Кнопка заполнения ниже установит полный предел.'
        else:
            message = f'Полный режим: расчёт всех доступных слотов. Предел общей очереди: {self.cap.value()}; существующие сообщения учитываются.'
        self.fill_status.setText(message)

    def prepare_full_queue(self):
        if not self.loaded:
            QMessageBox.information(self, 'Вход', 'Сначала подключите аккаунт и загрузите группы.')
            return
        if not self.selected_chats():
            QMessageBox.information(self, 'Группы', 'Выберите группу для заполнения очереди.')
            return
        self.test_mode.setChecked(False)
        self.cap.setValue(100)
        self.mode.setCurrentIndex(0)
        self.start.setDateTime(QDateTime.currentDateTime().addSecs(600).toTimeZone(self.timezone()))
        self.log('Подготовка полной очереди: тестовое ограничение снято, ежедневный повтор, начало не ранее чем через 10 минут. Создание — только после подтверждения плана.')
        self.prepare()

    def confirm(self, text):
        if len(text) > 1200 or 'БЫЛО:' in text:
            dialog = QDialog(self)
            dialog.setWindowTitle('Предпросмотр: было → станет')
            dialog.resize(850, 650)
            layout = QVBoxLayout(dialog)
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setPlainText(text)
            layout.addWidget(view)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Подтвердить изменения')
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            return dialog.exec() == QDialog.DialogCode.Accepted
        return QMessageBox.question(self, 'Подтверждение действия в Telegram', text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def execute(self):
        if not self.plan:
            QMessageBox.information(self, 'План', 'Сначала подготовьте план.')
            return
        count = sum(len(p['slots']) for p in self.plan)
        if self.confirm(f'Создать {count} сообщений по ПОКАЗАННОМУ плану в {len(self.plan)} группах?\n'
                        'Текст и настройки зафиксированы в предварительном просмотре.\n'
                        'Подтверждаю разрешение администраторов и соответствие правилам групп.'):
            self.run('execute')

    def resume(self):
        self.save()
        if self.confirm('Продолжить ранее сохранённые планы выбранных групп с прежним текстом и датами?\n'
                        'Новый шаблон при этом не применяется. Уже подтверждённые слоты не создаются заново.'):
            self.run('resume', self.selected_chats())

    def clear_progress(self):
        if self.confirm('Забыть локальный прогресс выбранных групп?\nРазрешено только при пустой очереди Telegram.'):
            self.run('clear_progress', self.selected_chats())

    def prepare_updates(self):
        self.save_template()
        self.changes = []
        self.run('prepare_updates', self.selected_chats(), self.offers())

    def apply_updates(self):
        if not self.changes:
            QMessageBox.information(self, 'Обновление', 'Сначала подготовьте предпросмотр изменений.')
            return
        if self.confirm(f'Применить {len(self.changes)} просмотренных изменений в Telegram?\n'
                        'Будут использованы тексты из предпросмотра. Даты и повторы сохраняются при замене текста.'):
            self.run('apply_prepared_changes')

    def selected_rows(self):
        return [dict(self.queue_rows[i.row()]) for i in self.queue.selectionModel().selectedRows() if not self.queue.isRowHidden(i.row())]

    def bind_rows(self):
        rows = self.selected_rows()
        if rows and self.confirm(f'Связать выбранные сообщения ({len(rows)}) с шаблоном «{self.template.currentText()}»?\n'
                                 'Подтверждайте только свои сообщения. Это локальная связь; Telegram не изменится.'):
            self.run('bind_rows', rows, self.template.currentText())

    def select_daily(self):
        from PySide6.QtCore import QItemSelectionModel
        self.queue.clearSelection()
        for i, row in enumerate(self.queue_rows):
            if row['repeat'] == 86400 and not self.queue.isRowHidden(i):
                self.queue.selectionModel().select(self.queue.model().index(i, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)

    def shift_scheduled(self):
        rows = self.selected_rows()
        if not rows:
            QMessageBox.information(self, 'Перенос', 'Выберите сообщения в очереди.')
            return
        if len(rows) == 1:
            dialog = QDialog(self)
            dialog.setWindowTitle('Новая дата и время отложки')
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel(f"{rows[0]['title']} / ID {rows[0]['id']}\nЧасовой пояс — как во вкладке расписания. Повтор сохраняется."))
            date = QDateTimeEdit()
            date.setCalendarPopup(True)
            date.setTimeZone(self.timezone())
            date.setDisplayFormat('dd.MM.yyyy HH:mm:ss')
            date.setDateTime(QDateTime.fromSecsSinceEpoch(rows[0]['stamp'], self.timezone()))
            layout.addWidget(date)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                delta = date.dateTime().toSecsSinceEpoch() - rows[0]['stamp']
                if delta:
                    self.run('prepare_shift', rows, delta)
            return
        minutes, ok = QInputDialog.getInt(self, 'Перенос времени',
            'Сдвиг в минутах для выбранных сообщений (отрицательный — раньше).\nСначала будет проверка и предпросмотр:', 16, -1439, 1439)
        if ok and minutes:
            self.run('prepare_shift', rows, minutes * 60)

    def edit_scheduled(self):
        if self.busy:
            return
        rows = self.selected_rows()
        if len(rows) != 1:
            QMessageBox.information(self, 'Редактирование', 'Выделите одно сообщение. Для нескольких используйте замену шаблоном.')
            return
        row = rows[0]
        if row.get('media'):
            QMessageBox.information(self, 'Редактирование', 'Редактирование сообщений с вложениями пока не поддерживается. Удаление доступно.')
            return
        text, ok = QInputDialog.getMultiLineText(self, 'Редактировать отложенное сообщение',
            f"{row['title']} / ID {row['id']}\nДата и повтор сохранятся. Новый текст:", row['text'])
        if not ok or text == row['text']:
            return
        offer = {'text': text, 'format': 'plain'}
        try:
            if decode(row.get('entities')):
                offer = edited_offer(row['text'], text, row.get('entities'))
            parse_offer(offer)
        except ValueError as exc:
            QMessageBox.warning(self, 'Текст', str(exc))
            return
        if self.confirm(f"Изменить сообщение в «{row['title']}», ID {row['id']}?\n\nБЫЛО:\n{row['text']}\n\nСТАНЕТ:\n{text}\n\nДата и ежедневный повтор сохранятся. Локальный шаблон не изменится."):
            self.run('mutate', rows, 'edit', offer)

    def mutate(self, action):
        selected = self.selected_rows()
        if not selected:
            QMessageBox.information(self, 'Очередь', 'Сначала обновите очередь и выделите нужные строки. Несколько сообщений — Ctrl/Shift.')
            return
        description = '\n\n'.join(f"{r['title']} / ID {r['id']} / {self.format_time(r['stamp'])}\nБЫЛО:\n{r['text']}\nПовтор: {r['repeat']} с; элементов форматирования: {len(decode(r.get('entities')))}" for r in selected)
        verb = {'edit': 'Заменить текст', 'delete': 'Удалить отложенные сообщения',
                'repeat_daily': 'Включить ежедневный повтор', 'repeat_once': 'Отключить повтор, оставить разовую отправку'}[action]
        offer, extra = '', ''
        if action == 'edit':
            names = list(self.config['templates'])
            name, ok = QInputDialog.getItem(self, 'Замена шаблоном', 'Шаблон для выбранных сообщений:', names, max(0, self.template.currentIndex()), False)
            if not ok:
                return
            offer = self.offers()[name]
            try:
                plain, entities = parse_offer(offer)
            except ValueError as exc:
                QMessageBox.warning(self, 'Шаблон', str(exc))
                return
            extra = f'\n\nСТАНЕТ ДЛЯ КАЖДОГО ВЫБРАННОГО СООБЩЕНИЯ (шаблон {name}):\n{plain}\nЭлементов форматирования: {len(entities)}. Даты и повторы сохранятся.'
        elif action == 'delete':
            extra = '\nСТАНЕТ: сообщения удалены из очереди вместе с будущими повторами. Уже опубликованные сообщения не удаляются.'
        if self.confirm(f'{verb}: {len(selected)} шт.?\n{description}{extra}\n\nЭто изменит реальную очередь Telegram.'):
            self.run('mutate', selected, action, offer)

    def cancel(self):
        if self.busy:
            self.worker.cancel.set()
            self.log('Запрошена остановка. Уже начатый запрос может завершиться на сервере.')

    def log(self, message):
        label = next((p['name'] for p in self.profiles.data['profiles']
                      if p['id'] == self.profiles.data['active']), 'Аккаунт')
        message = f'[{label}] {message}'
        logging.info(message)
        if self.busy:
            self.status.setText(message)
        self.journal.appendPlainText(f'{datetime.now():%H:%M:%S}  {message}')

    def result(self, result):
        self.busy = False
        if self.qr_dialog and result.get('auth') != 'qr':
            self.qr_dialog.close()
            self.qr_dialog = None
        self.operation_progress.setRange(0, max(1, self.progress_state.get('total', 0)))
        self.operation_progress.setValue(self.progress_state.get('completed', 0))
        self.progress_label.setText(('Остановлено: ' + result['error']) if 'error' in result else 'Операция завершена. Подробности — в журнале и соответствующем списке.')
        if 'error' in result or 'done' in result:
            self.notify_windows('Операция остановлена' if 'error' in result else 'Операция завершена', result.get('error') or result['done'])
        if 'preflight' in result:
            self.preview.setPlainText('\n\n'.join(result['preflight']) or 'Выберите группы для проверки.')
            self.tabs.setCurrentIndex(3)
        self.enable_editing(True)
        self.status.setText('Готово • ' + ('аккаунт подключён' if self.loaded else 'ожидается вход'))
        for report in result.get('chat_reports', []):
            self.log(f"Группа {report['chat']}: {report['status']}")
            for row in range(self.chats.rowCount()):
                if self.chats.item(row, 0).data(Qt.ItemDataRole.UserRole) == report['chat']:
                    self.chats.setItem(row, 7, QTableWidgetItem(report['status']))
                    if 'slow' in report:
                        self.chats.setItem(row, 6, QTableWidgetItem(str(report['slow'])))
        if 'shift_changes' in result:
            changes = result['shift_changes']
            detail = '\n\n'.join(f"{c['row']['title']} / ID {c['row']['id']}\n{c['row']['text']}\nБЫЛО: {self.format_time(c['row']['stamp'])}\nСТАНЕТ: {self.format_time(c['target_stamp'])}\nПовтор: {c['row']['repeat']} с (сохраняется)" for c in changes)
            if self.confirm('Перенести эти сообщения в Telegram?\n\n' + detail):
                self.run('apply_prepared_changes')
        if 'saved_messages' in result:
            self.choose_saved_message(result['saved_messages'])
        if 'diagnostics' in result:
            self.diagnostics.setPlainText('\n'.join(result['diagnostics']))
        if result.get('session_expired'):
            self.loaded = False
            self.status.setText('Сессия недействительна. Введите номер телефона и войдите заново.')
        if 'error' in result:
            self.receive_progress({**self.progress_state, 'status': result['error']})
            self.log(result['error'])
            QMessageBox.warning(self, 'Операция остановлена', result['error'])
        elif result.get('profile_switched'):
            self.profiles.select(self.pending_profile)
            self.load_profile_view()
        elif result.get('auth') == 'qr':
            self.auth_stage = 'qr'
            self.show_qr_login(result['qr_url'])
            QTimer.singleShot(0, lambda: self.run('wait_qr_login'))
        elif result.get('auth') in ('code', 'password'):
            self.auth_stage = result['auth']
            if result['auth'] == 'code' and result.get('code_delivery'):
                QMessageBox.information(self, 'Код запрошен', result['code_delivery'])
            self.auth_input()
        elif result.get('auth') == 'ready':
            self.profiles = Profiles(self.root_directory)
            identity = result.get('account_id')
            other = next((p for p in self.profiles.active_profiles()
                          if p['id'] != self.profiles.data['active'] and p.get('account_id') == identity), None) if identity else None
            if other:
                self.loaded = False
                QMessageBox.warning(self, 'Аккаунт уже добавлен', f"Используйте профиль «{other['name']}», чтобы не создавать дубли расписаний.")
                return
            for profile in self.profiles.data['profiles']:
                if profile['id'] == self.profiles.data['active']:
                    profile['account_id'] = identity
            self.profiles.save()
            self.loaded = True
            self.auth_stage = None
            self.status.setText(f"Подключено • Premium: {'да' if result['premium'] else 'нет'} • "
                                f"Telethon {result['version']} • repeat API: {'есть' if result['repeat'] else 'нет'}")
            self.log(self.status.text())
            self.tabs.setCurrentIndex(1)
            QTimer.singleShot(0, lambda: self.run('dialogs'))
        elif 'account_dialogs' in result:
            self.cleanup_rows = list(result['account_dialogs'])
            available = {row['id'] for row in self.cleanup_rows}
            self.cleanup_selected_ids.intersection_update(available)
            current_folder = self.cleanup_folder.currentData()
            self.cleanup_folder.blockSignals(True)
            self.cleanup_folder.clear()
            self.cleanup_folder.addItem('Все папки', 'all')
            for folder in result.get('dialog_folders', []):
                self.cleanup_folder.addItem(folder['title'], folder['id'])
            self.cleanup_folder.setCurrentIndex(max(0, self.cleanup_folder.findData(current_folder)))
            self.cleanup_folder.blockSignals(False)
            self.render_cleanup_dialogs()
            self.log(f"Загружено диалогов для управления: {len(self.cleanup_rows)}. Отметки поиском и сортировкой не сбрасываются.")
        elif 'cleanup_reports' in result:
            removed = {report['id'] for report in result['cleanup_reports'] if report.get('ok')}
            self.cleanup_rows = [row for row in self.cleanup_rows if row['id'] not in removed]
            self.cleanup_selected_ids.difference_update(removed)
            for row_number in range(self.chats.rowCount() - 1, -1, -1):
                if self.chats.item(row_number, 0).data(Qt.ItemDataRole.UserRole) in removed:
                    self.chats.removeRow(row_number)
            self.save()
            for ident in removed:
                self.config['chats'].pop(str(ident), None)
            save_json(self.config_path, self.config)
            self.render_cleanup_dialogs()
            failed = [report for report in result['cleanup_reports'] if not report.get('ok')]
            if failed:
                self.log(f"Не очищено диалогов: {len(failed)}. Они остались отмечены; подробности в журнале.")
        elif 'dialogs' in result:
            self.chats.setRowCount(0)
            for chat in result['dialogs']:
                row = self.chats.rowCount()
                self.chats.insertRow(row)
                saved = self.config['chats'].get(str(chat['id']), {})
                for col, checked in ((0, saved.get('selected', False)), (2, saved.get('allowed', False))):
                    item = QTableWidgetItem()
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                    item.setData(Qt.ItemDataRole.UserRole, chat['id'])
                    self.chats.setItem(row, col, item)
                title = QTableWidgetItem(chat['title'])
                title.setToolTip(str(chat['id']))
                title.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.chats.setItem(row, 1, title)
                manual = QSpinBox()
                manual.setRange(0, 10080)
                manual.setValue(saved.get('manual', 15))
                self.chats.setCellWidget(row, 3, manual)
                self.chats.setItem(row, 4, QTableWidgetItem(saved.get('rules', '')))
                assigned = QComboBox()
                assigned.addItems(self.config['templates'])
                assigned.setCurrentText(saved.get('template_id', self.template.currentText()))
                self.chats.setCellWidget(row, 5, assigned)
                self.chats.setItem(row, 6, QTableWidgetItem('проверка при чтении очереди'))
                self.chats.setItem(row, 7, QTableWidgetItem('готов к проверке'))
                self.chats.setItem(row, 8, QTableWidgetItem(str(chat['id'])))
                count = chat.get('participants_count')
                count_item = QTableWidgetItem(str(count) if count is not None else 'Недоступно')
                count_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                count_item.setToolTip('Количество на момент загрузки из Telegram. «Недоступно» означает, что Telegram не сообщил число.')
                self.chats.setItem(row, 9, count_item)
            self.filter_chats()
            self.log(f"Загружено групп: {len(result['dialogs'])}")
            self.log('Список групп загружен. Для проверки отложек отметьте нужные группы и нажмите «Обновить очередь выбранных групп» на вкладке 5.')
        elif 'plan' in result:
            self.plan = result['plan']
            lines = []
            for p in self.plan:
                lines += [f"{p['title']} • slow mode {p['slow']} с • интервал {p['interval']/60:g} мин",
                          f"В очереди {p['existing']}; создать {len(p['slots'])}; повтор {p['repeat']} с",
                          f"После создания в очереди: {p['existing'] + len(p['slots'])}. Допустимо по интервалу за суточный цикл: {86400 // p['interval']}.",
                          'ТЕСТ: план ограничен одним новым слотом. Для заполнения нажмите кнопку полного плана.' if p.get('test_mode') else 'Полный план: существующие сообщения сохраняются, добавляются доступные слоты.',
                          f"Правила: {p['rules']}", 'ТЕКСТ:', p['text'], 'TELEGRAM HTML:', telegram_html.unparse(p['text'], decode(p.get('entities'))), 'СЛОТЫ:']
                for t in p['slots']:
                    content = p.get('slot_contents', {}).get(str(t), p)
                    lines += [f"{self.format_time(t)} • {content.get('template_id', '')}", content['text'],
                              telegram_html.unparse(content['text'], decode(content.get('entities')))]
                lines += ['-' * 65]
            self.preview.setPlainText('\n'.join(lines))
        elif 'changes' in result:
            self.changes = result['changes']
            lines = []
            for change in self.changes:
                row = change['row']
                lines += [f"{row['title']} / {self.format_time(row['stamp'])} / шаблон {row.get('template_id') or 'не привязан'}",
                          f"Действие: {change['action']}; повтор: {row['repeat']} с",
                          'БЫЛО (текст):', row['text'], 'СТАНЕТ (текст):', change.get('text', ''),
                          'TELEGRAM HTML БЫЛО:', telegram_html.unparse(row['text'], decode(row.get('entities'))), 'TELEGRAM HTML СТАНЕТ:', telegram_html.unparse(change.get('text', row['text']), decode(change.get('entities', row.get('entities')))), '-' * 60]
            self.change_preview.setPlainText('\n'.join(lines) if lines else 'Нет связанных сообщений, требующих изменения. Проверьте связи во вкладке очереди.')
            self.tabs.setCurrentIndex(2)
        elif 'queue' in result:
            self.queue.clearSelection()
            self.queue_rows = sorted(result['queue'], key=lambda r: (r['stamp'], r['chat'], r['id']))
            self.queue.setRowCount(len(self.queue_rows))
            for i, r in enumerate(self.queue_rows):
                for j, value in enumerate([r['title'], r['id'], self.format_time(r['stamp']), r['repeat'], r['text'], r.get('template_id') or 'не привязан', r.get('issue') or 'Синхронизировано']):
                    self.queue.setItem(i, j, QTableWidgetItem(str(value)))
                    self.queue.item(i, j).setToolTip(str(value))
            self.log(f'Очередь обновлена: {len(self.queue_rows)} сообщений')
            for i, row in enumerate(self.queue_rows):
                self.queue.item(i, 4).setToolTip(row.get('issue') or row['text'])
            self.filter_queue()
        if 'done' in result:
            self.log(result['done'])
            QMessageBox.information(self, 'Готово', result['done'])

    def closeEvent(self, event):
        if self.busy:
            self.cancel()
            QMessageBox.information(self, 'Остановка', 'Дождитесь окончания текущего запроса, затем закройте окно снова.')
            event.ignore()
            return
        self.save()
        try:
            self.worker.shutdown()
        except Exception:
            event.ignore()
            return
        event.accept()


def configure_app(app):
    app.setStyle('Fusion')
    font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'segoeui.ttf'
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont('Segoe UI', 10))


def main():
    app = QApplication(sys.argv)
    configure_app(app)
    directory = Path(sys.argv[sys.argv.index('--data-dir') + 1]) if '--data-dir' in sys.argv else data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / 'app.lock'))
    if not lock.tryLock(100):
        QMessageBox.warning(None, 'Уже открыто', 'Для этой папки данных уже работает экземпляр приложения.')
        return 1
    handler = RotatingFileHandler(directory / 'activity.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(message)s')
    logging.getLogger('telethon').setLevel(logging.CRITICAL)
    window = Window(directory)
    window.show()
    if '--smoke-test' in sys.argv:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2000, window.close)
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
