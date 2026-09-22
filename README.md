# UZ train delay tracker

Polls https://uz-vezemo.uz.gov.ua/delayform twice an hour during the night window
via GitHub Actions and appends one row per watched train to `data/delays.csv`.
Watched trains live in `trains.json`.

## Columns
`observed_at_utc`, `observed_at_kyiv` — when the scraper looked
`site_updated_at` — the "Останнє оновлення" timestamp shown on the page
`train_number` — as on the delay page, e.g. `85/86`
`on_delay_page` — `yes` if the train was listed, `no` if not (delay recorded as 0)
`delay_minutes` — the "+H:MM" delay converted to minutes
`scheduled_departure_bila_tserkva`, `scheduled_arrival_lviv` — from `trains.json`, for convenience in analysis
`departure_date`, `origin_station`, `destination_station`, `forecast_arrival`, `scheduled_arrival`, `status`, `forecast_reliability`, `reason` — copied from the page (empty when `on_delay_page` is `no`)

`on_delay_page = no` means "on time" only on days the train actually runs; several of the watched trains run on a special schedule.
