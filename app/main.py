"""
Met Office Mountain Forecast logger.

Fetches the printable PDF for a given mountain area daily, archives the raw
PDF + extracted text unconditionally, then attempts to parse it into
structured rows. Parsing is resilient per-field: a failure in one section
never loses the rest of that day's capture, and the raw files are always
kept so a broken parser can be re-run against history later.

Run as a long-lived container: sleeps until the configured daily capture
time, captures, then sleeps until the next day. Exceptions during a capture
are logged and swallowed so one bad day doesn't kill the schedule.
"""

import datetime
import io
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import pdfplumber
import requests

PARSER_VERSION = "1.2.0"

AREA = os.environ.get("MOUNTAIN_AREA", "lake-district")
PDF_URL = f"https://data.consumer-digital.api.metoffice.gov.uk/v1/mountain/{AREA}.pdf"

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / os.environ.get("DB_FILENAME", "mountain_weather.sqlite3")

CAPTURE_HOUR = int(os.environ.get("CAPTURE_HOUR", "7"))
CAPTURE_MINUTE = int(os.environ.get("CAPTURE_MINUTE", "0"))

NOTIFY_URL = os.environ.get("NOTIFY_URL", "")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("mountain-logger")

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
ALTITUDES = ["900", "600", "300", "Valley"]


def notify(title: str, message: str) -> None:
    """Best-effort push notification: POSTs the message as plain text to NOTIFY_URL
    (ntfy-style: title in a header, body as text). Never raises."""
    if not NOTIFY_URL:
        return
    try:
        # HTTP headers must be latin-1 safe, so keep the title ASCII.
        title = title.encode("ascii", "replace").decode()
        requests.post(
            NOTIFY_URL, data=message.encode("utf-8"), timeout=10,
            headers={"Title": title, "Priority": "high", "Tags": "warning"},
        ).raise_for_status()
    except Exception:
        log.exception("Failed to send notification")


def init_db(conn: sqlite3.Connection) -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()


def fetch_pdf() -> bytes:
    log.info("Fetching %s", PDF_URL)
    try:
        resp = requests.get(PDF_URL, timeout=30, headers={"User-Agent": "personal-weather-archive/1.0"})
        resp.raise_for_status()
    except requests.HTTPError as e:
        r = e.response
        log.error("Fetch failed: HTTP %s %s from %s (body: %.200r)",
                  r.status_code, r.reason, PDF_URL, r.text)
        raise
    except requests.Timeout:
        log.error("Fetch failed: timed out after 30s fetching %s", PDF_URL)
        raise
    except requests.RequestException as e:
        log.error("Fetch failed: %s fetching %s: %s", type(e).__name__, PDF_URL, e)
        raise
    content_type = resp.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() or not resp.content.startswith(b"%PDF"):
        log.warning("Fetched response may not be a PDF (Content-Type=%r, %d bytes)",
                    content_type, len(resp.content))
    log.info("Fetched %d bytes (HTTP %s)", len(resp.content), resp.status_code)
    return resp.content


def extract_text(pdf_bytes: bytes, tmp_path: Path) -> str:
    tmp_path.write_bytes(pdf_bytes)
    with pdfplumber.open(tmp_path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


# ---- individual field parsers -------------------------------------------
# Each returns a plain value (or raises) so the caller can catch failures
# per field without losing the rest of the capture.

def parse_issued(text: str):
    m = re.search(r"Forecast issued on ([A-Za-z]+ \d{1,2} [A-Za-z]+ \d{4}) at (\d{2}:\d{2})", text, re.I)
    return f"{m.group(1)} {m.group(2)}" if m else None


def parse_confidence(text: str):
    m = re.search(r"Confidence:\s*(.+)", text)
    return m.group(1).strip() if m else None


def parse_headline(text: str):
    # Older PDFs: "Headline for <date>: <text>" on one line; newer: no colon,
    # headline on the following line.
    m = re.search(r"Headline for [A-Za-z]+ \d{1,2} [A-Za-z]+ \d{4}:?[ \t]*\n?(.+)", text)
    return m.group(1).strip() if m else None


def parse_hazards(text: str):
    """Hazards are printed as three name-triples followed by three
    likelihood-triples, e.g.:
        Blizzards Heavy persistent snow Storm force winds
        No Likelihood No Likelihood No Likelihood
    """
    block = re.search(
        r"Mountain weather hazards on [^\n]+\n(.+?)Weather and chance of precipitation",
        text, re.S,
    )
    if not block:
        return []
    lines = [l.strip() for l in block.group(1).strip().splitlines() if l.strip()]
    names, likelihoods = [], []
    KNOWN_HAZARDS = [
        "Blizzards", "Heavy Persistent Snow", "Storm Force Winds", "Gales",
        "Severe Chill Effect", "Poor Visibility", "Thunderstorms",
        "Heavy Persistent Rain", "Strong Sunlight",
    ]
    remaining = " ".join(lines)
    for name in KNOWN_HAZARDS:
        if re.search(name, remaining, re.I):
            names.append(name)
    for lvl in re.findall(r"(No|Low|Medium|High) Likelihood", remaining):
        likelihoods.append(lvl)
    return list(zip(names, likelihoods)) if len(names) == len(likelihoods) else []


def parse_weather_descs_by_position(pdf_bytes: bytes):
    """Returns dict: time -> weather description, reading the 800m weather row
    by x-position. Descriptions can wrap onto two lines ("Partly cloudy" /
    "(night)") and the flattened text interleaves neighbouring cells, so each
    word is assigned to the nearest time column instead."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            hdr = next((i for i, w in enumerate(words) if w["text"] == "Time"), None)
            if hdr is None:
                continue
            cols = []
            for w in words[hdr + 1:]:
                if not re.fullmatch(r"\d{2}:\d{2}", w["text"]):
                    break
                cols.append(w)
            end = next((w for w in words[hdr:] if w["text"] == "Precipitation"), None)
            if len(cols) < 2 or end is None:
                continue
            centres = [(c["x0"] + c["x1"]) / 2 for c in cols]
            cells = {c["text"]: [] for c in cols}
            for w in words:
                if cols[0]["bottom"] <= w["top"] < end["top"]:
                    mid = (w["x0"] + w["x1"]) / 2
                    nearest = min(range(len(cols)), key=lambda i: abs(centres[i] - mid))
                    cells[cols[nearest]["text"]].append(w)
            return {t: " ".join(w["text"] for w in sorted(ws, key=lambda w: (round(w["top"]), w["x0"])))
                    for t, ws in cells.items() if ws}
    return {}


def parse_day1_weather_precip(text: str, pdf_bytes: bytes = None):
    """Returns dict: time -> {weather_desc, precip_pct}."""
    block = re.search(
        r"Weather and chance of precipitation at 800m.*?"
        r"\n(\d{2}:\d{2}(?:[ \t]+\d{2}:\d{2})+)[ \t]*\n(.+?)\nPrecipitation probability\n(.+?)\n",
        text, re.S,
    )
    if not block:
        return {}
    times = block.group(1).split()
    weathers_line = block.group(2).strip()
    # weather descriptions are multi-word and space-separated with no fixed
    # delimiter; this greedy split works for the standard vocabulary but
    # verify against live output — see README.
    positional = parse_weather_descs_by_position(pdf_bytes) if pdf_bytes else {}
    # The text-only split can't cope with descriptions wrapped over several
    # lines, so it's only a fallback for the single-line layout.
    weather_words = [] if "\n" in weathers_line else re.split(r"(?<=[a-z])\s(?=[A-Z])", weathers_line)
    precips = re.findall(r"\d{1,3}%", block.group(3))
    out = {}
    for i, t in enumerate(times):
        out[t] = {
            "weather_desc": positional.get(t) or (weather_words[i] if i < len(weather_words) else None),
            "precip_pct": int(precips[i].rstrip("%")) if i < len(precips) else None,
        }
    return out


# "Forecast for Monday 24 Aug 2026", optionally followed by an ISO date (older PDFs).
FORECAST_FOR_RE = re.compile(r"Forecast for [A-Za-z]+ (\d{1,2}) ([A-Za-z]{3})[A-Za-z]* (\d{4})(?: (\d{4}-\d{2}-\d{2}))?")
MONTH_NUMS = {m[:3].lower(): i for i, m in enumerate(MONTHS.split("|"), 1)}


def _forecast_markers(text: str):
    """[(iso_date, match), ...] for each 'Forecast for ...' heading, in page order."""
    out = []
    for m in FORECAST_FOR_RE.finditer(text):
        iso = m.group(4) or f"{int(m.group(3)):04d}-{MONTH_NUMS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
        out.append((iso, m))
    return out


def parse_forecast_dates(text: str):
    """Returns [day1_iso, day2_iso]. The day-1 heading is repeated above the
    wind/temperature tables, so duplicates are dropped."""
    dates = []
    for iso, _ in _forecast_markers(text):
        if iso not in dates:
            dates.append(iso)
    return dates


def _cloud_free_pct(text: str):
    """First percentage in the 'Chance of cloud-free hill tops above 800m' entry.
    The label can wrap ("...hill\ntops above 800m:") and the value may be a
    phrase like 'Around 75% until midday in east, otherwise close to 100%'."""
    m = re.search(r"Chance of cloud-free hill(.*?)Low cloud and visibility", text, re.S)
    pct = re.search(r"(\d+)%", m.group(1)) if m else None
    return int(pct.group(1)) if pct else None


def parse_day1_detail(text: str):
    m_weather = re.search(r"Weather:\s*(.+?)\nChance of cloud-free hill\s+tops", text, re.S)
    m_vis = re.search(r"Low cloud and visibility:\s*(.+?)(?=\nCheck our weather|\nMountain Weather Forecast|\nTemperature:)", text, re.S)
    m_met = re.search(r"Meteorologist.s View\s*\n(.+?)\nForecast for", text, re.S)
    return {
        "weather_text": " ".join(m_weather.group(1).split()) if m_weather else None,
        "cloud_free_pct": _cloud_free_pct(text),
        "low_cloud_vis": " ".join(m_vis.group(1).split()) if m_vis else None,
        "meteorologist_view": " ".join(m_met.group(1).split()) if m_met else None,
    }


TIMES_LINE = r"(\d{2}:\d{2}(?:[ \t]+\d{2}:\d{2})+)[ \t]*\n"


def _day_offsets(times):
    """Day offset (0 = day 1, 1 = day 2) for each time column. The table may
    start at 00:00 and end on the next day's 00:00, so a time that goes
    backwards marks the rollover."""
    offsets, day, prev = [], 0, None
    for t in times:
        if prev is not None and t < prev:
            day += 1
        offsets.append(day)
        prev = t
    return offsets


def parse_wind_table(text: str):
    """Returns list of (altitude, time, mean_mph, gust_mph, direction, day_offset)."""
    m = re.search(
        r"Wind Speed mean/gusts \(mph\) Wind Direction[^\n]*\n" + TIMES_LINE +
        r"(.*?)(?=Height \(m\)|Meteorologist)",
        text, re.S,
    )
    if not m:
        return []
    times = m.group(1).split()
    days = _day_offsets(times)
    lines = [l.split() for l in m.group(2).strip().splitlines() if l.strip()]
    is_speed = lambda tok: re.fullmatch(r"\d+/\d+", tok)
    rows = []
    for i, parts in enumerate(lines):
        if parts[0] not in ALTITUDES:
            continue
        # each timepoint is "mean/gust" then "DIR" e.g. "16/26 SE"
        rest = parts[1:]
        readings = [(rest[j], rest[j + 1]) for j in range(0, len(rest) - 1, 2)]
        missing = len(times) - len(readings)
        if missing:
            # The last columns can wrap: their speeds on the line above the
            # row and their directions on the line below it.
            pre = lines[i - 1] if i > 0 and all(is_speed(t) for t in lines[i - 1]) else []
            nxt = lines[i + 1] if i + 1 < len(lines) else []
            post = nxt if nxt and not any(is_speed(t) for t in nxt) and nxt[0] not in ALTITUDES else []
            if len(pre) != missing or len(post) != missing:
                raise ValueError(f"wind row {parts[0]}: {len(readings)} of {len(times)} readings found")
            readings += list(zip(pre, post))
        for t, day, (speed_gust, direction) in zip(times, days, readings):
            mean_s, gust_s = speed_gust.split("/")
            rows.append((parts[0], t, int(mean_s), int(gust_s), direction, day))
    return rows


def parse_temp_table(text: str):
    """Returns (rows, freezing_levels) where rows = list of
    (altitude, time, temp_c, feels_like_c, day_offset) and
    freezing_levels = list of (time, metres, day_offset)."""
    m = re.search(
        r"Temperature / Feels Like[^\n]*\n" + TIMES_LINE +
        r"((?:(?:900|600|300|Valley).+\n?){4})"
        # label is "Freezing Level*" on one line, or split around the values
        r"Freezing(?:[ \t]+Level\*)?\s*((?:\d+m[ \t]*)+)",
        text,
    )
    if not m:
        return [], []
    times = m.group(1).split()
    days = _day_offsets(times)
    rows = []
    for line in m.group(2).strip().splitlines():
        parts = line.split()
        altitude = parts[0]
        pairs = parts[1:]
        if len(pairs) != len(times):
            raise ValueError(f"temperature row {altitude}: {len(pairs)} of {len(times)} readings found")
        for t, day, pair in zip(times, days, pairs):
            temp_s, feels_s = pair.split("/")
            rows.append((altitude, t, int(temp_s), int(feels_s), day))
    fz_values = [int(v) for v in re.findall(r"(\d+)m", m.group(3))]
    if len(fz_values) != len(times):
        raise ValueError(f"freezing level: {len(fz_values)} of {len(times)} values found")
    freezing = list(zip(times, fz_values, days))
    return rows, freezing


def parse_day2(text: str):
    # Isolate the block starting at the first "Forecast for <date>" heading whose
    # date differs from day 1 — "Weather:" and "Chance of cloud-free hill tops" also
    # appear in the day-1 detail section, so searching the whole document risks
    # matching there.
    markers = _forecast_markers(text)
    day2 = next((m for iso, m in markers if iso != markers[0][0]), None) if markers else None
    if day2 is None:
        return {}
    day2_text = text[day2.end():]

    def field(pattern):
        m = re.search(pattern, day2_text, re.S)
        return " ".join(m.group(1).split()) if m else None

    # In the newer layout the freezing-level label is split: "Freezing <value>\nlevel:"
    freezing = field(r"Freezing level:\s*(.+?)\n") or field(r"Freezing\s+(.+?)\s*\nlevel:")
    weather = field(r"Weather:\s*(.+?)\nMaximum wind:")
    if not (weather and freezing):
        return {}
    return {
        "weather_text": weather,
        "max_wind_text": field(r"Maximum wind:\s*(.+?)\nChance of cloud-free hill"),
        "cloud_free_pct": _cloud_free_pct(day2_text),
        "low_cloud_vis": field(r"Low cloud and visibility:\s*(.+?)\nTemperature:"),
        "temperature_text": field(r"Temperature:\s*(.+?)\nFreezing"),
        "freezing_level_text": freezing,
    }


def parse_ground_conditions(text: str):
    m = re.search(r"Ground Conditions Supplement\s*\n(.+?)\nIssued at:", text, re.S)
    value = m.group(1).strip() if m else None
    # The Met Office sometimes publishes the unfilled template placeholder.
    return None if value and re.fullmatch(r"<<.*>>", value) else value


# ---- orchestration --------------------------------------------------------

def capture_and_store(conn: sqlite3.Connection) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    pdf_bytes = fetch_pdf()
    raw_pdf_path = RAW_DIR / f"{AREA}-{stamp}.pdf"
    raw_pdf_path.write_bytes(pdf_bytes)

    text = extract_text(pdf_bytes, DATA_DIR / "_tmp.pdf")
    raw_text_path = RAW_DIR / f"{AREA}-{stamp}.txt"
    raw_text_path.write_text(text, encoding="utf-8")

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO captures (captured_at, area, source_url, parser_version, "
        "raw_pdf_path, raw_text_path, parse_ok, parse_errors) VALUES (?,?,?,?,?,?,1,NULL)",
        (now.isoformat(), AREA, PDF_URL, PARSER_VERSION, str(raw_pdf_path), str(raw_text_path)),
    )
    parse_and_store(conn, cur.lastrowid, text, pdf_bytes)


def parse_and_store(conn: sqlite3.Connection, capture_id: int, text: str, pdf_bytes: bytes = None,
                    quiet: bool = False) -> None:
    """Parses one capture's text/PDF into the child tables and records the
    outcome on its `captures` row. Also used by reparse.py to backfill (quiet=True
    suppresses the failure notification). Safe to re-run: existing child rows are replaced."""
    errors = []

    def safe(label, fn, *args):
        try:
            return fn(*args)
        except Exception:
            log.exception("Failed to parse field: %s", label)
            errors.append(label)
            return None

    def required(label, value):
        # A parser that quietly returns nothing is the failure mode that went
        # unnoticed when the Met Office changed the PDF layout — flag it.
        if not value and label not in errors:
            errors.append(label)
        return value

    cur = conn.cursor()
    for table in ("daily_summary", "hazards", "hourly_readings"):
        cur.execute(f"DELETE FROM {table} WHERE capture_id=?", (capture_id,))
    issued_at = required("issued_at", safe("issued_at", parse_issued, text))
    dates = required("forecast_dates", safe("forecast_dates", parse_forecast_dates, text)) or []  # [day1_date, day2_date]
    cur.execute("UPDATE captures SET issued_at=?, parser_version=? WHERE capture_id=?",
                (issued_at, PARSER_VERSION, capture_id))

    confidence = required("confidence", safe("confidence", parse_confidence, text))
    headline = required("headline", safe("headline", parse_headline, text))
    day1_detail = safe("day1_detail", parse_day1_detail, text) or {}
    ground = safe("ground_conditions", parse_ground_conditions, text)

    if dates:
        cur.execute(
            "INSERT INTO daily_summary (capture_id, forecast_date, forecast_day, confidence, "
            "headline, weather_text, cloud_free_pct, low_cloud_vis, meteorologist_view, ground_conditions) "
            "VALUES (?,?,1,?,?,?,?,?,?,?)",
            (capture_id, dates[0], confidence, headline, day1_detail.get("weather_text"),
             day1_detail.get("cloud_free_pct"), day1_detail.get("low_cloud_vis"),
             day1_detail.get("meteorologist_view"), ground),
        )

    if len(dates) > 1:
        day2 = required("day2", safe("day2", parse_day2, text)) or {}
        cur.execute(
            "INSERT INTO daily_summary (capture_id, forecast_date, forecast_day, weather_text, "
            "max_wind_text, cloud_free_pct, low_cloud_vis, freezing_level_text) "
            "VALUES (?,?,2,?,?,?,?,?)",
            (capture_id, dates[1], day2.get("weather_text"), day2.get("max_wind_text"),
             day2.get("cloud_free_pct"), day2.get("low_cloud_vis"), day2.get("freezing_level_text")),
        )

    hazards = required("hazards", safe("hazards", parse_hazards, text)) or []
    for name, likelihood in hazards:
        if dates:
            cur.execute(
                "INSERT INTO hazards (capture_id, forecast_date, hazard_name, likelihood) VALUES (?,?,?,?)",
                (capture_id, dates[0], name, likelihood),
            )

    weather_precip = required("weather_precip", safe("weather_precip", parse_day1_weather_precip, text, pdf_bytes)) or {}
    if dates:
        for t, vals in weather_precip.items():
            fdt = f"{dates[0]}T{t}"
            if vals.get("weather_desc"):
                cur.execute(
                    "INSERT INTO hourly_readings (capture_id, forecast_time, altitude, metric, value_text) "
                    "VALUES (?,?,?,?,?)",
                    (capture_id, fdt, "800m", "weather_desc", vals["weather_desc"]),
                )
            if vals.get("precip_pct") is not None:
                cur.execute(
                    "INSERT INTO hourly_readings (capture_id, forecast_time, altitude, metric, value_numeric) "
                    "VALUES (?,?,?,?,?)",
                    (capture_id, fdt, "800m", "precip_chance_pct", vals["precip_pct"]),
                )

    wind_rows = required("wind_table", safe("wind_table", parse_wind_table, text)) or []
    if dates:
        for altitude, t, mean_s, gust_s, direction, day in wind_rows:
            # a trailing 00:00 column belongs to the following calendar day
            fdate = dates[min(day, len(dates) - 1)]
            fdt = f"{fdate}T{t}"
            alt_label = "valley" if altitude.lower() == "valley" else f"{altitude}m"
            for metric, val in (("wind_speed_mph", mean_s), ("wind_gust_mph", gust_s)):
                cur.execute(
                    "INSERT INTO hourly_readings (capture_id, forecast_time, altitude, metric, value_numeric) "
                    "VALUES (?,?,?,?,?)",
                    (capture_id, fdt, alt_label, metric, val),
                )
            cur.execute(
                "INSERT INTO hourly_readings (capture_id, forecast_time, altitude, metric, value_text) "
                "VALUES (?,?,?,?,?)",
                (capture_id, fdt, alt_label, "wind_direction", direction),
            )

    temp_rows, freezing = safe("temp_table", parse_temp_table, text) or ([], [])
    required("temp_table", temp_rows and freezing)
    if dates:
        for altitude, t, temp_c, feels_c, day in temp_rows:
            fdate = dates[min(day, len(dates) - 1)]
            fdt = f"{fdate}T{t}"
            alt_label = "valley" if altitude.lower() == "valley" else f"{altitude}m"
            for metric, val in (("temperature_c", temp_c), ("feels_like_c", feels_c)):
                cur.execute(
                    "INSERT INTO hourly_readings (capture_id, forecast_time, altitude, metric, value_numeric) "
                    "VALUES (?,?,?,?,?)",
                    (capture_id, fdt, alt_label, metric, val),
                )
        for t, metres, day in freezing:
            fdate = dates[min(day, len(dates) - 1)]
            fdt = f"{fdate}T{t}"
            cur.execute(
                "INSERT INTO hourly_readings (capture_id, forecast_time, altitude, metric, value_numeric) "
                "VALUES (?,?,?,?,?)",
                (capture_id, fdt, None, "freezing_level_m", metres),
            )

    if errors:
        cur.execute("UPDATE captures SET parse_ok=0, parse_errors=? WHERE capture_id=?",
                    (json.dumps(errors), capture_id))
        log.warning("Capture %s stored with partial parse failures: %s", capture_id, errors)
        if not quiet:
            notify(f"Fellwatch: partial parse ({AREA})",
                   f"Capture {capture_id} stored, but these fields failed to parse: {', '.join(errors)}")
    else:
        cur.execute("UPDATE captures SET parse_ok=1, parse_errors=NULL WHERE capture_id=?", (capture_id,))
        log.info("Capture %s stored cleanly.", capture_id)

    conn.commit()


def next_run_time(hour: int, minute: int) -> datetime.datetime:
    now = datetime.datetime.now()
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now:
        run += datetime.timedelta(days=1)
    return run


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    log.info("Mountain logger started. Area=%s DB=%s daily capture at %02d:%02d",
              AREA, DB_PATH, CAPTURE_HOUR, CAPTURE_MINUTE)

    # Capture immediately on first start so you don't wait a full day to see it work.
    try:
        capture_and_store(conn)
    except Exception as e:
        conn.rollback()
        log.exception("Initial capture failed")
        notify(f"Fellwatch: capture failed ({AREA})", f"Initial capture failed: {type(e).__name__}: {e}")

    while True:
        target = next_run_time(CAPTURE_HOUR, CAPTURE_MINUTE)
        sleep_secs = (target - datetime.datetime.now()).total_seconds()
        log.info("Next capture at %s (sleeping %.1fh)", target, sleep_secs / 3600)
        time.sleep(max(sleep_secs, 1))
        try:
            capture_and_store(conn)
        except Exception as e:
            conn.rollback()
            log.exception("Scheduled capture failed — will retry next cycle")
            notify(f"Fellwatch: capture failed ({AREA})",
                   f"Scheduled capture failed: {type(e).__name__}: {e}. Next attempt tomorrow.")


if __name__ == "__main__":
    main()
