"""Dates as the Delhi Medical Council writes them: day first, in a few formats, sometimes with a two-digit year.

  find_date("18-Apr-00") == "2000-04-18"      "12-01-2018", "02/01/2020", "20.11.2019", "25th May, 2017", "28th September, 2004"
"""

import re
from datetime import date

MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{2,4})\b"),                                  # 12-01-2018, 02/01/2020, 20.11.2019
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]+([A-Za-z]{3})[A-Za-z]*\.?,?[\s-]+(\d{2,4})\b"),               # 18-Apr-00, 25th May, 2017
]


def to_date(day, month, year):
    year = int(year)
    if year < 100:
        year += 2000 if year <= 40 else 1900  # the registers run 2000 onwards: "00" is 2000, "99" a typo
    try:
        return date(year, int(month), int(day)).isoformat()
    except ValueError:
        return ""


def find_date(text):
    """The first date in the text as YYYY-MM-DD, or ''. Day first, as the registers write it."""
    best = None
    for rx in DATE_PATTERNS:
        m = rx.search(text or "")
        if m and (best is None or m.start() < best[0]):
            day, month, year = m.groups()
            if not month.isdigit():
                month = MONTHS.get(month[:3].lower())
                if not month:
                    continue
            iso = to_date(day, month, year)
            if iso:
                best = (m.start(), iso)
    return best[1] if best else ""
