# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Fellwatch is a single long-running Python container that downloads the Met Office Mountain Forecast PDF once a day, archives it, parses it into SQLite, and exposes the DB to Grafana (read-only). Deployed via docker compose on a home server ("containerHost"); see `README.md` for deployment and env-var tables.

## Commands

There is no test suite, linter, or build step beyond Docker. Code runs from `app/` with deps `requests` + `pdfplumber` (`pip install -r requirements.txt`).

```bash
docker compose up -d --build                  # run the logger + Grafana (Grafana on :3010)
docker logs fellwatch                         # watch captures / parse errors
docker exec fellwatch python reparse.py       # re-parse only captures with parse_ok=0 or no hourly rows
docker exec fellwatch python reparse.py --all # re-parse every archived capture
```

Running locally: `DATA_DIR=./data python app/main.py` (defaults to `/data`; it also does an immediate capture on startup, which hits the live Met Office URL).

Config comes from env vars (`MOUNTAIN_AREA`, `CAPTURE_HOUR/MINUTE`, `TZ`, `LOG_LEVEL`, `NOTIFY_URL`, `DATA_DIR`, `DB_FILENAME`) — the host data dir is `FELLWATCH_DATA` in `.env` (gitignored; copy from `.env.example`). `NOTIFY_URL` is a private ntfy-style URL: never commit it, and don't hardcode credentials (Grafana creds were deliberately removed from compose).

## Architecture

- `app/main.py` — everything: fetch, text extraction, per-section parsers, DB writes, scheduler loop. `main()` captures once at startup, then sleeps to the next `CAPTURE_HOUR:CAPTURE_MINUTE` (local, needs `TZ` + tzdata) in a loop. Capture failures are logged, rolled back, and sent via `notify()`; they never kill the loop.
- `app/reparse.py` — imports `main` and re-runs `parse_and_store` against archived PDFs in `raw/`. This is the intended workflow after fixing a parser: fix, then reparse to backfill.
- `app/schema.sql` — applied by `init_db`. `captures` (one row per fetch) is parent to `daily_summary`, `hazards`, and `hourly_readings` (a narrow metric/altitude/value table, day 1 only).
- `grafana-provisioning/datasources/` — provisions the SQLite datasource (frser-sqlite-datasource plugin) against the shared `/data` mount.

### Key design rules

- **Raw first, parse second.** `capture_and_store` writes the PDF and extracted text to `$DATA_DIR/raw/` and inserts the `captures` row *before* any parsing. Preserve this ordering so a broken parser never loses data.
- **Per-field resilience.** `parse_and_store` wraps each parser in `safe(...)` and tracks failures; `required(...)` also flags parsers that silently return empty (the failure mode when the Met Office changed the PDF layout). Failures set `captures.parse_ok=0` and list field names in `parse_errors` (JSON). New parsers should go through `safe`/`required`.
- **Idempotent re-parse.** `parse_and_store` deletes existing child rows for the capture before inserting, so it is safe to re-run. `PARSER_VERSION` is stamped on each capture — bump it when parser behaviour changes.
- **The PDF text is fragile.** Layout comes from pdfplumber text extraction with no delimiters; weather descriptions are recovered by regex heuristics and by word position (`parse_weather_descs_by_position`). Recent commits handle 8/9-column forecasts and wrapped wind rows — expect further layout drift and check `parse_errors`.
- Day 1 has hourly detail per altitude (900m/600m/300m/valley); day 2 is only coarse text fields in `daily_summary` (`max_wind_text`, `freezing_level_text`).

### Fixtures

`app/test_fixture*.txt` are extracted-text samples from real PDFs kept as regression references for parser changes (dated ones capture layout variants). They are not wired to an automated test runner; verify parser changes by running the parsers against them manually.

## Working style: one step at a time

When walking the user through anything multi-step (deploying, debugging on containerHost, setting up Grafana, running a backfill), give **one step at a time**. Don't dump a list of five steps up front. State the single next step, wait for the user to report the result, then give the next one based on what actually happened.
