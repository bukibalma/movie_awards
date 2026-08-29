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
import wikidata_client
import films
from config import AWARDS, guess_title

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


def _store_categories(ceremony_id, categories, wiki_title, wiki_url):
    db.clear_categories(ceremony_id)
    for cat in categories:
        cat_id = db.add_category(ceremony_id, cat["category"])
        for nom in cat["nominations"]:
            db.add_nomination(cat_id, nom["film"], nom.get("nominee"), nom["is_winner"])
    db.set_ceremony_status(ceremony_id, "scraped", wiki_title=wiki_title, wiki_url=wiki_url)


def _try_wikidata(award_code, year, guess_title):
    """
    Attempt the clean-data path: resolve the ceremony's Wikidata item and
    pull structured nominations. Returns (categories, wiki_title) on
    success, or (None, guess_title) if Wikidata has nothing usable —
    callers should fall back to scraping Wikipedia's own page in that case.
    """
    try:
        qid = wikidata_client.search_entity(guess_title)
        if not qid:
            return None, guess_title
        flat = wikidata_client.get_nominations(qid)
    except Exception as e:
        log.info("Wikidata lookup failed for %s: %s", guess_title, e)
        return None, guess_title

    if not flat:
        return None, guess_title

    grouped = {}
    for n in flat:
        grouped.setdefault(n["category"], []).append(
            {"film": n["film"], "nominee": None, "is_winner": n["is_winner"]}
        )
    categories = [{"category": cat, "nominations": noms} for cat, noms in grouped.items()]
    return categories, guess_title


def scrape_one(ceremony):
    award = AWARDS[ceremony["award_code"]]
    guess = guess_title(ceremony["award_code"], ceremony["year"])

    # Festivals (Cannes, Berlinale, Venice, Sundance) don't really have a
    # "nominees" concept the way Oscars-style awards do — a jury picks a
    # winner directly from the whole competition lineup, with no public
    # shortlist announced beforehand. Wikidata mostly only records the
    # winner ("award received") for these, never a nominee list, because
    # there usually isn't one to record. Wikipedia's "Official Selection"
    # table — the full competition lineup — is the better source for what
    # the person actually wants here, so skip Wikidata entirely for
    # festivals and go straight to the Wikipedia scraper.
    categories, wiki_title = (None, guess) if award.get("festival") else \
        _try_wikidata(ceremony["award_code"], ceremony["year"], guess)

    if categories:
        wiki_url = scraper.WIKI_BASE + wiki_title.replace(" ", "_")
        _store_categories(ceremony["id"], categories, wiki_title, wiki_url)
        log.info("Scraped %s %s from Wikidata: %d categories", award["name"], ceremony["year"], len(categories))
        return

    # Wikidata had nothing (item not found, no nomination statements yet,
    # or skipped entirely for a festival) — use the Wikipedia HTML scraper.
    try:
        html, title = scraper.resolve_page(ceremony["award_code"], ceremony["year"], award["name"])
        wiki_url = scraper.WIKI_BASE + title.replace(" ", "_")
        categories = scraper.parse_ceremony_html(html)
        if not categories:
            db.set_ceremony_status(ceremony["id"], "failed", wiki_title=title, wiki_url=wiki_url)
            log.info("No parseable data anywhere yet for %s %s (%s)", award["name"], ceremony["year"], title)
            return
        _store_categories(ceremony["id"], categories, title, wiki_url)
        log.info("Scraped %s %s from Wikipedia: %d categories", award["name"], ceremony["year"], len(categories))
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


def rescrape_all():
    """
    Manual, heavier operation: re-scrapes every non-manual ceremony
    regardless of its current status or how old it is. The normal weekly
    cycle deliberately skips already-'scraped' ceremonies outside the
    recent-year window (to avoid needless requests), which means a parser
    or data-source improvement never reaches older ceremonies on its own.
    This is how to retroactively apply such a fix to everything at once.
    """
    if not _cycle_lock.acquire(blocking=False):
        log.info("A scrape cycle is already running — try again shortly")
        return
    try:
        ceremonies = [c for c in db.list_ceremonies() if c["status"] != "manual"]
        log.info("Full re-scrape starting: %d ceremonies", len(ceremonies))
        for ceremony in ceremonies:
            scrape_one(ceremony)
            time.sleep(REQUEST_DELAY_SECONDS)
        log.info("Full re-scrape finished")
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
            db.set_tmdb_match(
                norm, match["tmdb_id"], match["year"], match["title"], match["poster_path"],
                runtime=match.get("runtime"), overview=match.get("overview"),
                vote_average=match.get("vote_average"), imdb_id=match.get("imdb_id"),
            )
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
