#!/usr/bin/env python3
"""
Build Events Leipzig Calendar.

Sources:
- PLANLOS Leipzig
- Sachsenpunk, filtered to Leipzig

The generated events-leipzig.ics is intended for GitHub Pages/iOS
calendar subscriptions.
"""

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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "events-leipzig.ics"
LOCATIONS_FILE = ROOT / "locations.json"

PLANLOS = "https://www.planlos-leipzig.org"
SACHSENPUNK = "https://sachsenpunk.de"
SACHSEN_DATES = f"{SACHSENPUNK}/dates/"

BERLIN = tz.gettz("Europe/Berlin")

# Do not publish obviously broken/incomplete scrapes.
MIN_TOTAL_EVENTS = 80

# Keep a small safety margin for temporary source problems.
MIN_PLANLOS_EVENTS = 45
MIN_SACHSENPUNK_EVENTS = 20

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Events-Leipzig-Calendar/1.0 "
            "(+https://github.com/23KE74E6R347/events-leipzig-calendar)"
        )
    }
)

MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def get(url: str, timeout: int = 30) -> requests.Response:
    """GET a URL with a normal browser-like user agent."""
    response = SESSION.get(url, timeout=timeout)
    response.raise_for_status()
    return response


def clean(value: str) -> str:
    """Normalize whitespace and HTML entities."""
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def normalize_dash(value: str) -> str:
    """Normalize different Unicode dash characters."""
    return (
        value.replace("—", "–")
        .replace("−", "–")
        .replace("-", "–")
    )


def make_uid(source: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, source))


# ---------------------------------------------------------------------------
# Location handling
# ---------------------------------------------------------------------------

def load_aliases() -> dict[str, str]:
    if not LOCATIONS_FILE.exists():
        return {}

    try:
        data = json.loads(
            LOCATIONS_FILE.read_text(encoding="utf-8")
        )
        return data.get("aliases", {})
    except Exception as exc:
        print(
            f"WARNING: could not read locations.json: {exc}",
            file=sys.stderr,
        )
        return {}


def normalize_location(
    name: str,
    aliases: dict[str, str],
) -> tuple[str, str]:
    """
    Return (venue_name, address).

    Exact aliases are preferred, followed by substring matches.
    """
    name = clean(name)

    for key, address in aliases.items():
        if name.casefold() == key.casefold():
            return name, address

    for key, address in aliases.items():
        if key.casefold() in name.casefold():
            return name, address

    return name, ""


# ---------------------------------------------------------------------------
# PLANLOS
# ---------------------------------------------------------------------------

def parse_ics_datetime(
    value: str,
    property_name: str = "",
):
    value = value.strip()

    if "VALUE=DATE" in property_name:
        return (
            datetime.strptime(
                value[:8],
                "%Y%m%d",
            ).replace(tzinfo=BERLIN),
            True,
        )

    if value.endswith("Z"):
        return (
            datetime.strptime(
                value[:-1],
                "%Y%m%dT%H%M%S",
            )
            .replace(tzinfo=tz.UTC)
            .astimezone(BERLIN),
            False,
        )

    for fmt in (
        "%Y%m%dT%H%M%S",
        "%Y%m%dT%H%M",
    ):
        try:
            return (
                datetime.strptime(
                    value,
                    fmt,
                ).replace(tzinfo=BERLIN),
                False,
            )
        except ValueError:
            pass

    return None, False


def unfold_ics(text: str) -> list[str]:
    lines = []

    for line in (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
    ):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    return lines


def ical_unescape(value: str) -> str:
    return (
        value
        .replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def parse_ics(
    text: str,
    source: str,
) -> list[dict]:
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

                fields.setdefault(
                    key.upper(),
                    [],
                ).append(value)

            summary = ical_unescape(
                fields.get(
                    "SUMMARY",
                    [""],
                )[0]
            )

            start_key = next(
                (
                    key
                    for key in fields
                    if key.startswith("DTSTART")
                ),
                None,
            )

            if summary and start_key:

                start, all_day = parse_ics_datetime(
                    fields[start_key][0],
                    start_key,
                )

                if start:

                    end = None

                    end_key = next(
                        (
                            key
                            for key in fields
                            if key.startswith("DTEND")
                        ),
                        None,
                    )

                    if end_key:
                        end, _ = parse_ics_datetime(
                            fields[end_key][0],
                            end_key,
                        )

                    events.append(
                        {
                            "uid": fields.get(
                                "UID",
                                [make_uid(
                                    f"{source}|{summary}|{start}"
                                )],
                            )[0],
                            "title": summary,
                            "start": start,
                            "end": end,
                            "all_day": all_day,
                            "location": ical_unescape(
                                fields.get(
                                    "LOCATION",
                                    [""],
                                )[0]
                            ),
                            "description": ical_unescape(
                                fields.get(
                                    "DESCRIPTION",
                                    [""],
                                )[0]
                            ),
                            "url": fields.get(
                                "URL",
                                [""],
                            )[0],
                            "source": source,
                        }
                    )

            inside = False
            continue

        if inside:
            block.append(line)

    return events


def fetch_planlos_ics() -> list[dict]:
    """
    Try the historical PLANLOS Event Organiser iCal endpoints.

    They currently appear to return 404, so this is only a first attempt.
    """
    feeds = [
        f"{PLANLOS}/events/feed/eo-events/",
        f"{PLANLOS}/feed/eo-events/",
        f"{PLANLOS}/events/feed/",
    ]

    for url in feeds:

        try:
            response = get(url)

            if "BEGIN:VCALENDAR" not in response.text:
                continue

            events = parse_ics(
                response.text,
                url,
            )

            if events:
                print(
                    f"PLANLOS iCal: {len(events)} events"
                )
                return events

        except Exception as exc:
            print(
                f"PLANLOS iCal failed: {url}: {exc}",
                file=sys.stderr,
            )

    return []


def parse_planlos_listing_page(
    page_url: str,
) -> list[dict]:
    """
    Parse a PLANLOS calendar/listing page.

    The important part is that this parses the listing itself instead of
    requesting every event page individually. This avoids PLANLOS HTTP 429s.
    """

    try:
        response = get(page_url)
    except Exception as exc:
        print(
            f"PLANLOS page failed: {page_url}: {exc}",
            file=sys.stderr,
        )
        return []

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    # Event Organiser / WordPress event listings often expose events
    # as article/list-item elements. We first inspect semantic containers.
    containers = soup.select(
        "article, "
        ".event, "
        ".eo-event, "
        ".eventorganiser-event, "
        ".eo-events, "
        ".eo-event-list"
    )

    candidates = containers if containers else soup.find_all("li")

    events = []

    date_re = re.compile(
        r"(\d{1,2})\.(\d{1,2})\.(\d{4})"
    )

    time_re = re.compile(
        r"\b(\d{1,2}):(\d{2})\b"
        r"(?:\s*[-–]\s*(\d{1,2}):(\d{2}))?"
    )

    for container in candidates:

        text = clean(
            container.get_text(
                " ",
                strip=True,
            )
        )

        if not text:
            continue

        date_match = date_re.search(text)

        if not date_match:
            continue

        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = int(date_match.group(3))

        try:
            date = datetime(
                year,
                month,
                day,
                tzinfo=BERLIN,
            )
        except ValueError:
            continue

        # Only accept Leipzig events.
        if "leipzig" not in text.casefold():
            continue

        # Try semantic title elements first.
        title = ""

        title_node = container.select_one(
            "h1, h2, h3, h4, "
            ".event-title, "
            ".eo-event-title, "
            ".entry-title"
        )

        if title_node:
            title = clean(
                title_node.get_text(
                    " ",
                    strip=True,
                )
            )

        # Otherwise try the event link text.
        if not title:

            for anchor in container.find_all(
                "a",
                href=True,
            ):
                anchor_text = clean(
                    anchor.get_text(
                        " ",
                        strip=True,
                    )
                )

                href = urljoin(
                    PLANLOS,
                    anchor["href"],
                )

                if (
                    anchor_text
                    and "/events/" in href
                ):
                    title = anchor_text
                    break

        if not title:
            continue

        time_match = time_re.search(text)

        start = date
        end = None
        all_day = True

        if time_match:

            hour = int(time_match.group(1))
            minute = int(time_match.group(2))

            if hour <= 23:

                start = date.replace(
                    hour=hour,
                    minute=minute,
                )

                all_day = False

                if time_match.group(3):

                    end = date.replace(
                        hour=int(
                            time_match.group(3)
                        ),
                        minute=int(
                            time_match.group(4)
                        ),
                    )

        # Try to get a location from common semantic fields.
        location = ""

        location_node = container.select_one(
            ".event-location, "
            ".eo-event-location, "
            ".location, "
            "[class*='location']"
        )

        if location_node:
            location = clean(
                location_node.get_text(
                    " ",
                    strip=True,
                )
            )

        # Do not use the entire container as location.
        # If no dedicated location exists, leave it empty.
        event_url = ""

        for anchor in container.find_all(
            "a",
            href=True,
        ):
            href = urljoin(
                PLANLOS,
                anchor["href"],
            )

            if "/events/" in href:
                event_url = href
                break

        uid_source = (
            f"planlos|"
            f"{title}|"
            f"{start.isoformat()}|"
            f"{location}|"
            f"{event_url}"
        )

        events.append(
            {
                "uid": (
                    "planlos-"
                    + make_uid(uid_source)
                ),
                "title": title,
                "start": start,
                "end": end,
                "all_day": all_day,
                "location": location,
                "description": "",
                "url": event_url,
                "source": page_url,
            }
        )

    return events


def parse_planlos_html() -> list[dict]:
    """
    Crawl PLANLOS listing/calendar pages.

    We deliberately do NOT request individual event pages.
    """

    pages = [
        PLANLOS,
        f"{PLANLOS}/test/",
        f"{PLANLOS}/events/",
    ]

    events = []

    for page in pages:

        page_events = parse_planlos_listing_page(
            page
        )

        print(
            f"PLANLOS listing {page}: "
            f"{len(page_events)} events"
        )

        events.extend(page_events)

    events = deduplicate(events)

    print(
        f"PLANLOS listing total: "
        f"{len(events)}"
    )

    return events


# ---------------------------------------------------------------------------
# Sachsenpunk
# ---------------------------------------------------------------------------

def parse_sachsenpunk() -> list[dict]:
    """
    Parse the Sachsenpunk /dates/ page.

    Expected structure:

    Leipzig – Venue – Event – 20 Uhr!
    """

    response = get(
        SACHSEN_DATES
    )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    lines = [
        clean(line)
        for line
        in soup.get_text("\n").splitlines()
    ]

    lines = [
        line
        for line in lines
        if line
    ]

    aliases = load_aliases()

    events = []

    current_year = None
    current_month = None
    current_date = None

    month_re = re.compile(
        r"(Januar|Februar|März|April|Mai|Juni|Juli|August|"
        r"September|Oktober|November|Dezember)"
        r"\s+(\d{4})",
        re.IGNORECASE,
    )

    date_re = re.compile(
        r"^(\d{1,2})\.(\d{2})\.\s*\("
    )

    for line in lines:

        # Month/year heading
        month_match = month_re.search(
            line
        )

        if month_match:

            month_name = month_match.group(
                1
            ).casefold()

            current_month = MONTHS.get(
                month_name
            )

            current_year = int(
                month_match.group(2)
            )

            current_date = None

            continue

        # Date heading
        date_match = date_re.match(
            line
        )

        if (
            date_match
            and current_year
            and current_month
        ):

            day = int(
                date_match.group(1)
            )

            # Sachsenpunk repeats the month number
            # in the date heading.
            month_number = int(
                date_match.group(2)
            )

            if 1 <= month_number <= 12:
                current_month = month_number

            try:
                current_date = datetime(
                    current_year,
                    current_month,
                    day,
                    tzinfo=BERLIN,
                )
            except ValueError:
                current_date = None

            continue

        if not current_date:
            continue

        # Only Leipzig.
        if not line.casefold().startswith(
            "leipzig"
        ):
            continue

        normalized = normalize_dash(
            line
        )

        # Remove "Leipzig –"
        payload = re.sub(
            r"^Leipzig\s*–\s*",
            "",
            normalized,
            flags=re.IGNORECASE,
        )

        # Sachsenpunk uses dashes as separators.
        parts = [
            clean(part)
            for part
            in re.split(
                r"\s*–\s*",
                payload,
            )
            if clean(part)
        ]

        if len(parts) < 2:
            continue

        venue = parts[0]

        remainder = " – ".join(
            parts[1:]
        )

        # Time can appear anywhere at the end,
        # e.g. "20 Uhr!".
        time_match = re.search(
            r"(?<!\d)"
            r"(\d{1,2})"
            r"(?:[:.](\d{2}))?"
            r"\s*Uhr!?"
            r"\b",
            remainder,
            flags=re.IGNORECASE,
        )

        start = current_date
        all_day = True

        if time_match:

            hour = int(
                time_match.group(1)
            )

            minute = int(
                time_match.group(2)
                or 0
            )

            if hour <= 23:

                start = current_date.replace(
                    hour=hour,
                    minute=minute,
                )

                all_day = False

        title = re.sub(
            r"\s*[-–—]?\s*"
            r"(?<!\d)"
            r"\d{1,2}"
            r"(?:[:.]\d{2})?"
            r"\s*Uhr!?"
            r"\b",
            "",
            remainder,
            flags=re.IGNORECASE,
        )

        title = clean(
            title
        ).strip(
            "–—- "
        )

        venue_name, address = (
            normalize_location(
                venue,
                aliases,
            )
        )

        if address:
            location = (
                f"{venue_name}, "
                f"{address}"
            )
        else:
            location = (
                f"{venue_name}, Leipzig"
            )

        uid_source = (
            f"sachsenpunk|"
            f"{current_date.date()}|"
            f"{venue_name}|"
            f"{title}|"
            f"{start.time()}"
        )

        events.append(
            {
                "uid": (
                    "sachsenpunk-"
                    + make_uid(uid_source)
                ),
                "title": title,
                "start": start,
                "end": None,
                "all_day": all_day,
                "location": location,
                "description": (
                    "Quelle: Sachsenpunk dates"
                ),
                "url": SACHSEN_DATES,
                "source": SACHSEN_DATES,
            }
        )

    events = deduplicate(
        events
    )

    print(
        "SACHSENPUNK Leipzig: "
        f"{len(events)}"
    )

    return events


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(
    events: list[dict],
) -> list[dict]:

    seen = set()
    result = []

    for event in events:

        key = (
            event["title"]
            .casefold(),
            event["start"]
            .isoformat(),
            event["location"]
            .casefold(),
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(event)

    return sorted(
        result,
        key=lambda event: (
            event["start"],
            event["title"]
            .casefold(),
        ),
    )


# ---------------------------------------------------------------------------
# ICS generation
# ---------------------------------------------------------------------------

def ical_escape(
    value: str,
) -> str:

    return (
        str(value)
        .replace(
            "\\",
            "\\\\",
        )
        .replace(
            ";",
            "\\;",
        )
        .replace(
            ",",
            "\\,",
        )
        .replace(
            "\r",
            "",
        )
        .replace(
            "\n",
            "\\n",
        )
    )


def fold_ical_line(
    line: str,
) -> str:

    pieces = []

    while len(
        line.encode("utf-8")
    ) > 74:

        cut = 74

        while len(
            line[:cut].encode("utf-8")
        ) > 74:
            cut -= 1

        pieces.append(
            line[:cut]
        )

        line = (
            " "
            + line[cut:]
        )

    pieces.append(line)

    return "\r\n".join(
        pieces
    )


def make_ics(
    events: list[dict],
) -> str:

    timestamp = datetime.now(
        tz=BERLIN
    ).strftime(
        "%Y%m%dT%H%M%S"
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        (
            "PRODID:"
            "//23KE74E6R347"
            "//Events Leipzig Calendar"
            "//DE"
        ),
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        (
            "X-WR-CALNAME:"
            "Events Leipzig Calendar"
        ),
        (
            "X-WR-TIMEZONE:"
            "Europe/Berlin"
        ),
        f"DTSTAMP:{timestamp}",
    ]

    for event in events:

        lines.extend(
            [
                "BEGIN:VEVENT",
                (
                    "UID:"
                    + ical_escape(
                        event["uid"]
                    )
                ),
            ]
        )

        if event["all_day"]:

            lines.append(
                (
                    "DTSTART;VALUE=DATE:"
                    + event["start"].strftime(
                        "%Y%m%d"
                    )
                )
            )

        else:

            lines.append(
                (
                    "DTSTART;TZID="
                    "Europe/Berlin:"
                    + event["start"].strftime(
                        "%Y%m%dT%H%M%S"
                    )
                )
            )

        if event.get("end"):

            if event["all_day"]:

                lines.append(
                    (
                        "DTEND;VALUE=DATE:"
                        + event["end"].strftime(
                            "%Y%m%d"
                        )
                    )
                )

            else:

                lines.append(
                    (
                        "DTEND;TZID="
                        "Europe/Berlin:"
                        + event["end"].strftime(
                            "%Y%m%dT%H%M%S"
                        )
                    )
                )

        lines.append(
            "SUMMARY:"
            + ical_escape(
                event["title"]
            )
        )

        if event.get(
            "location"
        ):

            lines.append(
                "LOCATION:"
                + ical_escape(
                    event["location"]
                )
            )

        if event.get(
            "description"
        ):

            lines.append(
                "DESCRIPTION:"
                + ical_escape(
                    event["description"]
                )
            )

        if event.get(
            "url"
        ):

            lines.append(
                "URL:"
                + event["url"]
            )

        lines.append(
            "END:VEVENT"
        )

    lines.append(
        "END:VCALENDAR"
    )

    return (
        "\r\n".join(
            fold_ical_line(line)
            for line in lines
        )
        + "\r\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:

    print("=" * 70)
    print("Events Leipzig Calendar")
    print("=" * 70)

    # PLANLOS
    planlos = fetch_planlos_ics()

    if not planlos:

        print(
            "PLANLOS iCal unavailable."
        )

        planlos = parse_planlos_html()

    # Sachsenpunk
    sachsenpunk = parse_sachsenpunk()

    # Combined
    events = deduplicate(
        planlos
        + sachsenpunk
    )

    # Remove only clearly stale events.
    # Keep today's events.
    cutoff = (
        datetime.now(
            tz=BERLIN
        )
        - timedelta(
            days=2
        )
    )

    events = [
        event
        for event in events
        if event["start"]
        >= cutoff
    ]

    print()
    print(
        f"PLANLOS: "
        f"{len(planlos)}"
    )

    print(
        f"SACHSENPUNK Leipzig: "
        f"{len(sachsenpunk)}"
    )

    print(
        f"TOTAL: "
        f"{len(events)}"
    )

    print()

    # ------------------------------------------------------------------
    # Safety checks
    # ------------------------------------------------------------------

    if len(planlos) < MIN_PLANLOS_EVENTS:

        raise RuntimeError(
            "PLANLOS scrape looks incomplete: "
            f"{len(planlos)} events found, "
            f"minimum expected is "
            f"{MIN_PLANLOS_EVENTS}. "
            "Existing ICS was NOT overwritten."
        )

    if (
        len(sachsenpunk)
        < MIN_SACHSENPUNK_EVENTS
    ):

        raise RuntimeError(
            "Sachsenpunk scrape looks incomplete: "
            f"{len(sachsenpunk)} Leipzig events found, "
            f"minimum expected is "
            f"{MIN_SACHSENPUNK_EVENTS}. "
            "Existing ICS was NOT overwritten."
        )

    if (
        len(events)
        < MIN_TOTAL_EVENTS
    ):

        raise RuntimeError(
            "Combined scrape looks incomplete: "
            f"{len(events)} events found, "
            f"minimum expected is "
            f"{MIN_TOTAL_EVENTS}. "
            "Existing ICS was NOT overwritten."
        )

    # ------------------------------------------------------------------
    # Write only after all checks pass.
    # ------------------------------------------------------------------

    OUT.write_text(
        make_ics(events),
        encoding="utf-8",
    )

    print(
        f"Successfully wrote: "
        f"{OUT}"
    )

    print(
        f"VEVENT count: "
        f"{len(events)}"
    )


if __name__ == "__main__":
    main()
