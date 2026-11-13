"""Pure schedule calculations. All times are UTC epoch seconds."""
DAY = 86400


def interval_seconds(slowmode: int, manual_minutes: int, margin: int = 60) -> int:
    if slowmode < 0 or manual_minutes < 0 or margin < 60:
        raise ValueError('Интервал не может быть отрицательным')
    return max(900, slowmode, manual_minutes * 60) + int(margin)


def distance(a: int, b: int) -> int:
    d = abs(a - b) % DAY
    return min(d, DAY - d)


def make_slots(start: int, now: int, interval: int, occupied=(), limit=90, allowed=None):
    if start < now + 60 or start >= now + DAY:
        raise ValueError('Начало должно быть от +1 минуты до +24 часов')
    if interval > DAY:
        raise ValueError('Интервал больше суток: ежедневный повтор невозможен')
    if interval < 960:
        raise ValueError('Минимальный интервал с запасом — 16 минут')
    if limit <= 0:
        return []
    # Existing phases split the circle into free arcs. An optimum can start at
    # the requested lower bound or immediately after an occupied phase's guard.
    # Try those finite boundary points rather than dropping points from one grid.
    phases = sorted(set(start + (int(x) - start) % DAY for x in occupied))
    centers = [p + shift for p in phases for shift in (-DAY, 0, DAY)]
    seeds = sorted({start, *(start + (p + interval - start) % DAY for p in phases)})
    if allowed is not None:
        previous = allowed(start)
        for stamp in range((start // 60 + 1) * 60, now + DAY, 60):
            current = allowed(stamp)
            if current and not previous:
                seeds.append(stamp)
            previous = current
        seeds = sorted(set(seeds))
    best = []
    for seed in seeds:
        stamp, result = seed, []
        upper = min(now + DAY - 1, seed + DAY - interval)
        while stamp <= upper and len(result) < limit:
            while True:
                if allowed is not None and not allowed(stamp):
                    stamp = (stamp // 60 + 1) * 60
                    if stamp > upper:
                        break
                    continue
                conflicts = [p for p in centers if abs(stamp - p) < interval]
                if not conflicts:
                    break
                stamp = max(p + interval for p in conflicts)
            if stamp > upper:
                break
            result.append(stamp)
            stamp += interval
        if len(result) > len(best):
            best = result
    return best


def validate_text(text):
    if not text.strip():
        raise ValueError('Шаблон пуст')
    if len(text.encode('utf-16-le')) // 2 > 4096:
        raise ValueError('Текст длиннее 4096 UTF-16 символов')
    return text
