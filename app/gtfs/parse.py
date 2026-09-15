"""Tolerant readers for Renfe's GTFS feeds.

Renfe pads every field in the Cercanías feed with trailing spaces (including the
header row), so a plain csv.DictReader produces keys like ``"end_date       "``.
Everything here strips both header names and values, and normalises the handful
of places where the two feeds disagree about formatting.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Iterator

# Renfe writes some AVLD times as "8:30:00" and some Cercanías times as
# "08:30:00"; GTFS also allows >24h for trips running past midnight.
DAY = 24 * 3600


def read_rows(path: Path) -> Iterator[dict[str, str]]:
    """Yield rows with stripped keys and values, skipping blank lines."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            return
        for raw in reader:
            if not raw or all(not c.strip() for c in raw):
                continue
            row = {header[i]: raw[i].strip() for i in range(min(len(header), len(raw)))}
            for missing in header[len(raw):]:
                row[missing] = ""
            yield row


def parse_time(value: str) -> int | None:
    """GTFS ``HH:MM:SS`` -> seconds after midnight. Hours may exceed 24."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value.strip(), "%Y%m%d").date()


WEEKDAY_FIELDS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def service_dates(calendar_path: Path, calendar_dates_path: Path | None) -> dict[str, set[dt.date]]:
    """Expand calendar.txt + calendar_dates.txt into explicit per-service date sets.

    The feeds are small enough in calendar terms (a few months) that materialising
    the dates is far simpler than evaluating the rules at query time.
    """
    dates: dict[str, set[dt.date]] = {}

    if calendar_path.exists():
        for row in read_rows(calendar_path):
            sid = row["service_id"]
            start, end = parse_date(row["start_date"]), parse_date(row["end_date"])
            active = {i for i, f in enumerate(WEEKDAY_FIELDS) if row.get(f) == "1"}
            if not active:
                dates.setdefault(sid, set())
                continue
            bucket = dates.setdefault(sid, set())
            day = start
            while day <= end:
                if day.weekday() in active:
                    bucket.add(day)
                day += dt.timedelta(days=1)

    if calendar_dates_path and calendar_dates_path.exists():
        for row in read_rows(calendar_dates_path):
            sid = row["service_id"]
            day = parse_date(row["date"])
            bucket = dates.setdefault(sid, set())
            if row.get("exception_type") == "1":
                bucket.add(day)
            else:
                bucket.discard(day)

    return dates
