"""
Fetches https://uz-vezemo.uz.gov.ua/delayform, keeps only the trains listed in
trains.json (going in the watched direction), and appends one row per watched
train to data/delays.csv - including trains that are NOT on the delay page,
so that "checked and on time" is recorded explicitly.

Exits with a non-zero code (so the GitHub Actions run turns red) if the page
does not look like the delay page any more.
"""

import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

DELAY_PAGE_URL = "https://uz-vezemo.uz.gov.ua/delayform"
WATCHED_TRAINS_PATH = Path("trains.json")
OUTPUT_CSV_PATH = Path("data/delays.csv")
DEBUG_PAGE_PATH = Path("debug/last_page.html")   # what the runner actually received; uploaded on failure
KYIV_TIMEZONE = ZoneInfo("Europe/Kyiv")

CSV_COLUMNS = [
    "observed_at_utc",
    "observed_at_kyiv",
    "site_updated_at",
    "train_number",
    "on_delay_page",
    "delay_minutes",
    "scheduled_departure_bila_tserkva",
    "scheduled_arrival_lviv",
    "departure_date",
    "origin_station",
    "destination_station",
    "forecast_arrival",
    "scheduled_arrival",
    "status",
    "forecast_reliability",
    "reason",
    # per-station detail rows (added when the site started publishing a stop list per train)
    "bila_tserkva_delay_minutes",
    "bila_tserkva_forecast",
    "bila_tserkva_scheduled",
    "bila_tserkva_passed",
    "lviv_delay_minutes",
    "lviv_forecast",
    "lviv_scheduled",
    "lviv_passed",
]

STATION_DETAIL_COLUMNS = {
    "БІЛА ЦЕРКВА": "bila_tserkva",
    "ЛЬВІВ": "lviv",
}

# Browser-like headers: the site returned HTTP 500 to a custom non-browser User-Agent.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
}
FETCH_ATTEMPTS = 3
SECONDS_BETWEEN_ATTEMPTS = 20


# --------------------------------------------------------------------------- fetch & parse

def save_debug_page(html: str) -> None:
    DEBUG_PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_PAGE_PATH.write_text(html, encoding="utf-8")


def fetch_page_html() -> str:
    """Fetches the page, retrying a few times; keeps the last response body for debugging."""
    last_error_description = ""
    for attempt_number in range(1, FETCH_ATTEMPTS + 1):
        try:
            response = requests.get(DELAY_PAGE_URL, headers=REQUEST_HEADERS, timeout=30)
        except requests.RequestException as request_error:
            last_error_description = f"request failed: {request_error}"
            print(f"Attempt {attempt_number}/{FETCH_ATTEMPTS}: {last_error_description}", file=sys.stderr)
        else:
            save_debug_page(response.text)  # always keep what we got, even an error page
            if response.status_code == 200:
                return response.text
            last_error_description = f"HTTP {response.status_code}, {len(response.text)} characters in body"
            print(f"Attempt {attempt_number}/{FETCH_ATTEMPTS}: {last_error_description}", file=sys.stderr)

        if attempt_number < FETCH_ATTEMPTS:
            time.sleep(SECONDS_BETWEEN_ATTEMPTS)

    raise SystemExit(f"ERROR: could not fetch {DELAY_PAGE_URL} after {FETCH_ATTEMPTS} attempts ({last_error_description})")


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_delay_to_minutes(delay_text: str) -> int | None:
    """'+6:07' -> 367, '+0:38' -> 38, '-0:03' -> -3. Returns None for '—' or anything else."""
    match = re.match(r"^([+-]?)(\d+):(\d{2})$", delay_text)
    if match is None:
        return None
    sign = -1 if match.group(1) == "-" else 1
    hours = int(match.group(2))
    minutes = int(match.group(3))
    return sign * (hours * 60 + minutes)


def split_train_number_and_date(cell_text: str) -> tuple[str, str]:
    """'85/86 14.09' -> ('85/86', '14.09'); '3/4' -> ('3/4', '')."""
    parts = cell_text.split()
    if len(parts) == 0:
        return "", ""
    train_number = parts[0]
    departure_date = parts[1] if len(parts) > 1 else ""
    return train_number, departure_date


def split_route(route_text: str) -> tuple[str, str]:
    """'Дніпро-Головний → Ужгород' -> ('Дніпро-Головний', 'Ужгород')."""
    if "→" in route_text:
        origin, destination = route_text.split("→", 1)
        return clean_text(origin), clean_text(destination)
    return route_text, ""


def find_site_updated_at(soup: BeautifulSoup) -> str | None:
    """Finds the text after 'Останнє оновлення:' e.g. '15 вересня 08:04'."""
    page_text = soup.get_text(" ")
    match = re.search(r"Останнє оновлення:\s*(\d{1,2}\s+\S+\s+\d{1,2}:\d{2})", page_text)
    if match is None:
        return None
    return clean_text(match.group(1))


def find_delay_table(soup: BeautifulSoup):
    """Returns the <table> whose header mentions 'Затримка', or None."""
    for table in soup.find_all("table"):
        header_text = table.get_text(" ")
        if "Затримка" in header_text and "Сполучення" in header_text:
            return table
    return None


def empty_station_detail(column_prefix: str) -> dict:
    return {
        f"{column_prefix}_delay_minutes": "",
        f"{column_prefix}_forecast": "",
        f"{column_prefix}_scheduled": "",
        f"{column_prefix}_passed": "",
    }


def parse_main_row(cell_texts: list[str]) -> dict:
    train_number, departure_date = split_train_number_and_date(cell_texts[0])
    origin_station, destination_station = split_route(cell_texts[1])
    delay_minutes = parse_delay_to_minutes(cell_texts[2])

    row = {
        "train_number": train_number,
        "departure_date": departure_date,
        "origin_station": origin_station,
        "destination_station": destination_station,
        "delay_minutes": delay_minutes if delay_minutes is not None else "",
        "forecast_arrival": cell_texts[3],
        "scheduled_arrival": cell_texts[4],
        "status": cell_texts[5],
        "forecast_reliability": cell_texts[6],
        "reason": cell_texts[7],
    }
    for column_prefix in STATION_DETAIL_COLUMNS.values():
        row.update(empty_station_detail(column_prefix))
    return row


def add_station_detail(row: dict, cell_texts: list[str], row_classes: list[str]) -> None:
    """
    A detail row is one stop of the train above it:
    [rail graphic, STATION, delay at that stop, forecast time, scheduled time, '', '', ''].
    Only the stations we care about are kept.
    """
    station = cell_texts[1]
    column_prefix = STATION_DETAIL_COLUMNS.get(station)
    if column_prefix is None:
        return
    delay_minutes = parse_delay_to_minutes(cell_texts[2])
    row[f"{column_prefix}_delay_minutes"] = delay_minutes if delay_minutes is not None else ""
    row[f"{column_prefix}_forecast"] = cell_texts[3]
    row[f"{column_prefix}_scheduled"] = cell_texts[4]
    row[f"{column_prefix}_passed"] = "yes" if "delay-row__detail--passed" in row_classes else "no"


def parse_all_delay_rows(table) -> list[dict]:
    """
    Every train of the delay table, as plain dicts, before any filtering.
    The table has one main row per train (class "delay-row") followed by hidden
    detail rows, one per stop (class "delay-row__detail"), which we fold into the train.
    """
    rows = []
    for table_row in table.find_all("tr"):
        cells = table_row.find_all("td")
        if len(cells) < 8:
            continue  # header row or something malformed
        cell_texts = [clean_text(cell.get_text(" ")) for cell in cells]
        row_classes = table_row.get("class", [])

        if "delay-row__detail" in row_classes:
            if rows:
                add_station_detail(rows[-1], cell_texts, row_classes)
            continue

        rows.append(parse_main_row(cell_texts))
    return rows


# --------------------------------------------------------------------------- watched trains

def load_watched_trains() -> list[dict]:
    with WATCHED_TRAINS_PATH.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    return config["trains"]


def find_page_rows_for_watched_train(watched_train: dict, page_rows: list[dict]) -> list[dict]:
    """
    The delay page lists the same number once per direction (e.g. 41/42 Дніпро→Трускавець
    and 41/42 Трускавець→Дніпро), so match on number AND destination.
    In the afternoon two service days of the same westbound train can be on the page at once
    (yesterday's still running late, today's just departed), so this returns all matches.
    """
    matching_rows = []
    for page_row in page_rows:
        same_number = page_row["train_number"] == watched_train["train_number"]
        westbound = page_row["destination_station"] in watched_train["westbound_destinations"]
        if same_number and westbound:
            matching_rows.append(page_row)
    return matching_rows


def build_observation_rows(
    watched_trains: list[dict],
    page_rows: list[dict],
    observed_at_utc: str,
    observed_at_kyiv: str,
    site_updated_at: str,
) -> list[dict]:
    """
    At least one output row per watched train: one per matching page row when it is listed
    (usually exactly one), or a single "not on page" row with delay 0 when it is not.
    """
    observation_rows = []
    for watched_train in watched_trains:
        common_fields = {
            "observed_at_utc": observed_at_utc,
            "observed_at_kyiv": observed_at_kyiv,
            "site_updated_at": site_updated_at,
            "train_number": watched_train["train_number"],
            "scheduled_departure_bila_tserkva": watched_train["scheduled_departure_bila_tserkva"],
            "scheduled_arrival_lviv": watched_train["scheduled_arrival_lviv"],
        }

        matching_page_rows = find_page_rows_for_watched_train(watched_train, page_rows)

        if len(matching_page_rows) == 0:
            observation = dict(common_fields)
            observation["on_delay_page"] = "no"
            observation["delay_minutes"] = 0
            for column in CSV_COLUMNS:
                if column not in observation:
                    observation[column] = ""
            observation_rows.append(observation)
            continue

        for page_row in matching_page_rows:
            observation = dict(common_fields)
            observation["on_delay_page"] = "yes"
            observation.update(page_row)
            observation_rows.append(observation)
    return observation_rows


# --------------------------------------------------------------------------- output

def check_existing_csv_header() -> None:
    """
    If data/delays.csv was written with an older column set that is a prefix of the
    current one, rewrite it once with the new columns added (empty for old rows).
    Any other mismatch fails loudly.
    """
    if not OUTPUT_CSV_PATH.exists():
        return
    with OUTPUT_CSV_PATH.open(encoding="utf-8", newline="") as csv_file:
        reader = csv.reader(csv_file)
        existing_header = next(reader, [])
        if existing_header == CSV_COLUMNS:
            return
        if CSV_COLUMNS[:len(existing_header)] != existing_header:
            raise SystemExit(
                f"ERROR: {OUTPUT_CSV_PATH} has columns {existing_header}, "
                f"but the script now writes {CSV_COLUMNS}. Delete or rename the old file."
            )
        existing_rows = list(reader)

    added_column_count = len(CSV_COLUMNS) - len(existing_header)
    with OUTPUT_CSV_PATH.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_COLUMNS)
        for existing_row in existing_rows:
            writer.writerow(existing_row + [""] * added_column_count)
    print(f"Migrated {OUTPUT_CSV_PATH}: added {added_column_count} columns to {len(existing_rows)} existing rows")


def append_rows_to_csv(rows: list[dict]) -> None:
    OUTPUT_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_already_exists = OUTPUT_CSV_PATH.exists()
    with OUTPUT_CSV_PATH.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        if not file_already_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


# --------------------------------------------------------------------------- main

def main() -> int:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    observed_at_utc = now_utc.isoformat()
    observed_at_kyiv = now_utc.astimezone(KYIV_TIMEZONE).strftime("%Y-%m-%d %H:%M")

    check_existing_csv_header()
    watched_trains = load_watched_trains()

    html = fetch_page_html()
    print(f"Fetched {len(html)} characters, saved to {DEBUG_PAGE_PATH}")

    soup = BeautifulSoup(html, "lxml")

    site_updated_at = find_site_updated_at(soup)
    if site_updated_at is None:
        print("ERROR: could not find 'Останнє оновлення' on the page - layout changed?", file=sys.stderr)
        return 1

    table = find_delay_table(soup)
    if table is None:
        print("ERROR: could not find the delay table on the page - layout changed?", file=sys.stderr)
        return 1

    page_rows = parse_all_delay_rows(table)

    rows_with_unparsed_delay = [row for row in page_rows if row["delay_minutes"] == ""]
    if rows_with_unparsed_delay:
        print(f"WARNING: {len(rows_with_unparsed_delay)} page rows had an unparseable delay value", file=sys.stderr)

    observation_rows = build_observation_rows(
        watched_trains, page_rows, observed_at_utc, observed_at_kyiv, site_updated_at
    )
    append_rows_to_csv(observation_rows)

    delayed_watched = [row for row in observation_rows if row["on_delay_page"] == "yes"]
    print(
        f"{observed_at_kyiv} Kyiv: page updated {site_updated_at}, "
        f"{len(page_rows)} trains delayed overall, "
        f"{len(delayed_watched)} of {len(watched_trains)} watched trains delayed"
    )
    for row in delayed_watched:
        print(
            f"  {row['train_number']} {row['departure_date']} → {row['destination_station']}: "
            f"+{row['delay_minutes']} min overall, "
            f"Біла Церква {row['bila_tserkva_delay_minutes'] or '—'} (passed: {row['bila_tserkva_passed'] or '?'}), "
            f"Львів {row['lviv_delay_minutes'] or '—'} (passed: {row['lviv_passed'] or '?'})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
