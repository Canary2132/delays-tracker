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
]

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal train-delay tracker; polite, twice an hour at night)",
    "Accept-Language": "uk-UA,uk;q=0.9",
}


# --------------------------------------------------------------------------- fetch & parse

def fetch_page_html() -> str:
    response = requests.get(DELAY_PAGE_URL, headers=REQUEST_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_delay_to_minutes(delay_text: str) -> int | None:
    """'+6:07' -> 367, '+0:38' -> 38. Returns None if it does not match."""
    match = re.match(r"^\+?(\d+):(\d{2})$", delay_text)
    if match is None:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    return hours * 60 + minutes


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


def parse_all_delay_rows(table) -> list[dict]:
    """Every row of the delay table, as plain dicts, before any filtering."""
    rows = []
    for table_row in table.find_all("tr"):
        cells = table_row.find_all("td")
        if len(cells) < 8:
            continue  # header row or something malformed

        cell_texts = [clean_text(cell.get_text(" ")) for cell in cells]

        train_number, departure_date = split_train_number_and_date(cell_texts[0])
        origin_station, destination_station = split_route(cell_texts[1])
        delay_minutes = parse_delay_to_minutes(cell_texts[2])

        rows.append(
            {
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
        )
    return rows


# --------------------------------------------------------------------------- watched trains

def load_watched_trains() -> list[dict]:
    with WATCHED_TRAINS_PATH.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    return config["trains"]


def find_page_row_for_watched_train(watched_train: dict, page_rows: list[dict]) -> dict | None:
    """
    The delay page lists the same number once per direction (e.g. 41/42 Дніпро→Трускавець
    and 41/42 Трускавець→Дніпро), so match on number AND destination.
    """
    for page_row in page_rows:
        same_number = page_row["train_number"] == watched_train["train_number"]
        westbound = page_row["destination_station"] in watched_train["westbound_destinations"]
        if same_number and westbound:
            return page_row
    return None


def build_observation_rows(
    watched_trains: list[dict],
    page_rows: list[dict],
    observed_at_utc: str,
    observed_at_kyiv: str,
    site_updated_at: str,
) -> list[dict]:
    """One output row per watched train, whether or not it appeared on the page."""
    observation_rows = []
    for watched_train in watched_trains:
        page_row = find_page_row_for_watched_train(watched_train, page_rows)

        observation = {
            "observed_at_utc": observed_at_utc,
            "observed_at_kyiv": observed_at_kyiv,
            "site_updated_at": site_updated_at,
            "train_number": watched_train["train_number"],
            "scheduled_departure_bila_tserkva": watched_train["scheduled_departure_bila_tserkva"],
            "scheduled_arrival_lviv": watched_train["scheduled_arrival_lviv"],
        }

        if page_row is None:
            observation["on_delay_page"] = "no"
            observation["delay_minutes"] = 0
            for column in ["departure_date", "origin_station", "destination_station",
                           "forecast_arrival", "scheduled_arrival", "status",
                           "forecast_reliability", "reason"]:
                observation[column] = ""
        else:
            observation["on_delay_page"] = "yes"
            observation.update(page_row)

        observation_rows.append(observation)
    return observation_rows


# --------------------------------------------------------------------------- output

def check_existing_csv_header() -> None:
    """Fail loudly if data/delays.csv was written with a different column set."""
    if not OUTPUT_CSV_PATH.exists():
        return
    with OUTPUT_CSV_PATH.open(encoding="utf-8", newline="") as csv_file:
        existing_header = next(csv.reader(csv_file), [])
    if existing_header != CSV_COLUMNS:
        raise SystemExit(
            f"ERROR: {OUTPUT_CSV_PATH} has columns {existing_header}, "
            f"but the script now writes {CSV_COLUMNS}. Delete or rename the old file."
        )


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
    DEBUG_PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_PAGE_PATH.write_text(html, encoding="utf-8")
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
        print(f"  {row['train_number']} → {row['destination_station']}: +{row['delay_minutes']} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
