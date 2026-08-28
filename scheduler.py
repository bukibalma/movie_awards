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
import threading
import time

import db
import scraper
import plex_client
import tmdb_client
import films
from config import AWARDS

log = logging.getLogger("scheduler")

RECENT_WINDOW_YEARS = 1     # re-scrape ceremonies within +/- this many years of today
FUTURE_HORIZON_YEARS = 2    # how far ahead to seed placeholder ceremonies
PAST_HORIZON_YEARS = 5      # how far back to seed ceremonies
REQUEST_DELAY_SECONDS = 3   # be polite to Wikipedia between requests — the initial
                             # seed run hits ~80 ceremonies, so this adds up on purpose

_cycle_lock = threading.Lock()


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
    if not _cycle_lock.acquire(blocking=False):
        log.info("Auto-update cycle already running — skipping this trigger")
        return
    try:
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
    finally:
        _cycle_lock.release()


def resolve_tmdb_matches():
    """
    Resolves nominated films AND watched films to exact TMDb IDs + poster
    images (posters show throughout the app; the Radarr export additionally
    relies on the ID for unwatched films specifically). Only runs once a
    TMDb API key has been saved (Settings page). Results are cached
    indefinitely per normalized title — a film's TMDb ID doesn't change —
    so this only does work for titles it hasn't seen before.
    """
    api_key = db.get_setting("tmdb_api_key")
    if not api_key:
        return

    combined = films.titles_needing_posters()
    resolved = 0
    for norm, g in combined.items():
        if db.get_tmdb_match(norm) is not None:
            continue  # already resolved (or already a confirmed miss)
        if g.get("appearances"):
            hint = films.year_hint(g["appearances"], AWARDS)
        else:
            hint = g.get("watched_year")
        try:
            match = tmdb_client.search_movie(api_key, g["title"], year_hint=hint)
        except Exception as e:
            log.warning("TMDb lookup failed for %s: %s", g["title"], e)
            continue
        if match:
            db.set_tmdb_match(norm, match["tmdb_id"], match["year"], match["title"], match["poster_path"])
        else:
            db.set_tmdb_match(norm, None, None, None, None)  # cache the miss too
        resolved += 1
        time.sleep(0.3)  # TMDb's limits are generous, but no reason to hammer it
    if resolved:
        log.info("TMDb resolution: processed %d film(s)", resolved)


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
    # TMDb resolution is cached per title, so cheap to check often — new
    # unwatched films get an ID within a few minutes rather than a week.
    bg.add_job(resolve_tmdb_matches, "interval", minutes=30, next_run_time=datetime.datetime.now())
    bg.start()
    return bg
