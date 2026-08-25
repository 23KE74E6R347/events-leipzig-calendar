#!/usr/bin/env python3
"""Build an iCalendar feed from PLANLOS Leipzig and Sachsenpunk Leipzig events."""

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

PLANLOS = "https://www.planlos-leipzig.org"
PLANLOS_FEEDS = (
    f"{PLANLOS}/events/feed/eo-events/?event_start_after=now",
    f"{PLANLOS}/feed/eo-events/?event_start_after=now",
)
SACHSEN_DATES = "https://sachsenpunk.de/dates/"

BERLIN = tz.gettz("Europe/Berlin")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Events-Leipzig-Calendar/1.0 "
        "(+https://github.com/23KE74E6R347/events-leipzig-calendar)"
    )
})

MONTHS = {
    "Januar": 1, "Februar": 2, "März": 3, "April": 4,
    "Mai": 5, "Juni": 6, "Juli": 7, "August": 8,
    "September": 9, "Oktober": 10, "November": 11, "Dezember": 12,
}


def get(url: str, timeout: int = 30) -> requests.Response:
    response = SESSION.get(url, timeout=timeout)
    response.raise_for_status()
    return response


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def ical_unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def unfold_ics(text: str) -> list[str]:
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def parse_ics_datetime(value: str, property_name: str = ""):
    value = value.strip()

    if "VALUE=DATE" in property_name:
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=BERLIN), True

    if value.endswith("Z"):
        return (
            datetime.strptime(value[:-1], "%Y%m%dT%H%M%S")
            .replace(tzinfo=tz.UTC)
            .astimezone(BERLIN),
            False,
        )

    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=BERLIN), False
        except ValueError:
            pass

    return None, False


def parse_ics(text: str, source: str) -> list[dict]:
    events = []
    block = []
    inside = False

    for line in unfold_ics(text):
        if line == "BEGIN:VEVENT":
            inside = True
            block = []
            continue

        if line == "END:VEVENT":
            if not inside:
                continue

            fields: dict[str, list[str]] = {}
            for item in block:
                if ":" not in item:
                    continue
                key, value = item.split(":", 1)
                fields.setdefault(key.upper(), []).append(value)

            summary = ical_unescape(fields.get("SUMMARY", [""])[0])
            start_key = next(
                (key for key in fields if key.startswith("DTSTART")),
                None,
            )

            if summary and start_key:
                start, all_day = parse_ics_datetime(
                    fields[start_key][0], start_key
                )
                if start:
                    end = None
                    end_key = next(
                        (key for key in fields if key.startswith("DTEND")),
                        None,
                    )
                    if end_key:
                        end, _ = parse_ics_datetime(
                            fields[end_key][0], end_key
                        )

                    events.append({
                        "uid": fields.get("UID", [str(uuid.uuid4())])[0],
                        "title": summary,
                        "start": start,
                        "end": end,
                        "all_day": all_day,
                        "location": ical_unescape(
                            fields.get("LOCATION", [""])[0]
                        ),
                        "description": ical_unescape(
                            fields.get("DESCRIPTION", [""])[0]
                        ),
                        "url": fields.get("URL", [""])[0],
                        "source": source,
                    })

            inside = False
            continue

        if inside:
            block.append(line)

    return events


def fetch_planlos_ics() -> list[dict]:
    for url in PLANLOS_FEEDS:
        try:
            response = get(url)
            if "BEGIN:VCALENDAR" in response.text:
                events = parse_ics(response.text, url)
                if events:
                    print(f"PLANLOS iCal: {len(events)}")
                    return events
        except Exception as exc:
            print(f"PLANLOS iCal failed: {url}: {exc}", file=sys.stderr)

    return []


def parse_planlos_html() -> list[dict]:
    """Fallback parser for PLANLOS event pages."""

    events = []

    try:
        page = get(PLANLOS).text
    except Exception as exc:
        print(f"PLANLOS HTML failed: {exc}", file=sys.stderr)
        return events

    soup = BeautifulSoup(page, "html.parser")
    links = []

    for anchor in soup.find_all("a", href=True):
        url = urljoin(PLANLOS, anchor["href"])
        if "/events/" in url:
            links.append(url)

    for url in dict.fromkeys(links):
        try:
            event_soup = BeautifulSoup(get(url).text, "html.parser")
            title_node = event_soup.find("h1") or event_soup.find("title")
            if not title_node:
                continue

            title = clean(title_node.get_text(" ", strip=True))
            text = clean(event_soup.get_text(" ", strip=True))

            date_match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", text)
            if not date_match:
                continue

            date = datetime.strptime(
                date_match.group(1), "%d.%m.%Y"
            ).replace(tzinfo=BERLIN)

            time_match = re.search(
                r"\b(\d{1,2}):(\d{2})\b"
                r"(?:\s*[-–]\s*(\d{1,2}):(\d{2}))?",
                text,
            )

            start = date
            end = None

            if time_match:
                start = date.replace(
                    hour=int(time_match.group(1)),
                    minute=int(time_match.group(2)),
                )
                if time_match.group(3):
                    end = date.replace(
                        hour=int(time_match.group(3)),
                        minute=int(time_match.group(4)),
                    )

            location = ""
            for heading in event_soup.find_all(["h2", "h3"]):
                if clean(heading.get_text()) == "Wo":
                    node = heading.find_next()
                    if node:
                        location = clean(node.get_text(" ", strip=True))
                    break

            if "leipzig" not in text.lower():
                continue

            events.append({
                "uid": f"planlos-{uuid.uuid5(uuid.NAMESPACE_URL, url)}",
                "title": title,
                "start": start,
                "end": end,
                "all_day": time_match is None,
                "location": location,
                "description": "",
                "url": url,
                "source": url,
            })

        except Exception as exc:
            print(f"PLANLOS event failed: {url}: {exc}", file=sys.stderr)

    return events


def load_aliases() -> dict[str, str]:
    if not LOCATIONS_FILE.exists():
        return {}
    try:
        data = json.loads(LOCATIONS_FILE.read_text(encoding="utf-8"))
        return data.get("aliases", {})
    except Exception as exc:
        print(f"Could not read locations.json: {exc}", file=sys.stderr)
        return {}


def normalize_location(name: str, aliases: dict[str, str]) -> tuple[str, str]:
    name = clean(name)

    for key, address in aliases.items():
        if name.casefold() == key.casefold():
            return name, address

    for key, address in aliases.items():
        if key.casefold() in name.casefold():
            return name, address

    return name, ""


def parse_sachsenpunk() -> list[dict]:
    soup = BeautifulSoup(get(SACHSEN_DATES).text, "html.parser")
    lines = [clean(line) for line in soup.get_text("\n").splitlines()]

    aliases = load_aliases()
    year = None
    month = None
    current_date = None
    events = []

    for line in lines:
        if not line:
            continue

        month_match = re.search(
            r"(Januar|Februar|März|April|Mai|Juni|Juli|August|"
            r"September|Oktober|November|Dezember)\s+(\d{4})",
            line,
        )

        if month_match:
            month = MONTHS[month_match.group(1)]
            year = int(month_match.group(2))
            current_date = None
            continue

        date_match = re.match(
            r"^(\d{1,2})\.(?:\d{2}\.)?\s*\(",
            line,
        )

        if date_match and year and month:
            current_date = datetime(
                year,
                month,
                int(date_match.group(1)),
                tzinfo=BERLIN,
            )
            continue

        if not current_date or not line.startswith("Leipzig"):
            continue

        parts = [
            clean(part)
            for part in re.split(r"\s+–\s+", line, maxsplit=3)
        ]

        if len(parts) < 3:
            continue

        city, venue, rest = parts[0], parts[1], parts[2]

        if city != "Leipzig":
            continue

        time_match = re.search(r"(\d{1,2}):(\d{2})\s*Uhr", rest)

        start = current_date
        if time_match:
            start = current_date.replace(
                hour=int(time_match.group(1)),
                minute=int(time_match.group(2)),
            )

        venue_name, address = normalize_location(venue, aliases)

        title = re.sub(
            r"\s*[-–]?\s*\d{1,2}:\d{2}\s*Uhr!?\s*[-–]?\s*",
            "",
            rest,
            count=1,
        )

        events.append({
            "uid": (
                "sachsenpunk-"
                + str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    line + current_date.isoformat(),
                ))
            ),
            "title": clean(title),
            "start": start,
            "end": None,
            "all_day": time_match is None,
            "location": (
                f"{venue_name}, {address}"
                if address
                else f"{venue_name}, Leipzig"
            ),
            "description": "Quelle: Sachsenpunk dates",
            "url": SACHSEN_DATES,
            "source": SACHSEN_DATES,
        })

    print(f"SACHSENPUNK Leipzig: {len(events)}")
    return events


def deduplicate(events: list[dict]) -> list[dict]:
    seen = set()
    result = []

    for event in events:
        key = (
            event["title"].casefold(),
            event["start"].isoformat(),
            event["location"].casefold(),
        )
        if key not in seen:
            seen.add(key)
            result.append(event)

    return sorted(
        result,
        key=lambda event: (event["start"], event["title"].casefold()),
    )


def ical_escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    pieces = []

    while len(line.encode("utf-8")) > 74:
        cut = 74
        while len(line[:cut].encode("utf-8")) > 74:
            cut -= 1
        pieces.append(line[:cut])
        line = " " + line[cut:]

    pieces.append(line)
    return "\r\n".join(pieces)


def make_ics(events: list[dict]) -> str:
    stamp = datetime.now(tz=BERLIN).strftime("%Y%m%dT%H%M%S")

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
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{ical_escape(event['uid'])}",
        ])

        if event["all_day"]:
            lines.append(
                "DTSTART;VALUE=DATE:"
                + event["start"].strftime("%Y%m%d")
            )
        else:
            lines.append(
                "DTSTART;TZID=Europe/Berlin:"
                + event["start"].strftime("%Y%m%dT%H%M%S")
            )

        if event.get("end"):
            if event["all_day"]:
                lines.append(
                    "DTEND;VALUE=DATE:"
                    + event["end"].strftime("%Y%m%d")
                )
            else:
                lines.append(
                    "DTEND;TZID=Europe/Berlin:"
                    + event["end"].strftime("%Y%m%dT%H%M%S")
                )

        lines.append(f"SUMMARY:{ical_escape(event['title'])}")

        if event.get("location"):
            lines.append(
                f"LOCATION:{ical_escape(event['location'])}"
            )

        if event.get("description"):
            lines.append(
                f"DESCRIPTION:{ical_escape(event['description'])}"
            )

        if event.get("url"):
            lines.append(f"URL:{event['url']}")

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


def main() -> None:
    planlos = fetch_planlos_ics()

    if not planlos:
        print("PLANLOS iCal unavailable; using HTML fallback.")
        planlos = parse_planlos_html()

    sachsenpunk = parse_sachsenpunk()

    events = deduplicate(planlos + sachsenpunk)

    cutoff = datetime.now(tz=BERLIN) - timedelta(days=2)
    events = [
        event for event in events
        if event["start"] >= cutoff
    ]

    if not events:
        raise RuntimeError(
            "No events were parsed; existing ICS was not overwritten."
        )

    OUT.write_text(make_ics(events), encoding="utf-8")

    print(f"PLANLOS total: {len(planlos)}")
    print(f"SACHSENPUNK Leipzig total: {len(sachsenpunk)}")
    print(f"TOTAL after dedupe/filter: {len(events)}")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
