"""
Re-parses archived captures from their raw PDFs and rewrites their rows.

Run inside the container after a parser fix, to backfill captures that were
stored with missing/failed fields (parse_ok=0, or no hourly rows):

    docker exec fellwatch python reparse.py            # only captures that need it
    docker exec fellwatch python reparse.py --all      # every capture
"""

import argparse
import sqlite3
from pathlib import Path

import main


def main_cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="re-parse every capture, not just failed ones")
    args = ap.parse_args()

    conn = sqlite3.connect(main.DB_PATH)
    main.init_db(conn)
    where = "" if args.all else (
        "WHERE parse_ok = 0 OR NOT EXISTS "
        "(SELECT 1 FROM hourly_readings h WHERE h.capture_id = captures.capture_id)"
    )
    rows = conn.execute(
        f"SELECT capture_id, raw_pdf_path FROM captures {where} ORDER BY capture_id"
    ).fetchall()

    fixed = failed = missing = 0
    for capture_id, pdf_path in rows:
        path = Path(pdf_path)
        if not path.exists():
            main.log.warning("Capture %s: raw PDF %s not found, skipping", capture_id, pdf_path)
            missing += 1
            continue
        pdf_bytes = path.read_bytes()
        text = main.extract_text(pdf_bytes, main.DATA_DIR / "_tmp.pdf")
        path.with_suffix(".txt").write_text(text, encoding="utf-8")
        main.parse_and_store(conn, capture_id, text, pdf_bytes, quiet=True)
        ok = conn.execute("SELECT parse_ok FROM captures WHERE capture_id=?", (capture_id,)).fetchone()[0]
        fixed, failed = fixed + ok, failed + (1 - ok)
    print(f"Re-parsed {len(rows)} captures: {fixed} clean, {failed} still failing, {missing} missing PDF")


if __name__ == "__main__":
    main_cli()
