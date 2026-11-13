"""Per-group publishing windows use fixed Moscow UTC+3; no background sender."""
def within_window(stamp, chat):
    start, end = chat.get('window_start', 0), chat.get('window_end', 0)
    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < 1440 and 0 <= end < 1440):
        raise ValueError('Некорректные часы публикации')
    minute = ((stamp + 3 * 3600) % 86400) // 60
    return start == end or (start <= minute < end if start < end else minute >= start or minute < end)


def template_names(chat):
    names = chat.get('rotation') or [chat.get('template_id')]
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
        raise ValueError('Укажите список шаблонов для чередования')
    return names
