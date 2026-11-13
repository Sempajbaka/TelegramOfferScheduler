"""Read-only cycle diagnosis. Queue presence never proves delivery."""
from planner import DAY
from synchronization import reconcile


def diagnose(jobs, messages, slow, allowed, margin=60):
    matched, issues = reconcile(jobs, messages)
    base = max(900, slow, int(allowed) * 60)
    lines = []
    for job in jobs:
        if job['state'] in ('deleted', 'cancelled'):
            continue
        msg = matched.get(job['stamp'])
        if msg:
            actual = int(msg.date.timestamp())
            drift = (actual - job['stamp'] + DAY // 2) % DAY - DAY // 2
            lines.append(f"Слот {job['stamp']}: ID {msg.id}; смещение от исходного цикла {drift:+d} с; повтор {getattr(msg, 'schedule_repeat_period', 0) or 0} с")
        else:
            lines.append(f"Слот {job['stamp']}: {issues.get(job['stamp'], 'не подтверждено')}; отправка/пропуск НЕ ПОДТВЕРЖДЕНЫ")
    linked = {m.id for m in matched.values()}
    for msg in messages:
        if msg.id not in linked:
            lines.append(f'ID {msg.id}: дополнительное или изменённое сообщение, автоматическая связь не подтверждена')
    phases = sorted((int(m.date.timestamp()) % DAY, m.id) for m in messages)
    conflicts = 0
    for i, (phase, ident) in enumerate(phases):
        next_phase, next_id = phases[(i + 1) % len(phases)]
        gap = next_phase - phase + (DAY if i == len(phases) - 1 else 0)
        if gap < base + margin:
            conflicts += 1
            severity = 'КОНФЛИКТ разрешённого интервала' if gap < base else 'запас уменьшился'
            lines.append(f'{severity}: ID {ident} → {next_id}, {gap} с; требуется {base} + {margin} с')
    lines.append('Доставлено: не подтверждено. Пропущено: не подтверждено. Наличие/отсутствие отложки не доказывает отправку.')
    if conflicts:
        lines.append('Проверьте очередь и правила. Можно увеличить запас вручную для нового плана; текущее расписание автоматически не изменяется.')
    return {'lines': lines, 'conflicts': conflicts, 'missing_or_changed': len(issues), 'extra': len(messages) - len(linked)}
