# UZ train delay tracker (Біла Церква → Львів)

Polls https://uz-vezemo.uz.gov.ua/delayform twice an hour during the night window
via GitHub Actions and appends one row per watched train to `data/delays.csv`.
Watched trains live in `trains.json`.

## Setup
1. Create a new **public** repository on GitHub (not a fork).
2. Upload these files, keeping the folder structure (`.github/workflows/scrape.yml` must be at that exact path).
3. Repo → Settings → Actions → General → "Workflow permissions" → select **Read and write permissions** → Save.
4. Actions tab → "Scrape UZ train delays" → **Run workflow** to test it once. A green run means it works.
5. It then runs on its own every 30 min between 19:00 and 08:30 UTC. Check the Actions tab occasionally for red runs.

If you already have a `data/delays.csv` from the older all-trains version, delete it first — the columns changed and the script refuses to append to a mismatched file.

## Columns
`observed_at_utc`, `observed_at_kyiv` — when the scraper looked
`site_updated_at` — the "Останнє оновлення" timestamp shown on the page
`train_number` — as on the delay page, e.g. `85/86`
`on_delay_page` — `yes` if the train was listed, `no` if not (delay recorded as 0)
`delay_minutes` — the "+H:MM" delay converted to minutes
`scheduled_departure_bila_tserkva`, `scheduled_arrival_lviv` — from `trains.json`, for convenience in analysis
`departure_date`, `origin_station`, `destination_station`, `forecast_arrival`, `scheduled_arrival`, `status`, `forecast_reliability`, `reason` — copied from the page (empty when `on_delay_page` is `no`)

`on_delay_page = no` means "on time" only on days the train actually runs; several of the watched trains run on a special schedule.
