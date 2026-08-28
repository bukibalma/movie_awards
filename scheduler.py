"""
Automatic update loop. No manual button-clicking required:

- On every run, makes sure ceremony rows exist for the tracked year range
  (last 5 years + upcoming), for every award.
- Attempts to scrape any ceremony that hasn't been successfully scraped yet
  (nominations may not be announced/on Wikipedia yet — it just retries next
  run, so whenever the page appears it gets picked up within one cycle).
- Re-scrapes ceremonies close to today's date even if already scraped, since
  nominee lists get filled in and winners get added on the night of the
  ceremony — this is how the app "notices" an update without being told.
- Skips 'manual' ceremonies (the user curated those by hand) and gives up
  retrying a given ceremony after a generous attempt cap, so a far-future
  year with no Wikipedia page yet doesn't get hit forever.

Runs inside the same process via APScheduler — once at startup, then on a
fixed interval — so it works automatically as long as the container is up.
"""
import datetime
import logging
import time

import db
import scraper
import plex_client
from config import AWARDS

log = logging.getLogger("scheduler")

RECENT_WINDOW_YEARS = 1     # re-scrape ceremonies within +/- this many years of today
FUTURE_HORIZON_YEARS = 2    # how far ahead to seed placeholder ceremonies
PAST_HORIZON_YEARS = 5      # how far back to seed ceremonies
REQUEST_DELAY_SECONDS = 1.5 # be polite to Wikipedia between requests


def _year_range():
    today = datetime.date.today().year
    return today - PAST_HORIZON_YEARS, today + FUTURE_HORIZON_YEARS


def ensure_seeded():
    start, end = _year_range()
    for code in AWARDS:
        for year in range(start, end + 1):
            db.get_or_create_ceremony(code, year)


def scrape_one(ceremony):
    award = AWARDS[ceremony["award_code"]]
    try:
        html, title = scraper.resolve_page(ceremony["award_code"], ceremony["year"], award["name"])
        categories = scraper.parse_ceremony_html(html)
        wiki_url = scraper.WIKI_BASE + title.replace(" ", "_")
        if not categories:
            db.set_ceremony_status(ceremony["id"], "failed", wiki_title=title, wiki_url=wiki_url)
            log.info("No parseable table yet for %s %s (%s)", award["name"], ceremony["year"], title)
            return

        db.clear_categories(ceremony["id"])
        for cat in categories:
            cat_id = db.add_category(ceremony["id"], cat["category"])
            for nom in cat["nominations"]:
                db.add_nomination(cat_id, nom["film"], nom["nominee"], nom["is_winner"])

        db.set_ceremony_status(ceremony["id"], "scraped", wiki_title=title, wiki_url=wiki_url)
        log.info("Scraped %s %s: %d categories", award["name"], ceremony["year"], len(categories))
    except scraper.ScrapeError:
        db.set_ceremony_status(ceremony["id"], "failed")
        log.info("No Wikipedia page yet for %s %s", award["name"], ceremony["year"])
    except Exception as e:
        db.set_ceremony_status(ceremony["id"], "failed")
        log.warning("Scrape error for %s %s: %s", award["name"], ceremony["year"], e)


def run_cycle():
    log.info("Auto-update cycle starting")
    ensure_seeded()

    current_year = datetime.date.today().year
    recent_years = (current_year - RECENT_WINDOW_YEARS, current_year + RECENT_WINDOW_YEARS)
    max_year = current_year + 1  # don't bother attempting years further out; no page will exist

    due = db.ceremonies_due_for_scrape(recent_years, max_year)
    log.info("Auto-update cycle: %d ceremonies due", len(due))
    for ceremony in due:
        scrape_one(ceremony)
        time.sleep(REQUEST_DELAY_SECONDS)
    log.info("Auto-update cycle finished")


def sync_plex():
    """
    Pull newly-watched movies from Plex's own watch-history API. Only runs
    once Plex connection settings have been saved (see /settings in the
    app). Safe to call on any cadence — add_watched() dedupes by title, and
    we only ask Plex for history after our last known checkpoint.
    """
    base_url = db.get_setting("plex_base_url")
    token = db.get_setting("plex_token")
    if not base_url or not token:
        return  # not configured yet

    since = int(db.get_setting("plex_last_synced", "0"))
    try:
        movies = plex_client.get_new_watched_movies(base_url, token, since_epoch=since)
    except Exception as e:
        log.warning("Plex sync failed: %s", e)
        return

    if not movies:
        return

    added = 0
    max_viewed_at = since
    for m in movies:
        if db.add_watched(m["title"], m["year"], imdb_id=None, source="plex"):
            added += 1
        max_viewed_at = max(max_viewed_at, m["viewed_at"])

    db.set_setting("plex_last_synced", str(max_viewed_at))
    log.info("Plex sync: %d new watched movie(s) added", added)


def start(interval_hours=24 * 7):
    from apscheduler.schedulers.background import BackgroundScheduler
    import watch_import

    bg = BackgroundScheduler(daemon=True)
    bg.add_job(run_cycle, "interval", hours=interval_hours, next_run_time=datetime.datetime.now())
    # Cheap local file check — fine to run far more often than the Wikipedia cycle.
    bg.add_job(watch_import.scan_import_folder, "interval", minutes=5,
               next_run_time=datetime.datetime.now())
    # Plex is a real local API, so this can run frequently without concern.
    bg.add_job(sync_plex, "interval", minutes=15, next_run_time=datetime.datetime.now())
    bg.start()
    return bg
