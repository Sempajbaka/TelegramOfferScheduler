"""Pure filtering and sorting helpers for account dialog cleanup."""


KIND_NAMES = {
    'group': 'Группа',
    'channel': 'Канал',
    'private': 'Личный чат',
    'bot': 'Бот',
}

def dialog_matches(row, query='', kind='all', folder='all'):
    query = (query or '').strip().casefold()
    if query and query not in row.get('title', '').casefold():
        return False
    if kind != 'all' and row.get('kind') != kind:
        return False
    return folder == 'all' or folder in row.get('folders', [])


def sort_dialogs(rows, order='activity_desc'):
    """Sort copies while keeping missing activity/member counts at the bottom."""
    rows = list(rows)
    title = lambda row: (row.get('title') or '').casefold()
    if order == 'activity_asc':
        key = lambda row: (not bool(row.get('last_activity')), row.get('last_activity') or 0, title(row))
    elif order == 'title_asc':
        key = lambda row: (title(row), row.get('id', 0))
    elif order == 'title_desc':
        return sorted(rows, key=lambda row: (title(row), row.get('id', 0)), reverse=True)
    elif order == 'members_desc':
        key = lambda row: (row.get('participants_count') is None,
                           -(row.get('participants_count') or 0), title(row))
    elif order == 'members_asc':
        key = lambda row: (row.get('participants_count') is None,
                           row.get('participants_count') or 0, title(row))
    elif order == 'folder_asc':
        key = lambda row: (', '.join(row.get('folder_names', [])).casefold(), title(row))
    else:
        key = lambda row: (not bool(row.get('last_activity')),
                           -(row.get('last_activity') or 0), title(row))
    return sorted(rows, key=key)
