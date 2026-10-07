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
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import pdfplumber
import requests

PARSER_VERSION = "1.0.0"

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
    m = re.search(r"Forecast Issued on ([A-Za-z]+ \d{1,2} [A-Za-z]+ \d{4}) at (\d{2}:\d{2})", text)
    return f"{m.group(1)} {m.group(2)}" if m else None


def parse_confidence(text: str):
    m = re.search(r"Confidence:\s*(.+)", text)
    return m.group(1).strip() if m else None


def parse_headline(text: str):
    m = re.search(r"Headline for [^:]+:\s*(.+)", text)
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


def parse_day1_weather_precip(text: str):
    """Returns dict: time -> (weather_desc, precip_pct)."""
    block = re.search(
        r"Weather and chance of precipitation at 800m.*?"
        r"(\d{2}:\d{2}(?:\s+\d{2}:\d{2}){5})\n(.+?)\nPrecipitation probability\n(.+?)\n",
        text, re.S,
    )
    if not block:
        return {}
    times = block.group(1).split()
    weathers_line = block.group(2).strip()
    # weather descriptions are multi-word and space-separated with no fixed
    # delimiter; this greedy split works for the standard vocabulary but
    # verify against live output — see README.
    weather_words = re.split(r"(?<=[a-z])\s(?=[A-Z])", weathers_line)
    precips = re.findall(r"\d{1,3}%", block.group(3))
    out = {}
    for i, t in enumerate(times):
        out[t] = {
            "weather_desc": weather_words[i] if i < len(weather_words) else None,
            "precip_pct": int(precips[i].rstrip("%")) if i < len(precips) else None,
        }
    return out


def parse_forecast_dates(text: str):
    """Returns [(day_number, date_iso), ...] from the 'Forecast for <day> <iso>' lines."""
    return re.findall(r"Forecast for [A-Za-z]+ \d{1,2} [A-Za-z]+ \d{4} (\d{4}-\d{2}-\d{2})", text)


def parse_day1_detail(text: str):
    m_weather = re.search(r"Weather:\s*(.+?)\nChance of cloud-free hill tops", text, re.S)
    m_cloud = re.search(r"Chance of cloud-free hill tops above 800m:\s*(\d+)%", text)
    m_vis = re.search(r"Low cloud and visibility:\s*(.+?)\n", text)
    m_met = re.search(r"Meteorologist.s View\s*\n(.+?)\nForecast for", text, re.S)
    return {
        "weather_text": " ".join(m_weather.group(1).split()) if m_weather else None,
        "cloud_free_pct": int(m_cloud.group(1)) if m_cloud else None,
        "low_cloud_vis": m_vis.group(1).strip() if m_vis else None,
        "meteorologist_view": m_met.group(1).strip() if m_met else None,
    }


def parse_wind_table(text: str):
    """Returns list of (altitude, time_index, mean_mph, gust_mph, direction)."""
    m = re.search(
        r"Wind Speed mean/gusts \(mph\) Wind Direction.*?\n"
        r"(\d{2}:\d{2}(?:\s+\d{2}:\d{2}){6})\n"
        r"((?:(?:900|600|300|Valley).+\n?){4})",
        text,
    )
    if not m:
        return []
    times = m.group(1).split()
    rows = []
    for line in m.group(2).strip().splitlines():
        parts = line.split()
        altitude = parts[0]
        rest = parts[1:]
        # each timepoint is "mean/gust" then "DIR" e.g. "16/26 SE"
        readings = []
        i = 0
        while i < len(rest) - 1:
            speed_gust, direction = rest[i], rest[i + 1]
            mean_s, gust_s = speed_gust.split("/")
            readings.append((int(mean_s), int(gust_s), direction))
            i += 2
        for t, (mean_s, gust_s, direction) in zip(times, readings):
            rows.append((altitude, t, mean_s, gust_s, direction))
    return rows


def parse_temp_table(text: str):
    """Returns (rows, freezing_levels) where rows = list of
    (altitude, time, temp_c, feels_like_c) and freezing_levels = list of (time, metres)."""
    m = re.search(
        r"Temperature / Feels Like.*?\n"
        r"(\d{2}:\d{2}(?:\s+\d{2}:\d{2}){6})\n"
        r"((?:(?:900|600|300|Valley).+\n?){4})"
        r"Freezing Level\*\s*(.+)\n",
        text,
    )
    if not m:
        return [], []
    times = m.group(1).split()
    rows = []
    for line in m.group(2).strip().splitlines():
        parts = line.split()
        altitude = parts[0]
        pairs = parts[1:]
        for t, pair in zip(times, pairs):
            temp_s, feels_s = pair.split("/")
            rows.append((altitude, t, int(temp_s), int(feels_s)))
    fz_values = re.findall(r"(\d+)m", m.group(3))
    freezing = list(zip(times, [int(v) for v in fz_values]))
    return rows, freezing


def parse_day2(text: str):
    # Isolate the block starting at the second "Forecast for <date> <iso>" marker
    # first — "Weather:" and "Chance of cloud-free hill tops" also appear in the
    # day-1 detail section, so searching the whole document risks matching there.
    markers = list(re.finditer(r"Forecast for [A-Za-z]+ \d{1,2} [A-Za-z]+ \d{4} \d{4}-\d{2}-\d{2}", text))
    if len(markers) < 2:
        return {}
    day2_text = text[markers[1].end():]

    m = re.search(
        r"Weather:\s*(.+?)\nMaximum wind:\s*(.+?)\n"
        r"Chance of cloud-free hill tops above 800m:\s*(\d+)%\n"
        r"Low cloud and visibility:\s*(.+?)\n"
        r"Temperature:\s*(.+?)\n(.+?)\nFreezing level:\s*(.+?)\n",
        day2_text, re.S,
    )
    if not m:
        return {}
    return {
        "weather_text": " ".join(m.group(1).split()),
        "max_wind_text": m.group(2).strip(),
        "cloud_free_pct": int(m.group(3)),
        "low_cloud_vis": " ".join(m.group(4).split()),
        "temperature_text": " ".join((m.group(5) + " " + m.group(6)).split()),
        "freezing_level_text": m.group(7).strip(),
    }


def parse_ground_conditions(text: str):
    m = re.search(r"Ground Conditions Supplement\s*\n(.+?)\nIssued at:", text, re.S)
    return m.group(1).strip() if m else None


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
    raw_text_path.write_text(text)

    errors = []

    def safe(label, fn, *args):
        try:
            return fn(*args)
        except Exception:
            log.exception("Failed to parse field: %s", label)
            errors.append(label)
            return None

    issued_at = safe("issued_at", parse_issued, text)
    dates = parse_forecast_dates(text) or []  # [day1_date, day2_date]

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO captures (captured_at, issued_at, area, source_url, parser_version, "
        "raw_pdf_path, raw_text_path, parse_ok, parse_errors) VALUES (?,?,?,?,?,?,?,1,NULL)",
        (now.isoformat(), issued_at, AREA, PDF_URL, PARSER_VERSION,
         str(raw_pdf_path), str(raw_text_path)),
    )
    capture_id = cur.lastrowid

    confidence = safe("confidence", parse_confidence, text)
    headline = safe("headline", parse_headline, text)
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
        day2 = safe("day2", parse_day2, text) or {}
        cur.execute(
            "INSERT INTO daily_summary (capture_id, forecast_date, forecast_day, weather_text, "
            "max_wind_text, cloud_free_pct, low_cloud_vis, freezing_level_text) "
            "VALUES (?,?,2,?,?,?,?,?)",
            (capture_id, dates[1], day2.get("weather_text"), day2.get("max_wind_text"),
             day2.get("cloud_free_pct"), day2.get("low_cloud_vis"), day2.get("freezing_level_text")),
        )

    hazards = safe("hazards", parse_hazards, text) or []
    for name, likelihood in hazards:
        if dates:
            cur.execute(
                "INSERT INTO hazards (capture_id, forecast_date, hazard_name, likelihood) VALUES (?,?,?,?)",
                (capture_id, dates[0], name, likelihood),
            )

    weather_precip = safe("weather_precip", parse_day1_weather_precip, text) or {}
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

    wind_rows = safe("wind_table", parse_wind_table, text) or []
    if dates:
        for altitude, t, mean_s, gust_s, direction in wind_rows:
            # the final 00:00 column belongs to the following calendar day
            fdate = dates[1] if (t == "00:00" and len(dates) > 1) else dates[0]
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
    if dates:
        for altitude, t, temp_c, feels_c in temp_rows:
            fdate = dates[1] if (t == "00:00" and len(dates) > 1) else dates[0]
            fdt = f"{fdate}T{t}"
            alt_label = "valley" if altitude.lower() == "valley" else f"{altitude}m"
            for metric, val in (("temperature_c", temp_c), ("feels_like_c", feels_c)):
                cur.execute(
                    "INSERT INTO hourly_readings (capture_id, forecast_time, altitude, metric, value_numeric) "
                    "VALUES (?,?,?,?,?)",
                    (capture_id, fdt, alt_label, metric, val),
                )
        for t, metres in freezing:
            fdate = dates[1] if (t == "00:00" and len(dates) > 1) else dates[0]
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
        notify(f"Fellwatch: partial parse ({AREA})",
               f"Capture {capture_id} stored, but these fields failed to parse: {', '.join(errors)}")
    else:
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
            log.exception("Scheduled capture failed — will retry next cycle")
            notify(f"Fellwatch: capture failed ({AREA})",
                   f"Scheduled capture failed: {type(e).__name__}: {e}. Next attempt tomorrow.")


if __name__ == "__main__":
    main()
