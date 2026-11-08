"""Simulated time as people write it: minutes since D0 00:00 <-> "D3 14:00" and event timestamps."""
from __future__ import annotations

import re
from datetime import date, datetime

MINUTES_PER_DAY = 1440
_LABEL = re.compile(r"D(-?\d+)\s+(\d{1,2}):(\d{2})")


def label(minute: int) -> str:
    day, m = divmod(minute, MINUTES_PER_DAY)
    return f"D{day} {m // 60:02d}:{m % 60:02d}"


def minute_of(ts: str, d0: date) -> int:
    """Minutes since D0 00:00 of a local (IST) timestamp."""
    t = datetime.fromisoformat(ts).replace(tzinfo=None)
    return int((t - datetime.combine(d0, datetime.min.time())).total_seconds() // 60)


def parse(text: str, d0: date) -> int:
    """`D3 14:00` or an ISO timestamp, as minutes since D0 00:00."""
    m = _LABEL.fullmatch(text.strip())
    if m:
        return int(m[1]) * MINUTES_PER_DAY + int(m[2]) * 60 + int(m[3])
    return minute_of(text.strip(), d0)
