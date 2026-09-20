"""
Fetches the list of trains actually scheduled for Біла Церква → Львів on given dates
from the UZ booking API (the same request the booking website makes as a guest) and
appends them to data/schedule.csv. The raw JSON for each date is also kept under
data/schedule-raw/ so the parser can be adjusted if the API shape changes.

Usage: python fetch_schedule.py            -> today and the next 2 days (Kyiv dates)
       python fetch_schedule.py 2026-09-24 -> one specific date
"""

import csv
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TRIPS_API_URL = "https://app.uz.gov.ua/api/v3/trips"
STATION_FROM_ID = "2200180"   # Біла Церква
STATION_TO_ID = "2218000"     # Львів

OUTPUT_CSV_PATH = Path("data/schedule.csv")
RAW_JSON_DIRECTORY = Path("data/schedule-raw")
KYIV_TIMEZONE = ZoneInfo("Europe/Kyiv")
DAYS_AHEAD_BY_DEFAULT = 2

CSV_COLUMNS = [
    "schedule_date",
    "train_number_api",       # as the API returns it, e.g. "085П"
    "train_number_delay_page",  # matched to our delay-page numbering, e.g. "85/86"
    "origin_station",
    "destination_station",
    "departure",
    "arrival",
    "fetched_at_kyiv",
]

# Delay-page numbers the analysis uses; "085П" -> 85 -> "85/86"
DELAY_PAGE_PAIRS = ["3/4", "5/6", "31/32", "39/40", "41/42", "61/62", "85/86",
                    "109/110", "233/234", "261/262", "285/286", "293/294"]


def build_request_headers() -> dict:
    """Mimics the booking website's guest request; X-Session-Id is a client-made UUID."""
    return {
        "Accept": "application/json",
        "Accept-Language": "uk-UA,uk;q=0.9",
        "Origin": "https://booking.uz.gov.ua",
        "Referer": "https://booking.uz.gov.ua/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "X-Client-Locale": "uk",
        "X-Session-Id": str(uuid.uuid4()),
        "X-User-Agent": "UZ/2 Web/1 User/guest",
    }


def fetch_trips_json(schedule_date: str) -> dict | list:
    response = requests.get(
        TRIPS_API_URL,
        params={
            "with_transfers": "0",
            "date": schedule_date,
            "station_from_id": STATION_FROM_ID,
            "station_to_id": STATION_TO_ID,
        },
        headers=build_request_headers(),
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"trips API returned HTTP {response.status_code} for {schedule_date}: {response.text[:300]}"
        )
    return response.json()


def save_raw_json(schedule_date: str, payload) -> None:
    RAW_JSON_DIRECTORY.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_JSON_DIRECTORY / f"{schedule_date}.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def to_delay_page_number(api_train_number: str) -> str:
    """'085П' -> '85/86'; '110О' -> '109/110'. Empty string if not one of ours."""
    digits_match = re.match(r"^0*(\d+)", api_train_number.strip())
    if digits_match is None:
        return ""
    train_number = int(digits_match.group(1))
    for pair in DELAY_PAGE_PAIRS:
        odd_number, even_number = pair.split("/")
        if train_number in (int(odd_number), int(even_number)):
            return pair
    return ""


def first_present(dictionary: dict, keys: list[str]):
    """Returns the value of the first key that exists in the dict, else None."""
    for key in keys:
        if key in dictionary and dictionary[key] not in (None, ""):
            return dictionary[key]
    return None


def station_name(value) -> str:
    """The API may give a station as a plain string or as an object with a name."""
    if isinstance(value, dict):
        name = first_present(value, ["name", "title", "station_name"])
        return str(name) if name is not None else ""
    if value is None:
        return ""
    return str(value)


def looks_like_trip(candidate: dict) -> bool:
    """A trip object carries a train number plus departure and arrival information."""
    train_number = extract_train_number(candidate)
    if train_number is None:
        return False
    has_departure = first_present(candidate, ["departure", "departure_time", "departure_date", "departure_at"]) is not None
    has_arrival = first_present(candidate, ["arrival", "arrival_time", "arrival_date", "arrival_at"]) is not None
    return has_departure and has_arrival


def extract_train_number(candidate: dict) -> str | None:
    train_field = candidate.get("train")
    if isinstance(train_field, dict):
        number = first_present(train_field, ["number", "train_number", "id"])
        if number is not None:
            return str(number)
    if isinstance(train_field, str) and train_field.strip():
        return train_field
    number = first_present(candidate, ["train_number", "number"])
    if number is not None:
        return str(number)
    return None


def walk_for_trips(node, found: list[dict]) -> None:
    """Recursively collects every dict that looks like a trip, wherever it sits in the JSON."""
    if isinstance(node, dict):
        if looks_like_trip(node):
            found.append(node)
            return  # do not descend into a trip's own nested objects
        for value in node.values():
            walk_for_trips(value, found)
    elif isinstance(node, list):
        for item in node:
            walk_for_trips(item, found)


def trips_to_rows(payload, schedule_date: str, fetched_at_kyiv: str) -> list[dict]:
    trip_objects: list[dict] = []
    walk_for_trips(payload, trip_objects)

    rows = []
    for trip in trip_objects:
        api_number = extract_train_number(trip) or ""
        train_field = trip.get("train") if isinstance(trip.get("train"), dict) else {}
        origin = first_present(trip, ["from", "station_from", "origin", "departure_station"])
        if origin is None:
            origin = first_present(train_field, ["from", "station_from", "origin"])
        destination = first_present(trip, ["to", "station_to", "destination", "arrival_station"])
        if destination is None:
            destination = first_present(train_field, ["to", "station_to", "destination"])
        rows.append(
            {
                "schedule_date": schedule_date,
                "train_number_api": api_number,
                "train_number_delay_page": to_delay_page_number(api_number),
                "origin_station": station_name(origin),
                "destination_station": station_name(destination),
                "departure": str(first_present(trip, ["departure", "departure_time", "departure_date", "departure_at"])),
                "arrival": str(first_present(trip, ["arrival", "arrival_time", "arrival_date", "arrival_at"])),
                "fetched_at_kyiv": fetched_at_kyiv,
            }
        )
    return rows


def already_recorded_dates() -> set[str]:
    if not OUTPUT_CSV_PATH.exists():
        return set()
    with OUTPUT_CSV_PATH.open(encoding="utf-8", newline="") as csv_file:
        return {row["schedule_date"] for row in csv.DictReader(csv_file)}


def append_rows_to_csv(rows: list[dict]) -> None:
    OUTPUT_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_already_exists = OUTPUT_CSV_PATH.exists()
    with OUTPUT_CSV_PATH.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        if not file_already_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(arguments: list[str]) -> int:
    now_kyiv = datetime.now(timezone.utc).astimezone(KYIV_TIMEZONE)
    fetched_at_kyiv = now_kyiv.strftime("%Y-%m-%d %H:%M")

    if arguments:
        dates_to_fetch = arguments
    else:
        dates_to_fetch = [
            (now_kyiv + timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(0, DAYS_AHEAD_BY_DEFAULT + 1)
        ]

    recorded_dates = already_recorded_dates()
    exit_code = 0
    for schedule_date in dates_to_fetch:
        if schedule_date in recorded_dates:
            print(f"{schedule_date}: already in {OUTPUT_CSV_PATH}, skipping")
            continue
        try:
            payload = fetch_trips_json(schedule_date)
        except Exception as error:
            print(f"ERROR fetching {schedule_date}: {error}", file=sys.stderr)
            exit_code = 1
            continue

        save_raw_json(schedule_date, payload)
        rows = trips_to_rows(payload, schedule_date, fetched_at_kyiv)
        if not rows:
            print(f"WARNING: {schedule_date}: response saved to {RAW_JSON_DIRECTORY} but no trips recognised in it", file=sys.stderr)
            exit_code = 1
            continue

        append_rows_to_csv(rows)
        watched = [row for row in rows if row["train_number_delay_page"]]
        print(f"{schedule_date}: {len(rows)} trains, {len(watched)} of them watched: "
              + ", ".join(f"{row['train_number_api']} {row['departure'][-5:]}" for row in watched))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
