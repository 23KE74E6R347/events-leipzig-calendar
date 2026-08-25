#!/usr/bin/env python3
from __future__ import annotations

import html
import json
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
LOCATIONS_FILE = ROOT / "locations.json"

PLANLOS_URL = "https://www.planlos-leipzig.org/"
SACHSENPUNK_URL = "https://sachsenpunk.de/dates/"
LOCATIONS_URL = "https://sachsenpunk.de/locations-gruppen-festivals/#locations"

BERLIN = tz.gettz("Europe/Berlin")

# Safety rails. These are deliberately below the current live counts.
# A broken scrape must never overwrite the last known-good ICS.
MIN_PLANLOS = 30
MIN_SACHSENPUNK = 20
MIN_TOTAL = 70

MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (compatible; Events-Leipzig-Calendar/3.0; "
        "+https://github.com/23KE74E6R347/events-leipzig-calendar)"
    ),
    "Accept": "text/html,application/xhtml+xml",
})

# PLANLOS live structure:
#   Di., 25. August 2026
#   19:00 - 23:15  | EVENT TITLE
#   Index, Leipzig
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
    r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s*[-–]\s*"
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})$"
)

# Sachsenpunk live structure:
#   ———— August 2026 ————
#   26.08. (Mi)
#   Leipzig – XonXanXop – Qualm + H.C: Behrendsten
SP_MONTH = re.compile(
    r"^—+\s*(Januar|Februar|März|April|Mai|Juni|Juli|August|"
    r"September|Oktober|November|Dezember)\s+(\d{4})\s*—+",
    re.I,
)
SP_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.\s*\([^)]+\)$")
SP_TIME = re.compile(
    r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*Uhr!?",
    re.I,
)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def make_uid(*parts: object) -> str:
    raw = "|".join(map(str, parts))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def fetch_html(url: str) -> str:
    response = SESSION.get(url, timeout=(10, 30))
    response.raise_for_status()
    return response.text


def deduplicate(events: list[dict]) -> list[dict]:
    seen = set()
    result = []

    for event in events:
        key = (
            event["title"].casefold(),
            event["start"].isoformat(),
            event["location"].casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(event)

    return sorted(
        result,
        key=lambda e: (e["start"], e["title"].casefold()),
    )


def load_aliases() -> dict[str, str]:
    if not LOCATIONS_FILE.exists():
        return {}

    try:
        data = json.loads(LOCATIONS_FILE.read_text(encoding="utf-8"))
        aliases = data.get("aliases", {})
        return {
            clean(k).casefold(): clean(v)
            for k, v in aliases.items()
        }
    except Exception as exc:
        print(f"WARNING: locations.json: {exc}", file=sys.stderr)
        return {}


def location_with_address(venue: str, aliases: dict[str, str]) -> str:
    venue_clean = clean(venue)
    key = venue_clean.casefold()

    if key in aliases:
        address = aliases[key]
        return f"{venue_clean}, {address}" if address else venue_clean

    # Handle variants such as "Werk 2 (Halle D)" vs "Werk 2".
    for alias, address in aliases.items():
        if alias in key or key in alias:
            return f"{venue_clean}, {address}" if address else venue_clean

    return f"{venue_clean}, Leipzig"


def parse_planlos() -> list[dict]:
    """
    Parse the single server-rendered PLANLOS calendar page.

    No individual event pages are requested. This is intentional: the old
    implementation triggered PLANLOS 429 responses by fetching every event.
    """
    html_text = fetch_html(PLANLOS_URL)
    soup = BeautifulSoup(html_text, "html.parser")

    # Build the link lookup once. The old parser searched every <a> for every
    # event, which was unnecessarily expensive.
    link_by_text: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        label = clean(anchor.get_text(" ", strip=True))
        if label:
            link_by_text.setdefault(
                label.casefold(),
                urljoin(PLANLOS_URL, anchor["href"]),
            )

    lines = [
        clean(x)
        for x in soup.get_text("\n", strip=True).splitlines()
    ]
    lines = [x for x in lines if x]

    events: list[dict] = []
    current_date: datetime | None = None
    i = 0

    while i < len(lines):
        line = lines[i]

        date_match = PLANLOS_DATE.match(line)
        if date_match:
            day = int(date_match.group(1))
            month = MONTHS[date_match.group(2).casefold()]
            year = int(date_match.group(3))
            current_date = datetime(
                year, month, day, tzinfo=BERLIN
            )
            i += 1
            continue

        if current_date is None:
            i += 1
            continue

        # Timed event:
        #   19:00 - 23:15
        #   TITLE
        #   LOCATION
        time_match = PLANLOS_TIME.match(line)

        if time_match:
            start = current_date.replace(
                hour=int(time_match.group(1)),
                minute=int(time_match.group(2)),
            )

            end = None
            if time_match.group(3):
                end = current_date.replace(
                    hour=int(time_match.group(3)),
                    minute=int(time_match.group(4)),
                )

            i += 1

            # Some versions of Event Organiser put the date range between
            # the time and title.
            if i < len(lines) and PLANLOS_RANGE.match(lines[i]):
                i += 1

            if i + 1 >= len(lines):
                continue

            title = lines[i]
            location = lines[i + 1]
            i += 2

            if not title or not location:
                continue

            # Only Leipzig locations belong in this calendar.
            if "leipzig" not in location.casefold():
                continue

            # Don't accidentally consume the next date heading.
            if PLANLOS_DATE.match(title) or PLANLOS_DATE.match(location):
                continue

            events.append({
                "uid": "planlos-" + make_uid(
                    title, start.isoformat(), location
                ),
                "title": title,
                "start": start,
                "end": end,
                "all_day": False,
                "location": location,
                "description": "Quelle: PLANLOS Leipzig",
                "url": link_by_text.get(
                    title.casefold(), PLANLOS_URL
                ),
                "source": "PLANLOS",
            })
            continue

        # All-day multi-day event:
        #   Ganztägig
        #   26.08.2026 - 31.08.2026
        #   TITLE
        #   LOCATION
        if line.casefold() == "ganztägig":
            i += 1

            if i >= len(lines):
                continue

            range_match = PLANLOS_RANGE.match(lines[i])
            if not range_match:
                continue

            start = datetime(
                int(range_match.group(3)),
                int(range_match.group(2)),
                int(range_match.group(1)),
                tzinfo=BERLIN,
            )
            end = datetime(
                int(range_match.group(6)),
                int(range_match.group(5)),
                int(range_match.group(4)),
                tzinfo=BERLIN,
            ) + timedelta(days=1)

            i += 1
            if i + 1 >= len(lines):
                continue

            title = lines[i]
            location = lines[i + 1]
            i += 2

            if "leipzig" not in location.casefold():
                continue

            events.append({
                "uid": "planlos-" + make_uid(
                    title, start.isoformat(), location
                ),
                "title": title,
                "start": start,
                "end": end,
                "all_day": True,
                "location": location,
                "description": "Quelle: PLANLOS Leipzig",
                "url": link_by_text.get(
                    title.casefold(), PLANLOS_URL
                ),
                "source": "PLANLOS",
            })
            continue

        i += 1

    return deduplicate(events)


def parse_sachsenpunk() -> list[dict]:
    """
    Parse Sachsenpunk's /dates/ page in one request.

    Each Leipzig event is represented by one visible line:
      Leipzig – VENUE – optional time – TITLE

    The location directory is used only for the separate location URL/address
    lookup; it is never crawled event-by-event.
    """
    html_text = fetch_html(SACHSENPUNK_URL)
    soup = BeautifulSoup(html_text, "html.parser")

    lines = [
        clean(x)
        for x in soup.get_text("\n", strip=True).splitlines()
    ]
    lines = [x for x in lines if x]

    aliases = load_aliases()

    events: list[dict] = []
    year: int | None = None
    month: int | None = None
    current_date: datetime | None = None

    for line in lines:
        month_match = SP_MONTH.match(line)
        if month_match:
            month = MONTHS[month_match.group(1).casefold()]
            year = int(month_match.group(2))
            current_date = None
            continue

        date_match = SP_DATE.match(line)
        if date_match and year:
            month_from_line = int(date_match.group(2))
            if 1 <= month_from_line <= 12:
                month = month_from_line

            try:
                current_date = datetime(
                    year,
                    month,
                    int(date_match.group(1)),
                    tzinfo=BERLIN,
                )
            except ValueError:
                current_date = None
            continue

        if current_date is None:
            continue

        if not line.casefold().startswith("leipzig"):
            continue

        normalized = (
            line.replace("—", "–")
                .replace("−", "–")
        )

        rest = re.sub(
            r"^Leipzig\s*–\s*",
            "",
            normalized,
            flags=re.I,
        )

        parts = [
            clean(part)
            for part in re.split(r"\s*–\s*", rest)
            if clean(part)
        ]

        if len(parts) < 2:
            continue

        venue = parts[0]
        remainder = " – ".join(parts[1:])

        time_match = SP_TIME.search(remainder)

        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)

            if hour > 23 or minute > 59:
                continue

            start = current_date.replace(
                hour=hour,
                minute=minute,
            )
            all_day = False

            title = clean(
                remainder[:time_match.start()]
                + " "
                + remainder[time_match.end():]
            ).strip("–—- ")
        else:
            start = current_date
            all_day = True
            title = clean(remainder).strip("–—- ")

        if not title:
            continue

        location = location_with_address(
            venue,
            aliases,
        )

        events.append({
            "uid": "sachsenpunk-" + make_uid(
                start.date().isoformat(),
                venue,
                title,
            ),
            "title": title,
            "start": start,
            "end": None,
            "all_day": all_day,
            "location": location,
            "description": "Quelle: Sachsenpunk",
            "url": SACHSENPUNK_URL,
            "source": "Sachsenpunk",
        })

    return deduplicate(events)


def ical_escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def fold_ical_line(line: str) -> str:
    pieces = []

    while len(line.encode("utf-8")) > 74:
        n = 74
        while len(line[:n].encode("utf-8")) > 74:
            n -= 1

        pieces.append(line[:n])
        line = " " + line[n:]

    pieces.append(line)
    return "\r\n".join(pieces)


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

    for event in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ical_escape(event['uid'])}",
        ]

        if event["all_day"]:
            lines.append(
                "DTSTART;VALUE=DATE:"
                + event["start"].strftime("%Y%m%d")
            )
            if event.get("end"):
                lines.append(
                    "DTEND;VALUE=DATE:"
                    + event["end"].strftime("%Y%m%d")
                )
        else:
            lines.append(
                "DTSTART;TZID=Europe/Berlin:"
                + event["start"].strftime("%Y%m%dT%H%M%S")
            )
            if event.get("end"):
                lines.append(
                    "DTEND;TZID=Europe/Berlin:"
                    + event["end"].strftime("%Y%m%dT%H%M%S")
                )

        lines.append(
            "SUMMARY:" + ical_escape(event["title"])
        )

        if event.get("location"):
            lines.append(
                "LOCATION:" + ical_escape(event["location"])
            )

        if event.get("description"):
            lines.append(
                "DESCRIPTION:" + ical_escape(event["description"])
            )

        if event.get("url"):
            lines.append(
                "URL:" + event["url"]
            )

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    return "\r\n".join(
        fold_ical_line(line)
        for line in lines
    ) + "\r\n"


def self_test() -> None:
    """Offline structural regression tests for the live formats we observed."""
    assert PLANLOS_DATE.match("Di., 25. August 2026")
    assert PLANLOS_TIME.match("19:00 - 23:15")
    assert PLANLOS_TIME.match("19:00")
    assert PLANLOS_RANGE.match("26.08.2026 - 31.08.2026")

    assert SP_MONTH.match("———— August 2026 ————")
    assert SP_DATE.match("26.08. (Mi)")
    assert SP_TIME.search("20 Uhr! – Hanakuso + Howling Setta")

    sample_sp = "Leipzig – Klubraum Plagwitz – 20 Uhr! – Hanakuso + Howling Setta + Hassan K."
    rest = re.sub(r"^Leipzig\s*–\s*", "", sample_sp, flags=re.I)
    parts = [clean(x) for x in re.split(r"\s*–\s*", rest) if clean(x)]
    assert parts[0] == "Klubraum Plagwitz"
    assert "Hanakuso" in " – ".join(parts[1:])

    sample_planlos = [
        "Di., 25. August 2026",
        "19:00 - 23:15",
        "Der ’technologische Schleier’. Anmerkungen zu einem Sprachbild bei Adorno (KI-Reihe)",
        "Index, Leipzig",
        "19:00",
        "Kino für Alle - Kein Land für Niemand",
        "Meuterei, Leipzig",
    ]

    current = None
    parsed = []
    i = 0
    while i < len(sample_planlos):
        m = PLANLOS_DATE.match(sample_planlos[i])
        if m:
            current = datetime(
                int(m.group(3)),
                MONTHS[m.group(2).casefold()],
                int(m.group(1)),
                tzinfo=BERLIN,
            )
            i += 1
            continue

        t = PLANLOS_TIME.match(sample_planlos[i])
        if t and current:
            parsed.append((
                sample_planlos[i + 1],
                sample_planlos[i + 2],
            ))
            i += 3
            continue
        i += 1

    assert len(parsed) == 2
    assert parsed[0][1] == "Index, Leipzig"
    assert parsed[1][1] == "Meuterei, Leipzig"

    print("SELF-TEST OK")


def main() -> None:
    if "--self-test" in sys.argv:
        self_test()
        return

    print("=" * 70)
    print("Events Leipzig Calendar")
    print("=" * 70)

    # Each source is fetched exactly once.
    planlos = parse_planlos()
    sachsenpunk = parse_sachsenpunk()

    today = datetime.now(BERLIN).date()

    # Do not keep old events in the subscription.
    planlos = [
        e for e in planlos
        if e["start"].date() >= today
    ]
    sachsenpunk = [
        e for e in sachsenpunk
        if e["start"].date() >= today
    ]

    events = deduplicate(planlos + sachsenpunk)

    print(f"PLANLOS Leipzig:     {len(planlos)}")
    print(f"Sachsenpunk Leipzig: {len(sachsenpunk)}")
    print(f"TOTAL:               {len(events)}")

    # CRITICAL: all validation happens before touching events-leipzig.ics.
    if len(planlos) < MIN_PLANLOS:
        raise RuntimeError(
            f"PLANLOS scrape suspiciously small: {len(planlos)} "
            f"< {MIN_PLANLOS}. Existing ICS was NOT overwritten."
        )

    if len(sachsenpunk) < MIN_SACHSENPUNK:
        raise RuntimeError(
            f"Sachsenpunk scrape suspiciously small: {len(sachsenpunk)} "
            f"< {MIN_SACHSENPUNK}. Existing ICS was NOT overwritten."
        )

    if len(events) < MIN_TOTAL:
        raise RuntimeError(
            f"Combined scrape suspiciously small: {len(events)} "
            f"< {MIN_TOTAL}. Existing ICS was NOT overwritten."
        )

    ics = make_ics(events)

    # Sanity check the generated calendar before publishing it.
    vevents = ics.count("BEGIN:VEVENT")
    if vevents != len(events):
        raise RuntimeError(
            f"ICS sanity check failed: {vevents} VEVENTs for "
            f"{len(events)} parsed events."
        )

    OUT.write_text(ics, encoding="utf-8")

    print(f"WROTE: {OUT}")
    print(f"VEVENTS: {vevents}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
