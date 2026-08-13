"""Calendar / schedule management with ICS import/export."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from friday.config import FridayConfig
from friday.prompts import WEEKDAYS_KO


def _cal_file(cfg: FridayConfig) -> Path:
    return cfg.calendar_dir / "events.jsonl"


def _load_events(cfg: FridayConfig) -> List[Dict[str, Any]]:
    path = _cal_file(cfg)
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _save_events(events: List[Dict[str, Any]], cfg: FridayConfig) -> None:
    cfg.calendar_dir.mkdir(parents=True, exist_ok=True)
    path = _cal_file(cfg)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def cal_add(
    title: str,
    date_str: str,
    time_str: Optional[str] = None,
    duration_min: int = 60,
    note: str = "",
    cfg: FridayConfig = None,
    dedupe_key: str = "",
) -> Dict[str, Any]:
    """Add a calendar event.  date_str='2026-04-05', time_str='14:00'."""
    # Validate date
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"날짜 형식 오류: {date_str} (YYYY-MM-DD)")

    if time_str:
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            raise ValueError(f"시간 형식 오류: {time_str} (HH:MM)")

    events = _load_events(cfg)
    if dedupe_key:
        existing = next(
            (event for event in events if event.get("dedupe_key") == dedupe_key),
            None,
        )
        if existing:
            return existing

    event = {
        "id": uuid4().hex[:8],
        "title": title,
        "date": date_str,
        "time": time_str or "",
        "duration_min": duration_min,
        "note": note,
        "dedupe_key": dedupe_key,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    events.append(event)
    _save_events(events, cfg)
    return event


def cal_list(
    cfg: FridayConfig,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    upcoming_days: int = 7,
) -> List[Dict[str, Any]]:
    """List events, optionally filtered by date range."""
    events = _load_events(cfg)
    if not date_from:
        date_from = datetime.now().strftime("%Y-%m-%d")
    if not date_to:
        from_dt = datetime.strptime(date_from, "%Y-%m-%d")
        to_dt = from_dt + timedelta(days=upcoming_days)
        date_to = to_dt.strftime("%Y-%m-%d")

    filtered = [
        e for e in events
        if date_from <= e.get("date", "") <= date_to
    ]
    return sorted(filtered, key=lambda e: (e.get("date", ""), e.get("time", "")))


def cal_delete(event_id: str, cfg: FridayConfig) -> Optional[Dict[str, Any]]:
    """Delete an event by ID.  Returns the removed event or None."""
    events = _load_events(cfg)
    removed = None
    new_events = []
    for e in events:
        if e.get("id") == event_id:
            removed = e
        else:
            new_events.append(e)
    if removed:
        _save_events(new_events, cfg)
    return removed


def cal_today(cfg: FridayConfig) -> List[Dict[str, Any]]:
    """Return today's events."""
    today = datetime.now().strftime("%Y-%m-%d")
    return cal_list(cfg, date_from=today, date_to=today)


def cal_export_ics(cfg: FridayConfig, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Path:
    """Export events to ICS file (Apple Calendar / Google Calendar compatible)."""
    try:
        from icalendar import Calendar as ICalendar, Event as IEvent  # type: ignore
    except ImportError:
        raise ImportError("ICS 내보내기에는 icalendar이 필요합니다.  pip install icalendar")

    events = cal_list(cfg, date_from=date_from, date_to=date_to, upcoming_days=365)

    ical = ICalendar()
    ical.add("prodid", "-//build-up CLI//build-up//KO")
    ical.add("version", "2.0")
    ical.add("calscale", "GREGORIAN")

    for ev in events:
        ie = IEvent()
        ie.add("uid", f"{ev['id']}@build-up-local")
        ie.add("summary", ev["title"])

        if ev.get("time"):
            start = datetime.strptime(f"{ev['date']}T{ev['time']}", "%Y-%m-%dT%H:%M")
        else:
            start = datetime.strptime(ev["date"], "%Y-%m-%d")

        ie.add("dtstart", start)
        ie.add("dtend", start + timedelta(minutes=ev.get("duration_min", 60)))
        if ev.get("note"):
            ie.add("description", ev["note"])
        ie.add("dtstamp", datetime.now())
        ical.add_component(ie)

    ics_path = cfg.calendar_dir / "night_calendar.ics"
    ics_path.write_bytes(ical.to_ical())
    return ics_path


def cal_import_ics(ics_path: Path, cfg: FridayConfig) -> int:
    """Import events from an ICS file.  Returns number of events added."""
    try:
        from icalendar import Calendar as ICalendar  # type: ignore
    except ImportError:
        raise ImportError("ICS 가져오기에는 icalendar이 필요합니다.  pip install icalendar")

    raw = ics_path.read_bytes()
    ical = ICalendar.from_ical(raw)
    count = 0

    for component in ical.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("dtstart")
        if not dtstart:
            continue
        dt = dtstart.dt
        date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)
        time_str = dt.strftime("%H:%M") if hasattr(dt, "hour") else ""
        title = str(component.get("summary", "Untitled"))
        note = str(component.get("description", ""))

        duration_min = 60
        dtend = component.get("dtend")
        if dtend and hasattr(dtend.dt, "hour"):
            delta = dtend.dt - dt
            duration_min = max(int(delta.total_seconds() / 60), 1)

        cal_add(title, date_str, time_str or None, duration_min, note, cfg)
        count += 1

    return count


def format_events(events: List[Dict[str, Any]]) -> str:
    """Format a list of events for display."""
    if not events:
        return "(일정 없음)"
    lines: List[str] = []
    current_date = ""
    for e in events:
        d = e.get("date", "")
        if d != current_date:
            current_date = d
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                wd = WEEKDAYS_KO[dt.weekday()]
                lines.append(f"\n📅 {d} ({wd}요일)")
            except ValueError:
                lines.append(f"\n📅 {d}")
        t = e.get("time", "")
        time_label = f"  {t}" if t else "  종일"
        dur = e.get("duration_min", 0)
        dur_label = f" ({dur}분)" if dur and t else ""
        note = f"  — {e['note']}" if e.get("note") else ""
        lines.append(f"{time_label}  {e['title']}{dur_label}{note}  (id: {e.get('id','')})")
    return "\n".join(lines).strip()
