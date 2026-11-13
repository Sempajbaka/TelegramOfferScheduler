"""Conservative one-to-one reconciliation; uncertain matches require user binding."""
from planner import DAY, distance
from formatting import same_entities


def compatible(job, msg, tolerance=120):
    if getattr(msg, 'media', None) or getattr(msg, 'reply_to', None):
        return False
    if (getattr(msg, 'schedule_repeat_period', None) or 0) != job['repeat']:
        return False
    if (msg.message or '') not in {job['text'], job.get('pending_text')}:
        return False
    if not any(same_entities(msg, value) for value in (job.get('entities'), job.get('pending_entities')) if value is not None):
        if job.get('entities') is not None:
            return False
    stamp = int(msg.date.timestamp())
    origin = job.get('server_stamp') or job['stamp']
    if job['repeat'] != DAY:
        return stamp == origin
    return stamp >= origin - tolerance and distance(stamp, origin) <= tolerance


def reconcile(jobs, messages):
    candidates = {}
    active = [j for j in jobs if j['state'] not in ('deleted', 'cancelled')]
    for job in active:
        candidates[job['stamp']] = [m for m in messages if compatible(job, m)]
    reverse = {}
    for stamp, options in candidates.items():
        for msg in options:
            reverse.setdefault(msg.id, []).append(stamp)
    matches, issues = {}, {}
    for job in active:
        options = candidates[job['stamp']]
        if len(options) == 1 and len(reverse[options[0].id]) == 1:
            matches[job['stamp']] = options[0]
        else:
            issues[job['stamp']] = 'неоднозначно' if options else 'не найдено / изменено'
    return matches, issues
