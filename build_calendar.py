#!/usr/bin/env python3
"""
Build events-leipzig.ics from PLANLOS and Sachsenpunk URL's.

The PLANLOS site uses WordPress/Event Organiser. 

First try its iCal feed, then fall back to HTML event pages if that doesn't work.

Sachsenpunk is a plain date listing. We parse only Leipzig entries and enrich locations from the Sachsenpunk locations page and a small alias/address map.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import tz

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "events-leipzig.ics"
ALIASES = json.loads((ROOT / "locations.json").read_text(encoding="utf-8"))["aliases"]

PLANLOS = "https://www.planlos-leipzig.org"
PLANLOS_FEEDS = [
    f"{PLANLOS}/events/feed/eo-events/?event_start_after=now",
    f"{PLANLOS}/feed/eo-events/?event_start_after=now",
]
PLANLOS_CALENDAR = f"{PLANLOS}/"
SACHSEN_DATES = "https://sachsenpunk.de/dates/"
SACHSEN_LOCATIONS = "https://sachsenpunk.de/locations-gruppen-festivals/#locations"

HEADERS = {
    "User-Agent": "Events-Leipzig-Calendar/1.0 (+https://github.com/23KE74E6R347/events-leipzig-calendar)"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
BERLIN = tz.gettz("Europe/Berlin")


def get(url: str, timeout: int = 30) -> requests.Response:
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def ical_unescape(s: str) -> str:
    return (s.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def unfold_ics(text: str) -> list[str]:
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_ics_dt(value: str, params: str = ""):
    value = value.strip()
    if "VALUE=DATE" in params:
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=BERLIN), True
    # UTC and local values handler
    if value.endswith("Z"):
        return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=tz.UTC).astimezone(BERLIN), False
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=BERLIN), False
        except ValueError:
            pass
    return None, False


def parse_ics(text: str, source: str) -> list[dict]:
    events = []
    block = []
    in_event = False
    for line in unfold_ics(text):
        if line == "BEGIN:VEVENT":
            in_event, block = True, []
        elif line == "END:VEVENT":
            if in_event:
                fields = {}
                for x in block:
                    if ":" not in x:
                        continue
                    k, v = x.split(":", 1)
                    fields.setdefault(k.split(";", 1)[0].upper(), []).append(v)
                title = ical_unescape(fields.get("SUMMARY", [""])[0])
                start_key = next((x for x in fields if x.startswith("DTSTART")), None)
                if title and start_key:
                    start, all_day = parse_ics_dt(fields[start_key][0], start_key)
                    if start:
                        end = None
                        end_key = next((x for x in fields if x.startswith("DTEND")), None)
                        if end_key:
                            end, _ = parse_ics_dt(fields[end_key][0], end_key)
                        events.append({
                            "uid": fields.get("UID", [str(uuid.uuid4())])[0],
                            "title": title,
                            "start": start,
                            "end": end,
                            "all_day": all_day,
                            "location": ical_unescape(fields.get("LOCATION", [""])[0]),
                            "description": ical_unescape(fields.get("DESCRIPTION", [""])[0]),
                            "url": fields.get("URL", [""])[0],
                            "source": source,
                        })
            in_event = False
        elif in_event:
            block.append(line)
    return events


def fetch_planlos_ics() -> list[dict]:
    for url in PLANLOS_FEEDS:
        try:
            r = get(url)
            if "BEGIN:VCALENDAR" in r.text and "VEVENT" in r.text:
                return parse_ics(r.text, url)
        except Exception as e:
            print(f"PLANLOS ICS failed: {url}: {e}", file=sys.stderr)
    return []


def parse_planlos_html() -> list[dict]:

"""
Fallback: scrape event links from the calendar page and their detail pages
"""

    events = []
    seen = set()
    try:
        page = get(PLANLOS_CALENDAR).text
    except Exception as e:
        print(f"PLANLOS HTML failed: {e}", file=sys.stderr)
        return events

    soup = BeautifulSoup(page, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(PLANLOS, a["href"])
        if "/events/" in href and href.rstrip("/") != PLANLOS.rstrip("/"):
            links.append(href)
    for url in dict.fromkeys(links):
        if url in seen:
            continue
        seen.add(url)
        try:
            s = BeautifulSoup(get(url).text, "html.parser")
            title = clean((s.find("h1") or s.title).get_text(" ", strip=True))
            text = clean(s.get_text(" ", strip=True))
            m = re.search(r"(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\s*-\s*(\d{2}\.\d{2}\.\d{4})", text)
            if not m:
                m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if not m:
                continue
            date = datetime.strptime(m.group(1), "%d.%m.%Y").replace(tzinfo=BERLIN)
            tm = re.search(r"(\d{1,2}:\d{2})\s*(?:-\s*(\d{1,2}:\d{2}))?", text)
            start = date
            end = None
            if tm:
                hh, mm = map(int, tm.group(1).split(":"))
                start = date.replace(hour=hh, minute=mm)
                if tm.group(2):
                    eh, em = map(int, tm.group(2).split(":"))
                    end = date.replace(hour=eh, minute=em)
            # address: look for "Wo" and nearby text
            loc = ""
            h2 = next((h for h in s.find_all(["h2","h3"]) if clean(h.get_text()) == "Wo"), None)
            if h2:
                nxt = h2.find_next()
                loc = clean(nxt.get_text(" ", strip=True)) if nxt else ""
            if "Leipzig" not in loc and "leipzig" not in text.lower():
                continue
            events.append({
                "uid": f"planlos-{uuid.uuid5(uuid.NAMESPACE_URL, url)}",
                "title": title,
                "start": start,
                "end": end,
                "all_day": tm is None,
                "location": loc,
                "description": "",
                "url": url,
                "source": url,
            })
        except Exception as e:
            print(f"PLANLOS event failed {url}: {e}", file=sys.stderr)
    return events


def normalize_sachsen_location(raw: str) -> tuple[str, str]:
    name = clean(raw)
    address = ""
    # exact aliases
    for key, addr in ALIASES.items():
        if name.casefold() == key.casefold():
            return name, addr
    # normalize variants
    for key, addr in ALIASES.items():
        if key.casefold() in name.casefold():
            return name, addr
    return name, address


def parse_sachsenpunk() -> list[dict]:
    text = BeautifulSoup(get(SACHSEN_DATES).text, "html.parser").get_text("\n")
    lines = [clean(x) for x in text.splitlines()]
    year = None
    month = None
    current_date = None
    events = []
    month_names = {
        "Januar":1,"Februar":2,"März":3,"April":4,"Mai":5,"Juni":6,
        "Juli":7,"August":8,"September":9,"Oktober":10,"November":11,"Dezember":12
    }
    for line in lines:
        if not line:
            continue
        ym = re.search(r"(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+(\d{4})", line)
        if ym:
            month, year = month_names[ym.group(1)], int(ym.group(2))
            current_date = None
            continue
        dm = re.match(r"^(\d{1,2})\.\d{2}\.\s*\(", line)
        if dm and year:
            # Sachsenpunk display date DD.MM.
            current_date = datetime(year, month, int(dm.group(1)), tzinfo=BERLIN)
            continue
        # omit the repeated month
        dm2 = re.match(r"^(\d{1,2})\.\s*\(", line)
        if dm2 and year and month:
            current_date = datetime(year, month, int(dm2.group(1)), tzinfo=BERLIN)
            continue
        if not current_date or not line.startswith("Leipzig"):
            continue
        parts = [clean(p) for p in re.split(r"\s+–\s+", line, maxsplit=3)]
        if len(parts) < 3:
            continue
        city, venue, rest = parts[0], parts[1], parts[2]
        if city != "Leipzig":
            continue
        tm = re.search(r"(\d{1,2}):(\d{2})\s*Uhr", rest)
        start = current_date
        if tm:
            start = current_date.replace(hour=int(tm.group(1)), minute=int(tm.group(2)))
        venue_name, address = normalize_sachsen_location(venue)
        # strip time from title
        title = re.sub(r"\s*–?\s*\d{1,2}:\d{2}\s*Uhr!?\s*–?\s*", "", rest, count=1)
        events.append({
            "uid": f"sachsenpunk-{uuid.uuid5(uuid.NAMESPACE_URL, line + current_date.isoformat())}",
            "title": clean(title),
            "start": start,
            "end": None,
            "all_day": tm is None,
            "location": f"{venue_name}, {address}" if address else f"{venue_name}, Leipzig",
            "description": "Quelle: Sachsenpunk dates",
            "url": SACHSEN_DATES,
            "source": SACHSEN_DATES,
        })
    return events


def dedupe(events):
    seen = set()
    out = []
    for e in events:
        key = (e["title"].casefold(), e["start"].isoformat(), e["location"].casefold())
        if key not in seen:
            seen.add(key)
            out.append(e)
    return sorted(out, key=lambda e: (e["start"], e["title"].casefold()))


def esc(v: str) -> str:
    return (str(v).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n").replace("\r", ""))


def fold_ical(line: str) -> str:
    # RFC 5545 lines <= 75 octets for UTF-8 text
    out = []
    while len(line.encode("utf-8")) > 74:
        cut = 74
        while len(line[:cut].encode("utf-8")) > 74:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    out.append(line)
    return "\r\n".join(out)


def fmt_dt(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


def make_ics(events):
    stamp = datetime.now(tz=BERLIN).strftime("%Y%m%dT%H%M%S")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//23KE74E6R347//Events Leipzig Calendar//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Events Leipzig Calendar",
        "X-WR-TIMEZONE:Europe/Berlin",
        f"DTSTAMP:{stamp}",
    ]
    for e in events:
        out.append("BEGIN:VEVENT")
        out.append(f"UID:{esc(e['uid'])}")
        if e["all_day"]:
            out.append(f"DTSTART;VALUE=DATE:{e['start'].strftime('%Y%m%d')}")
            if e.get("end"):
                out.append(f"DTEND;VALUE=DATE:{e['end'].strftime('%Y%m%d')}")
        else:
            out.append(f"DTSTART;TZID=Europe/Berlin:{fmt_dt(e['start'])}")
            if e.get("end"):
                out.append(f"DTEND;TZID=Europe/Berlin:{fmt_dt(e['end'])}")
        out.append(f"SUMMARY:{esc(e['title'])}")
        if e.get("location"):
            out.append(f"LOCATION:{esc(e['location'])}")
        if e.get("description"):
            out.append(f"DESCRIPTION:{esc(e['description'])}")
        if e.get("url"):
            out.append(f"URL:{e['url']}")
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(fold_ical(x) for x in out) + "\r\n"


def main():
    planlos = fetch_planlos_ics()
    if not planlos:
        planlos = parse_planlos_html()

    sachsen = parse_sachsenpunk()

    events = dedupe(planlos + sachsen)
    # bind envents and remove stale items
    cutoff = datetime.now(tz=BERLIN) - timedelta(days=2)
    events = [e for e in events if e["start"] >= cutoff]

    if not events:
        raise SystemExit("No events were parsed; refusing to overwrite the existing ICS.")

    OUT.write_text(make_ics(events), encoding="utf-8")
    print(f"PLANLOS: {len(planlos)}")
    print(f"SACHSENPUNK Leipzig: {len(sachsen)}")
    print(f"TOTAL after dedupe/filter: {len(events)}")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
