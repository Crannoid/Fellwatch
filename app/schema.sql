-- Met Office Mountain Forecast archive schema
-- One row per daily capture; hourly detail only exists for "day 1" of each capture,
-- day 2 is a coarser text/range summary as published by the Met Office.

CREATE TABLE IF NOT EXISTS captures (
    capture_id      INTEGER PRIMARY KEY,
    captured_at     TEXT NOT NULL,        -- UTC timestamp when we fetched it
    issued_at       TEXT,                 -- Met Office's own "Issued on ... at ..." timestamp
    area            TEXT NOT NULL,        -- e.g. 'lake-district'
    source_url      TEXT NOT NULL,
    parser_version  TEXT NOT NULL,
    raw_pdf_path    TEXT NOT NULL,        -- archived original PDF, always written
    raw_text_path   TEXT NOT NULL,        -- archived extracted text, always written
    parse_ok        INTEGER NOT NULL DEFAULT 1,  -- 0 if the capture had any field parse failures
    parse_errors    TEXT                  -- JSON list of field names that failed to parse, if any
);

CREATE TABLE IF NOT EXISTS daily_summary (
    id                  INTEGER PRIMARY KEY,
    capture_id          INTEGER NOT NULL REFERENCES captures(capture_id),
    forecast_date       TEXT NOT NULL,
    forecast_day        INTEGER NOT NULL,     -- 1 = rich hourly detail, 2 = coarse text-only
    confidence          TEXT,
    headline            TEXT,
    weather_text        TEXT,
    max_wind_text       TEXT,                 -- day 2 only
    cloud_free_pct      INTEGER,
    low_cloud_vis       TEXT,
    freezing_level_text TEXT,                 -- day 2 only, e.g. 'Above the summits'
    meteorologist_view  TEXT,
    ground_conditions   TEXT
);

CREATE TABLE IF NOT EXISTS hazards (
    id              INTEGER PRIMARY KEY,
    capture_id      INTEGER NOT NULL REFERENCES captures(capture_id),
    forecast_date   TEXT NOT NULL,
    hazard_name     TEXT NOT NULL,
    likelihood      TEXT CHECK(likelihood IN ('No','Low','Medium','High'))
);

CREATE TABLE IF NOT EXISTS hourly_readings (   -- day 1 only
    id              INTEGER PRIMARY KEY,
    capture_id      INTEGER NOT NULL REFERENCES captures(capture_id),
    forecast_time   TEXT NOT NULL,         -- e.g. '2026-08-24T09:00'
    altitude        TEXT,                  -- '900m' | '600m' | '300m' | 'valley' | '800m' | NULL
    metric          TEXT NOT NULL,         -- 'temperature_c' | 'feels_like_c' | 'wind_speed_mph' |
                                            -- 'wind_gust_mph' | 'wind_direction' | 'precip_chance_pct' |
                                            -- 'freezing_level_m' | 'weather_desc'
    value_numeric   REAL,
    value_text      TEXT
);

CREATE INDEX IF NOT EXISTS idx_hourly_metric_alt ON hourly_readings(metric, altitude, forecast_time);
CREATE INDEX IF NOT EXISTS idx_captures_captured_at ON captures(captured_at);
