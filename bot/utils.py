from __future__ import annotations

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_tz: ZoneInfo = ZoneInfo("Europe/Moscow")


def set_timezone(name: str) -> None:
    global _tz
    _tz = ZoneInfo(name)


def tz() -> ZoneInfo:
    return _tz


def local_now() -> datetime:
    return datetime.now(_tz)


def today() -> str:
    return local_now().strftime("%Y-%m-%d")


def month_start_day() -> str:
    return local_now().strftime("%Y-%m-01")


def day_start_ts() -> int:
    n = local_now()
    return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def month_start_ts() -> int:
    n = local_now()
    return int(n.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def days_ago(days: int) -> str:
    return (local_now() - timedelta(days=days)).strftime("%Y-%m-%d")


def fmt_dt(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, _tz).strftime("%d.%m.%Y %H:%M")


def fmt_date(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, _tz).strftime("%d.%m.%Y")


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} ТБ"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = abs(n) % 10, abs(n) % 100
    if n10 == 1 and n100 != 11:
        word = one
    elif 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def days_word(n: int) -> str:
    return plural(n, "день", "дня", "дней")


def devices_word(n: int) -> str:
    return plural(n, "устройство", "устройства", "устройств")


def left_str(until: int | None, now_ts: int) -> str:
    """«осталось 12 дн. 5 ч.»"""
    if not until or until <= now_ts:
        return "истекла"
    sec = until - now_ts
    d, h = sec // 86400, (sec % 86400) // 3600
    if d:
        return f"осталось {days_word(d)}"
    if h:
        return f"осталось {h} ч."
    return f"осталось {max(1, sec // 60)} мин."


def esc(text: object) -> str:
    return html.escape(str(text) if text is not None else "")
