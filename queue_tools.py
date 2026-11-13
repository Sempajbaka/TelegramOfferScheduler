"""Pure helpers for entity-safe edits and queue filtering."""
from difflib import SequenceMatcher
from formatting import decode, encode, parse_offer


def edited_offer(original, updated, serialized):
    entities = decode(serialized)
    old_units = [0]
    for char in original:
        old_units.append(old_units[-1] + len(char.encode('utf-16-le')) // 2)
    new_units = [0]
    for char in updated:
        new_units.append(new_units[-1] + len(char.encode('utf-16-le')) // 2)
    ops = SequenceMatcher(None, original, updated, autojunk=False).get_opcodes()

    def boundary(x, start):
        for tag, a, b, c, d in ops:
            if tag == 'insert' and x == a:
                return d if start else c
        for tag, a, b, c, d in ops:
            if a <= x <= b:
                if tag == 'equal':
                    return c + x - a
                if x == a:
                    return c
                if x == b:
                    return d
                raise ValueError('Правка пересекает границу форматирования. Измените текст внутри выделенного фрагмента или импортируйте новый шаблон.')
        return len(updated)

    result = []
    for entity in entities:
        a = old_units.index(entity.offset)
        b = old_units.index(entity.offset + entity.length)
        custom = type(entity).__name__ == 'MessageEntityCustomEmoji'
        if custom:
            match = next(((c+a-i, c+b-i) for tag, i, j, c, d in ops if tag == 'equal' and i <= a and b <= j), None)
            if match is None:
                raise ValueError('Кастомный эмодзи нельзя удалять или заменять в этом редакторе. Меняйте окружающий текст; для других эмодзи импортируйте новый шаблон.')
            left, right = match
        else:
            left, right = boundary(a, True), boundary(b, False)
        if right > left:
            entity.offset, entity.length = new_units[left], new_units[right] - new_units[left]
            result.append(entity)
    offer = {'text': updated, 'format': 'telegram', 'entities': encode(result)}
    parse_offer(offer)
    return offer


def queue_matches(row, query, kind):
    haystack = ' '.join(str(row.get(k) or '') for k in ('title', 'text', 'template_id', 'id')).casefold()
    return (query.strip().casefold() in haystack and
            (kind == 'all' or kind == 'daily' and row['repeat'] == 86400 or
             kind == 'once' and not row['repeat'] or kind == 'error' and bool(row.get('issue'))))
