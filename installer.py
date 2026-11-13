"""Per-user versioned installation wizard; user data and older installs are preserved."""
import os
import shutil
import sys
import uuid
import zipfile
from pathlib import Path, PurePosixPath

VERSION = '4.2'


def install_archive(archive, base, progress=lambda value: None):
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    destination = base / VERSION
    if destination.exists():
        raise ValueError('Эта версия уже установлена. Запустите её из папки ' + str(destination))
    staging = base / ('.install-' + uuid.uuid4().hex)
    staging.mkdir()
    try:
        with zipfile.ZipFile(archive) as package:
            members = package.infolist()
            for index, member in enumerate(members):
                path = PurePosixPath(member.filename)
                if (not path.parts or path.parts[0] != 'TelegramOfferScheduler' or path.is_absolute()
                        or '..' in path.parts or '\\' in member.filename or ':' in member.filename
                        or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError('Недопустимый путь в пакете установки')
                output = staging.joinpath(*path.parts[1:])
                if not output.resolve().is_relative_to(staging.resolve()):
                    raise ValueError('Путь выходит за пределы папки установки')
                if member.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as source, output.open('wb') as target:
                        shutil.copyfileobj(source, target)
                progress(int((index + 1) * 100 / max(1, len(members))))
        if not (staging / 'TelegramOfferScheduler.exe').is_file():
            raise ValueError('В пакете нет приложения')
        staging.rename(destination)
        return destination / 'TelegramOfferScheduler.exe'
    finally:
        if staging.exists() and staging.resolve().parent == base and staging.name.startswith('.install-'):
            shutil.rmtree(staging)


def main():
    if '--verify-package' in sys.argv:
        base = Path(sys.argv[sys.argv.index('--verify-package') + 1])
        install_archive(Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'payload.zip', base)
        return 0
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QWizard, QWizardPage, QVBoxLayout, QLabel, QLineEdit, QProgressBar, QCheckBox
    from desktop_shortcut import create_desktop_shortcut
    import subprocess
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'segoeui.ttf'
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont('Segoe UI', 10))
    class Wizard(QWizard):
        installing = False
        def reject(self):
            if not self.installing:
                super().reject()
        def closeEvent(self, event):
            if self.installing:
                event.ignore()
            else:
                super().closeEvent(event)
    wizard = Wizard()
    wizard.setWindowTitle('Установка Telegram Offer Scheduler ' + VERSION)
    wizard.setButtonText(QWizard.WizardButton.NextButton, 'Далее')
    wizard.setButtonText(QWizard.WizardButton.BackButton, 'Назад')
    wizard.setButtonText(QWizard.WizardButton.CancelButton, 'Отмена')
    wizard.setButtonText(QWizard.WizardButton.FinishButton, 'Готово')
    first = QWizardPage()
    first.setTitle('Установка или обновление')
    layout = QVBoxLayout(first)
    label = QLabel('Закройте работающую программу перед обновлением. Новая версия установится в отдельную папку. Старые версии и данные аккаунтов сохранятся. Права администратора не требуются.')
    label.setWordWrap(True)
    layout.addWidget(label)
    location = QLineEdit(str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Programs' / 'TelegramOfferScheduler'))
    layout.addWidget(location)
    shortcut = QCheckBox('Создать или обновить ярлык на рабочем столе')
    shortcut.setChecked(True)
    layout.addWidget(shortcut)
    wizard.addPage(first)
    class InstallWorker(QThread):
        changed = Signal(int)
        done = Signal(object)
        def run(self):
            try:
                archive = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'payload.zip'
                result = install_archive(archive, self.base, self.changed.emit)
                # Local settings backup is performed by the new app at first launch.
                notice = ''
                if self.shortcut:
                    try:
                        create_desktop_shortcut(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'TelegramOfferScheduler', executable=result, replace_existing=True)
                    except OSError as exc:
                        notice = '\nПриложение установлено, но ярлык не создан: ' + str(exc)
                self.done.emit({'path': str(result), 'notice': notice})
            except Exception as exc:
                self.done.emit({'error': str(exc)})
    class InstallPage(QWizardPage):
        def __init__(self):
            super().__init__()
            self.finished = False
            self.setTitle('Установка')
            box = QVBoxLayout(self)
            self.info = QLabel('Распаковка файлов…')
            self.info.setWordWrap(True)
            box.addWidget(self.info)
            self.progress = QProgressBar()
            box.addWidget(self.progress)
            self.launch = QCheckBox('Запустить программу после установки')
            self.launch.setChecked(True)
            self.launch.setEnabled(False)
            box.addWidget(self.launch)
        def isComplete(self):
            return self.finished
        def initializePage(self):
            wizard.installing = True
            wizard.button(QWizard.WizardButton.BackButton).setEnabled(False)
            wizard.button(QWizard.WizardButton.CancelButton).setEnabled(False)
            self.worker = InstallWorker()
            self.worker.base = location.text()
            self.worker.shortcut = shortcut.isChecked()
            self.worker.changed.connect(self.progress.setValue)
            self.worker.done.connect(lambda result: setattr(self, 'pending_result', result))
            self.worker.finished.connect(lambda: self.complete(self.pending_result))
            self.worker.start()
        def complete(self, result):
            wizard.installing = False
            self.result = result
            self.finished = True
            self.info.setText('Установка не выполнена: ' + result['error'] if 'error' in result else 'Установлено:\n' + result['path'] + result['notice'])
            self.launch.setEnabled('path' in result)
            self.completeChanged.emit()
    last = InstallPage()
    wizard.addPage(last)
    wizard.resize(650, 360)
    wizard.show()
    if '--smoke-test' in sys.argv:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2000, wizard.reject)
    def finished():
        result = getattr(last, 'result', {})
        if 'path' in result and last.launch.isChecked():
            subprocess.Popen([result['path']], cwd=str(Path(result['path']).parent), creationflags=subprocess.CREATE_NO_WINDOW)
    wizard.accepted.connect(finished)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
