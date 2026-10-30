import re

def _parse_duration_minutes(text: str) -> int | None:
    """Parse durations such as 20д 2ч 15м; cap at 30 days."""
    text = text.casefold().strip()
    pattern = r"(\d+)\s*(дней|дня|дн|день|д|часов|часа|час|ч|минуты|минута|минут|мин|м|сек|с|days|day|hours|hour|minutes|minute|min|hr|h|d|m)"
    matches = list(re.finditer(pattern, text))
    if not matches:
        return None
    total = 0
    covered = [False] * len(text)
    for match in matches:
        number, unit = int(match.group(1)), match.group(2)
        if unit in {"дней", "дня", "дн", "день", "д", "days", "day", "d"}: factor = 1440
        elif unit in {"часов", "часа", "час", "ч", "hours", "hour", "hr", "h"}: factor = 60
        elif unit in {"сек", "с"}: factor = 1 / 60
        else: factor = 1
        total += number * factor
        for i in range(match.start(), match.end()): covered[i] = True
    if ''.join(ch for i, ch in enumerate(text) if not covered[i] and not ch.isspace()):
        return None
    if total <= 0: return None
    return max(1, min(int(total), 30 * 24 * 60))
