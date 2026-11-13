"""Explicit opt-in Telegram HTML; plain templates retain literal text."""
import json
from telethon.extensions import html
from telethon import types
from planner import validate_text


def parse_offer(value):
    if isinstance(value, str):
        return validate_text(value), []
    source = value.get('text', '')
    if value.get('format') == 'telegram':
        entities = decode(value.get('entities', '[]'))
        boundaries, position = {0}, 0
        for char in source:
            position += len(char.encode('utf-16-le')) // 2
            boundaries.add(position)
        for entity in entities:
            if (type(entity.offset) is not int or type(entity.length) is not int
                    or entity.length <= 0 or entity.offset not in boundaries
                    or entity.offset + entity.length not in boundaries):
                raise ValueError('Повреждены границы форматирования Telegram (UTF-16). Импортируйте сообщение заново.')
        return validate_text(source), entities
    if value.get('format', 'plain') == 'plain':
        return validate_text(source), []
    if value.get('format') != 'html':
        raise ValueError('Неизвестный формат шаблона')
    text, entities = html.parse(source)
    return validate_text(text), entities


def encode(entities):
    return json.dumps([e.to_dict() for e in entities or []], sort_keys=True, ensure_ascii=False)


def decode(value):
    result = []
    for data in json.loads(value or '[]'):
        data = dict(data)
        name = data.pop('_')
        if not name.startswith('MessageEntity') or not hasattr(types, name):
            raise ValueError('Неизвестный элемент форматирования Telegram')
        cls = getattr(types, name)
        result.append(cls(**data))
    return result


def imported_offer(message):
    offer = {'text': message.message or '', 'format': 'telegram',
             'entities': encode(getattr(message, 'entities', None))}
    parse_offer(offer)
    return offer


def same_entities(message, value):
    return encode(getattr(message, 'entities', None)) == encode(decode(value))
