#!/usr/bin/env python3
from __future__ import annotations

import html
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import tz

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "events-leipzig.ics"

PLANLOS_URL = "https://www.planlos-leipzig.org/"
SACHSENPUNK_URL = "https://sachsenpunk.de/dates/"
LOCATIONS_URL = "https://sachsenpunk.de/locations-gruppen-festivals/#locations"
BERLIN = tz.gettz("Europe/Berlin")

# These are safety floors, not expected exact totals.
# A failed scrape must never replace the last good ICS.
MIN_PLANLOS = 40
MIN_SACHSENPUNK = 20
MIN_TOTAL = 70

MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}

S = requests.Session()
S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (compatible; Events-Leipzig-Calendar/2.0; "
        "+https://github.com/23KE74E6R347/events-leipzig-calendar)"
    )
})


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def uid(*parts: object) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(map(str, parts))))


def get(url: str) -> str:
    r = S.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def dedupe(events: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for e in events:
        key = (
            e["title"].casefold(),
            e["start"].isoformat(),
            e["location"].casefold(),
        )
        if key not in seen:
            seen.add(key)
            out.append(e)
    return sorted(out, key=lambda x: (x["start"], x["title"].casefold()))


# ---------------------------------------------------------------------------
# PLANLOS
#
# The live page is a server-rendered calendar. Its structure is:
#
#   heading: "Di., 25. August 2026"
#   event row: "19:00 - 23:15 | TITLE"
#   location row: "Index, Leipzig"
#
# We therefore parse the rendered calendar rows rather than individual
# event pages. This avoids the 429 problem from crawling dozens of pages.
# ---------------------------------------------------------------------------

PLANLOS_DATE = re.compile(
    r"^(?:Mo|Di|Mi|Do|Fr|Sa|So)\.,\s+"
    r"(\d{1,2})\.\s+"
    r"(Januar|Februar|März|April|Mai|Juni|Juli|August|"
    r"September|Oktober|November|Dezember)\s+(\d{4})$",
    re.I,
)

PLANLOS_TIME = re.compile(
    r"^(\d{1,2}):(\d{2})"
    r"(?:\s*[-–]\s*(\d{1,2}):(\d{2}))?$"
)

PLANLOS_RANGE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(\d{4})"
    r"\s*[-–]\s*"
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})$"
)


def parse_planlos() -> list[dict]:
    soup = BeautifulSoup(get(PLANLOS_URL), "html.parser")

    # Preserve the visible line structure of the server-rendered page.
    text = soup.get_text("\n", strip=True)
    raw = [clean(x) for x in text.splitlines()]
    lines = [x for x in raw if x]

    events = []
    current_date = None
    i = 0

    while i < len(lines):
        line = lines[i]

        dm = PLANLOS_DATE.match(line)
        if dm:
            day = int(dm.group(1))
            month = MONTHS[dm.group(2).casefold()]
            year = int(dm.group(3))
            current_date = datetime(
                year, month, day, tzinfo=BERLIN
            )
            i += 1
            continue

        if current_date is None:
            i += 1
            continue

        # A PLANLOS event consists of:
        #   time
        #   title (often an <a>)
        #   location
        #
        # Some all-day events additionally contain a date-range line.
        tm = PLANLOS_TIME.match(line)

        all_day = False
        start = current_date
        end = None
        title = None
        location = None

        if tm:
            start = current_date.replace(
                hour=int(tm.group(1)),
                minute=int(tm.group(2)),
            )
            if tm.group(3):
                end = current_date.replace(
                    hour=int(tm.group(3)),
                    minute=int(tm.group(4)),
                )
            i += 1

            # Skip a standalone date range if it appears after the time.
            if i < len(lines) and PLANLOS_RANGE.match(lines[i]):
                i += 1

            if i < len(lines):
                title = lines[i]
                i += 1
            if i < len(lines):
                location = lines[i]
                i += 1

        elif line.casefold() == "ganztägig":
            all_day = True
            i += 1

            if i >= len(lines):
                break

            range_match = PLANLOS_RANGE.match(lines[i])
            if range_match:
                a = datetime(
                    int(range_match.group(3)),
                    int(range_match.group(2)),
                    int(range_match.group(1)),
                    tzinfo=BERLIN,
                )
                b = datetime(
                    int(range_match.group(6)),
                    int(range_match.group(5)),
                    int(range_match.group(4)),
                    tzinfo=BERLIN,
                )
                start = a
                end = b + timedelta(days=1)
                i += 1

            if i < len(lines):
                title = lines[i]
                i += 1
            if i < len(lines):
                location = lines[i]
                i += 1

        if not title or not location:
            continue

        # Leipzig filter. "Leipzig Neustadt..." and postal-code Leipzig
        # locations are retained. Other cities such as Grimma are excluded.
        if "leipzig" not in location.casefold():
            continue

        # Defensive check: avoid accidentally consuming the next date heading.
        if PLANLOS_DATE.match(title) or PLANLOS_DATE.match(location):
            continue

        event_url = ""
        # Find the title's actual link in the DOM where possible.
        # The text parser above remains the source of truth for fields.
        for a in soup.find_all("a", href=True):
            if clean(a.get_text(" ", strip=True)) == title:
                href = urljoin(PLANLOS_URL, a["href"])
                if "/event" in href.casefold() or href.startswith(PLANLOS_URL):
                    event_url = href
                    break

        events.append({
            "uid": "planlos-" + uid(
                title, start.isoformat(), location
            ),
            "title": title,
            "start": start,
            "end": end,
            "all_day": all_day,
            "location": location,
            "description": "Quelle: PLANLOS Leipzig",
            "url": event_url or PLANLOS_URL,
            "source": "PLANLOS",
        })

    return dedupe(events)


# ---------------------------------------------------------------------------
# SACHSENPUNK
#
# The live page is a simple text calendar:
#
#   ———— August 2026 ————
#   26.08. (Mi)
#   Leipzig – Venue – 20 Uhr! – Band...
#
# Crucially, events can wrap over multiple HTML text lines. We therefore
# continue consuming Leipzig lines until the next date heading.
# ---------------------------------------------------------------------------

SP_MONTH = re.compile(
    r"^—+\s*(Januar|Februar|März|April|Mai|Juni|Juli|August|"
    r"September|Oktober|November|Dezember)\s+(\d{4})\s*—+",
    re.I,
)

SP_DATE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.\s*\([^)]+\)$"
)

SP_TIME = re.compile(
    r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*Uhr!?",
    re.I,
)


def split_sp_event(line: str) -> tuple[str, str, str] | None:
    line = clean(line)
    line = line.replace("—", "–").replace("−", "–")
    if not line.casefold().startswith("leipzig"):
        return None

    rest = re.sub(r"^Leipzig\s*–\s*", "", line, flags=re.I)
    parts = [clean(p) for p in re.split(r"\s*–\s*", rest) if clean(p)]
    if len(parts) < 2:
        return None

    venue = parts[0]
    remainder = " – ".join(parts[1:])

    tm = SP_TIME.search(remainder)
    hour = int(tm.group(1)) if tm else None
    minute = int(tm.group(2) or 0) if tm else 0

    if tm:
        title = (
            remainder[:tm.start()] + remainder[tm.end():]
        )
        title = re.sub(r"^\s*–\s*", "", title)
        title = re.sub(r"\s*–\s*$", "", title)
        title = clean(title)
    else:
        title = clean(remainder)

    return venue, title, (hour, minute) if hour is not None else None


def load_location_map() -> dict[str, str]:
    """
    Build a venue -> URL map from the Sachsenpunk location directory.

    The directory itself provides the Leipzig venue names and links. We keep
    the URL in the event description so an iOS user can follow it; we do not
    make hundreds of external venue requests during every calendar build.
    """
    try:
        soup = BeautifulSoup(get(LOCATIONS_URL), "html.parser")
    except Exception as exc:
        print(f"WARNING: locations page unavailable: {exc}", file=sys.stderr)
        return {}

    result = {}
    for a in soup.find_all("a", href=True):
        text = clean(a.get_text(" ", strip=True))
        if not text.casefold().startswith("leipzig"):
            continue
        venue = re.sub(r"^Leipzig\s*[–-]\s*", "", text, flags=re.I)
        venue = clean(venue)
        if venue:
            result[venue.casefold()] = urljoin(LOCATIONS_URL, a["href"])
    return result


def parse_sachsenpunk() -> list[dict]:
    soup = BeautifulSoup(get(SACHSENPUNK_URL), "html.parser")
    text = soup.get_text("\n", strip=True)
    raw = [clean(x) for x in text.splitlines()]
    lines = [x for x in raw if x]

    location_urls = load_location_map()

    events = []
    year = None
    month = None
    current_date = None
    current_event = None

    def flush():
        nonlocal current_event
        if current_event:
            events.append(current_event)
            current_event = None

    for line in lines:
        mm = SP_MONTH.search(line)
        if mm:
            flush()
            month = MONTHS[mm.group(1).casefold()]
            year = int(mm.group(2))
            current_date = None
            continue

        dm = SP_DATE.match(line)
        if dm and year and month:
            flush()
            day = int(dm.group(1))
            # The page's second numeric field is the month. Use it when
            # available, because it protects against a missing month heading.
            numeric_month = int(dm.group(2))
            if 1 <= numeric_month <= 12:
                month = numeric_month
            try:
                current_date = datetime(
                    year, month, day, tzinfo=BERLIN
                )
            except ValueError:
                current_date = None
            continue

        parsed = split_sp_event(line)
        if parsed and current_date:
            flush()
            venue, title, t = parsed
            if not title:
                continue

            if t:
                start = current_date.replace(
                    hour=t[0], minute=t[1]
                )
                all_day = False
            else:
                start = current_date
                all_day = True

            venue_url = (
                location_urls.get(venue.casefold(), "")
            )

            current_event = {
                "uid": "sachsenpunk-" + uid(
                    start.date().isoformat(), venue, title
                ),
                "title": title,
                "start": start,
                "end": None,
                "all_day": all_day,
                "location": f"{venue}, Leipzig",
                "description": (
                    "Quelle: Sachsenpunk. "
                    + (f"Location: {venue_url}" if venue_url else "")
                ),
                "url": venue_url or SACHSENPUNK_URL,
                "source": "Sachsenpunk",
            }
            continue

        # Wrapped continuation lines belong to the previous Sachsenpunk event.
        # Do not absorb unrelated headings.
        if current_event and not SP_MONTH.search(line) and not SP_DATE.match(line):
            if not line.startswith(("SHOUTBOX", "DATES", "Date schicken")):
                current_event["title"] = clean(
                    current_event["title"] + " " + line
                )

    flush()
    return dedupe(events)


# ---------------------------------------------------------------------------
# ICS
# ---------------------------------------------------------------------------

def esc(s: str) -> str:
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    out = []
    while len(line.encode("utf-8")) > 74:
        n = 74
        while len(line[:n].encode("utf-8")) > 74:
            n -= 1
        out.append(line[:n])
        line = " " + line[n:]
    out.append(line)
    return "\r\n".join(out)


def make_ics(events: list[dict]) -> str:
    stamp = datetime.now(BERLIN).strftime("%Y%m%dT%H%M%S")
    lines = [
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
        lines += [
            "BEGIN:VEVENT",
            f"UID:{esc(e['uid'])}",
        ]

        if e["all_day"]:
            lines.append(
                "DTSTART;VALUE=DATE:"
                + e["start"].strftime("%Y%m%d")
            )
            if e.get("end"):
                lines.append(
                    "DTEND;VALUE=DATE:"
                    + e["end"].strftime("%Y%m%d")
                )
        else:
            lines.append(
                "DTSTART;TZID=Europe/Berlin:"
                + e["start"].strftime("%Y%m%dT%H%M%S")
            )
            if e.get("end"):
                lines.append(
                    "DTEND;TZID=Europe/Berlin:"
                    + e["end"].strftime("%Y%m%dT%H%M%S")
                )

        lines.append("SUMMARY:" + esc(e["title"]))
        if e.get("location"):
            lines.append("LOCATION:" + esc(e["location"]))
        if e.get("description"):
            lines.append("DESCRIPTION:" + esc(e["description"]))
        if e.get("url"):
            lines.append("URL:" + e["url"])

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in lines) + "\r\n"


def main() -> None:
    print("=" * 70)
    print("Events Leipzig Calendar")
    print("=" * 70)

    planlos = parse_planlos()
    print(f"PLANLOS Leipzig: {len(planlos)}")

    sachsenpunk = parse_sachsenpunk()
    print(f"Sachsenpunk Leipzig: {len(sachsenpunk)}")

    events = dedupe(planlos + sachsenpunk)

    # Keep today's and future events. The source pages are current calendars,
    # so there is no reason to retain old entries.
    today = datetime.now(BERLIN).date()
    events = [e for e in events if e["start"].date() >= today]

    print(f"TOTAL future/current: {len(events)}")

    # Safety rails. If either source suddenly breaks, do NOT publish a
    # partial calendar over the last known-good version.
    if len(planlos) < MIN_PLANLOS:
        raise RuntimeError(
            f"PLANLOS scrape incomplete: {len(planlos)} < {MIN_PLANLOS}. "
            "Existing ICS was NOT overwritten."
        )

    if len(sachsenpunk) < MIN_SACHSENPUNK:
        raise RuntimeError(
            f"Sachsenpunk scrape incomplete: {len(sachsenpunk)} < "
            f"{MIN_SACHSENPUNK}. Existing ICS was NOT overwritten."
        )

    if len(events) < MIN_TOTAL:
        raise RuntimeError(
            f"Combined scrape suspiciously small: {len(events)} < "
            f"{MIN_TOTAL}. Existing ICS was NOT overwritten."
        )

    OUT.write_text(make_ics(events), encoding="utf-8")
    print(f"WROTE {OUT}")
    print(f"VEVENTS: {len(events)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
