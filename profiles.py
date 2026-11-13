"""Local profile registry and Windows user-bound protection. No secrets in JSON."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import uuid

from storage import save_json


def _is_windows():
    return os.name == 'nt'


def _is_macos():
    return sys.platform == 'darwin'


def protect(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('Защищённые сессии поддерживаются только в Windows')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError(ctypes.get_last_error(), 'Windows DPAPI: не удалось открыть/сохранить секрет')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class Vault:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _keychain_account(self, name):
        identity = f'{self.directory.resolve()}:{name}'.encode('utf-8')
        return 'tgos-' + hashlib.sha256(identity).hexdigest()

    def _marker(self, name):
        return self.directory / (name + '.keychain')

    def exists(self, name):
        if _is_windows():
            return (self.directory / (name + '.dpapi')).exists()
        if _is_macos():
            return self._marker(name).exists()
        return False

    def read(self, name):
        path = self.directory / (name + '.dpapi')
        if _is_windows():
            return protect(path.read_bytes(), decrypt=True).decode('utf-8') if path.exists() else ''
        if _is_macos():
            if not self._marker(name).exists():
                return ''
            result = subprocess.run(
                ['security', 'find-generic-password', '-s', 'TelegramOfferScheduler',
                 '-a', self._keychain_account(name), '-w'],
                capture_output=True, text=True, timeout=15)
            if result.returncode == 44:
                return ''
            if result.returncode:
                raise OSError('Не удалось прочитать секрет из macOS Keychain')
            return result.stdout.rstrip('\n')
        raise RuntimeError('Защищённые сессии поддерживаются только в Windows и macOS')

    def write(self, name, value):
        self.directory.mkdir(parents=True, exist_ok=True)
        if _is_windows():
            target = self.directory / (name + '.dpapi')
            temp = target.with_suffix('.tmp')
            temp.write_bytes(protect(value.encode('utf-8')))
            os.replace(temp, target)
            return
        if _is_macos():
            result = subprocess.run(
                ['security', 'add-generic-password', '-U', '-s', 'TelegramOfferScheduler',
                 '-a', self._keychain_account(name), '-w', value],
                capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise OSError('Не удалось сохранить секрет в macOS Keychain')
            marker = self._marker(name)
            temp = marker.with_suffix('.tmp')
            temp.write_text('macOS Keychain\n', encoding='utf-8')
            os.replace(temp, marker)
            return
        raise RuntimeError('Защищённые сессии поддерживаются только в Windows и macOS')

    def delete(self, name):
        if _is_windows():
            (self.directory / (name + '.dpapi')).unlink(missing_ok=True)
            return
        if _is_macos():
            subprocess.run(
                ['security', 'delete-generic-password', '-s', 'TelegramOfferScheduler',
                 '-a', self._keychain_account(name)],
                capture_output=True, text=True, timeout=15)
            self._marker(name).unlink(missing_ok=True)


class Profiles:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / 'profiles.json'
        if self.file.exists():
            self.data = json.loads(self.file.read_text(encoding='utf-8'))
        else:
            # Legacy data stays in place, including the original SQLite session.
            self.data = {'active': 'legacy', 'profiles': [
                {'id': 'legacy', 'name': 'Основной аккаунт', 'removed': False}]}
            self.save()
        if not self.active_profiles():
            self.add('Основной аккаунт')
        if self.data.get('active') not in [p['id'] for p in self.active_profiles()]:
            self.data['active'] = self.active_profiles()[0]['id']
            self.save()

    def save(self):
        save_json(self.file, self.data)

    def active_profiles(self):
        return [p for p in self.data['profiles'] if not p.get('removed')]

    def path(self, profile_id):
        if profile_id not in [p['id'] for p in self.data['profiles']]:
            raise ValueError('Неизвестный профиль')
        if profile_id == 'legacy':
            return self.root
        if str(uuid.UUID(profile_id)) != profile_id:
            raise ValueError('Некорректный профиль')
        path = self.root / 'profiles' / profile_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def add(self, name):
        name = name.strip()
        if not name:
            raise ValueError('Введите название аккаунта')
        profile_id = str(uuid.uuid4())
        self.data['profiles'].append({'id': profile_id, 'name': name, 'removed': False})
        self.path(profile_id)
        self.save()
        return profile_id

    def select(self, profile_id):
        if profile_id not in [p['id'] for p in self.active_profiles()]:
            raise ValueError('Этот профиль удалён из списка')
        self.data['active'] = profile_id
        self.save()

    def remove(self, profile_id):
        if len(self.active_profiles()) <= 1:
            raise ValueError('Сначала добавьте другой профиль')
        for profile in self.data['profiles']:
            if profile['id'] == profile_id:
                profile['removed'] = True
        self.data['active'] = self.active_profiles()[0]['id']
        self.save()

    def restore(self, profile_id):
        for profile in self.data['profiles']:
            if profile['id'] == profile_id:
                profile['removed'] = False
        self.save()

    def purge(self, profile_id):
        profile = next(p for p in self.data['profiles'] if p['id'] == profile_id)
        if not profile.get('removed') or profile_id == self.data['active']:
            raise ValueError('Сначала переключитесь на другой профиль и удалите выбранный из списка')
        directory = self.path(profile_id).resolve()
        if not directory.is_relative_to(self.root) or (profile_id != 'legacy' and directory.parent != self.root / 'profiles'):
            raise ValueError('Путь профиля не принадлежит приложению')
        vault = Vault(directory)
        for secret in ('session', 'api_hash'):
            vault.delete(secret)
        names = {'settings.json', 'settings.tmp', 'session.dpapi', 'session.keychain', 'session.tmp',
                 'api_hash.dpapi', 'api_hash.keychain', 'api_hash.tmp',
                 'account.session', 'account.session-journal', 'account.session-wal', 'account.session-shm',
                 'progress.sqlite3', 'progress.sqlite3-wal', 'progress.sqlite3-shm'}
        for path in directory.iterdir():
            if path.name in names or (path.name.startswith('progress.sqlite3.before-') and path.suffix == '.bak'):
                if path.is_symlink() or path.resolve().parent != directory:
                    raise ValueError('Небезопасный путь данных профиля')
                path.unlink(missing_ok=True)
        self.data['profiles'].remove(profile)
        self.save()

    def claim(self, directory, account_id):
        current = next((p for p in self.data['profiles'] if self.path(p['id']) == Path(directory).resolve()), None)
        if current is None:
            raise ValueError('Профиль не зарегистрирован')
        for profile in self.data['profiles']:
            if profile['id'] == current['id']:
                continue
            identity = profile.get('account_id')
            database = self.path(profile['id']) / 'progress.sqlite3'
            if identity is None and database.exists():
                connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
                try:
                    row = connection.execute("SELECT value FROM meta WHERE key='account'").fetchone()
                    identity = int(row[0]) if row else None
                finally:
                    connection.close()
            if identity == account_id:
                raise ValueError(f"Аккаунт уже принадлежит профилю «{profile['name']}». Переключитесь на него или восстановите его из списка удалённых.")
        current['account_id'] = account_id
        self.save()


def load_settings(path):
    if path.exists():
        config = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            raise ValueError('Повреждён файл настроек; исходный файл сохранён')
    else:
        config = {}
    config.setdefault('chats', {})
    config.setdefault('templates', {'Сотрудничество': ''})
    config.setdefault('timezone', 'system')
    config.setdefault('test_mode', True)
    config.setdefault('create_pause', config.get('pause', 2))
    return config
