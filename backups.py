"""Local settings snapshots. Never copy authorization secrets or server queues."""
import json
import time
from pathlib import Path
from storage import save_json
from formatting import parse_offer

KEYS = {'templates', 'template_formats', 'template_entities', 'template_versions', 'chats',
        'margin', 'pause', 'create_pause', 'timezone', 'test_mode', 'notifications'}


def create_backup(directory, reason='manual'):
    directory = Path(directory)
    source = directory / 'settings.json'
    if not source.exists():
        return None
    config = json.loads(source.read_text(encoding='utf-8'))
    target = directory / 'backups'
    target.mkdir(exist_ok=True)
    path = target / f'{time.time_ns()}-{reason}.json'
    save_json(path, {'version': 1, 'created': int(time.time()),
                     'settings': {k: v for k, v in config.items() if k in KEYS}})
    return path


def backup_before_upgrade(directory):
    directory = Path(directory)
    if (directory / 'settings.json').exists() and not list((directory / 'backups').glob('*-before-v4.0.json')):
        create_backup(directory, 'before-v4.0')


def read_backup(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('settings'), dict):
        raise ValueError('Неизвестный формат резервной копии')
    config = data['settings']
    if set(config) - KEYS or not isinstance(config.get('templates'), dict) or not config['templates']:
        raise ValueError('Некорректные настройки в копии')
    for key in ('template_formats', 'template_entities', 'template_versions'):
        if not isinstance(config.get(key, {}), dict):
            raise ValueError('Некорректные данные шаблонов')
    for history in config.get('template_versions', {}).values():
        if not isinstance(history, list) or any(not isinstance(v, dict) or not isinstance(v.get('text'), str)
                                               or not isinstance(v.get('time'), (int, float)) for v in history):
            raise ValueError('Некорректная история шаблонов')
    for name, text in config['templates'].items():
        if not isinstance(name, str) or not isinstance(text, str):
            raise ValueError('Некорректный шаблон')
        if text.strip():
            parse_offer({'text': text, 'format': config.get('template_formats', {}).get(name, 'plain'),
                         'entities': config.get('template_entities', {}).get(name, '[]')})
    if not isinstance(config.get('chats', {}), dict):
        raise ValueError('Некорректный список групп')
    for chat, value in config.get('chats', {}).items():
        if not str(chat).lstrip('-').isdigit() or not isinstance(value, dict):
            raise ValueError('Некорректные настройки группы')
        if not isinstance(value.get('manual', 15), (int, float)) or not isinstance(value.get('rules', ''), str):
            raise ValueError('Некорректный интервал или правила группы')
    if not isinstance(config.get('timezone', 'system'), str):
        raise ValueError('Некорректный часовой пояс')
    if not isinstance(config.get('test_mode', True), bool):
        raise ValueError('Некорректный тестовый режим')
    for key in ('margin', 'pause', 'create_pause'):
        if key in config and (not isinstance(config[key], (int, float)) or config[key] < 0):
            raise ValueError('Некорректные интервалы')
    return config


def restore_backup(directory, path):
    directory, path = Path(directory), Path(path)
    if path.resolve().parent != (directory / 'backups').resolve():
        raise ValueError('Копия должна принадлежать выбранному профилю')
    config = read_backup(path)
    create_backup(directory, 'before-restore')
    current = json.loads((directory / 'settings.json').read_text(encoding='utf-8'))
    current = {k: v for k, v in current.items() if k not in KEYS}
    save_json(directory / 'settings.json', {**current, **config})
