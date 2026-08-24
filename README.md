# Lake District Mountain Weather Logger

Daily archive of the Met Office Mountain Forecast for a national park area
(default: Lake District), stored in SQLite with the raw PDF/text kept
alongside every capture.

## What this does

Once a day (07:00 local time by default) the container:
1. Downloads `https://data.consumer-digital.api.metoffice.gov.uk/v1/mountain/lake-district.pdf`
2. Archives the raw PDF and its extracted text under `/data/raw/` — unconditionally, even if parsing fails
3. Parses it into `/data/mountain_weather.sqlite3` (schema in `app/schema.sql`)
4. Sleeps until the next day and repeats — one bad day never kills the container; it logs and retries on the next cycle

## Before you rely on this unattended — please validate

I built and tested the parser against one real PDF, fetched through my own
sandboxed tooling (which can't reach `metoffice.gov.uk` directly, so I
couldn't test a live re-fetch end to end). What's validated:

- Every field parses correctly against that one real sample (`app/test_fixture.txt` — kept in the repo as a regression fixture)
- The full pipeline runs end-to-end and writes correct rows to SQLite, including the midnight-rollover date logic for the last `00:00` timepoint

What's **not** validated, and worth checking once it's running on containerHost:
- **Weather description parsing is the fragile part.** `parse_day1_weather_precip` splits multi-word descriptions ("Sunny intervals", "Clear night") using a regex heuristic (lowercase-then-uppercase boundary), because there's no delimiter in the extracted text. It worked on the one sample I had, but an unusual description (or pdfplumber's extraction differing slightly from what I tested against) could break it. If it does, `weather_desc` rows will just be missing for that day — everything else still gets stored, and the raw PDF is archived so you can backfill.
- Only tested for the Lake District. Other park areas (`MOUNTAIN_AREA` env var) should use the same PDF structure but I haven't pulled one to confirm.
- Only tested one day's data — haven't confirmed the format holds on a day with active hazards, snow, or a "Further outlook" (3–5 day) section, which didn't appear in my sample.

**Suggested validation once deployed:** let it run for a week, then check `SELECT parse_ok, parse_errors FROM captures;` — anything with `parse_ok=0` tells you exactly which field broke, and you've still got the raw text/PDF for that day to fix the regex against and re-parse retroactively.

## Deploying on containerHost

```bash
git clone <this repo> mountain-weather-logger
cd mountain-weather-logger
docker compose up -d --build
```

Data persists in `./data` on the host (bind mount) — back this up like anything else on containerHost. `./data/mountain_weather.sqlite3` is the whole dataset; `./data/raw/` will grow by roughly two small files a day (a PDF and a text extract), worth an occasional check on disk usage over a few years but not a real concern at this scale.

## Config (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `MOUNTAIN_AREA` | `lake-district` | Met Office area slug used in the PDF URL |
| `CAPTURE_HOUR` / `CAPTURE_MINUTE` | `7` / `0` | Local time (per `TZ`) for the daily capture |
| `TZ` | — | Set in compose to `Europe/London`; needed for `CAPTURE_HOUR` to mean what you think it means |
| `LOG_LEVEL` | `INFO` | `docker logs mountain-weather-logger` to watch it run |

## Suggested next step: a heartbeat in Uptime Kuma

Given your existing monitoring setup, a silent failure here (e.g. the Met
Office changing the PDF format, or the URL moving) could go unnoticed for
months since nothing crashes loudly — it just quietly stops adding new rows.
Worth adding a push-monitor heartbeat: after a successful `capture_and_store`,
`curl` a Kuma push URL. I've left this out for now since it depends on your
Kuma instance details, but it's a small addition to `main.py` if you want it.
