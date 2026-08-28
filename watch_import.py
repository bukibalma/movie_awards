"""
Watches a local folder for IMDb CSV exports so importing doesn't require
opening the app. IMDb has no automatable pull path (robots.txt disallows
scraping their pages, and the CSV export button requires a logged-in
browser session), so this is the closest thing to "automatic": drop the
file, the app notices it on its own.
"""
import logging
import os
import shutil
import datetime

import db
import imdb_import

log = logging.getLogger("watch_import")

IMPORT_DIR = os.environ.get("IMDB_IMPORT_DIR", "/import")
PROCESSED_DIR = os.path.join(IMPORT_DIR, "processed")


def scan_import_folder():
    if not os.path.isdir(IMPORT_DIR):
        return
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    csv_files = [
        f for f in os.listdir(IMPORT_DIR)
        if f.lower().endswith(".csv") and os.path.isfile(os.path.join(IMPORT_DIR, f))
    ]
    if not csv_files:
        return

    for filename in csv_files:
        path = os.path.join(IMPORT_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            added, skipped, total = imdb_import.import_into_db(db, text)
            log.info(
                "Auto-imported %s: %d new, %d already present, %d total rows",
                filename, added, skipped, total,
            )
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = os.path.join(PROCESSED_DIR, f"{stamp}-{filename}")
            shutil.move(path, dest)
        except Exception as e:
            log.warning("Failed to auto-import %s: %s", filename, e)
