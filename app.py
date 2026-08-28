import datetime
import os
import threading
from flask import Flask, render_template, redirect, url_for, request, flash

import db
import scraper
import scheduler
import imdb_import
from config import AWARDS
from utils import normalize_title

app = Flask(__name__)
app.secret_key = "movie-awards-tracker"

db.init_db()

CURRENT_YEAR = datetime.date.today().year
DEFAULT_YEARS = list(range(CURRENT_YEAR - 5, CURRENT_YEAR + 3))  # last 5 years + future

# Start the background auto-update loop once per process. The container
# runs a single gunicorn worker (see Dockerfile) specifically so this only
# starts once; set DISABLE_SCHEDULER=1 to turn it off (e.g. for tests).
if os.environ.get("DISABLE_SCHEDULER") != "1":
    scheduler.start(interval_hours=24 * 7)


@app.route("/")
def index():
    ceremonies = db.list_ceremonies()
    by_award = {}
    for c in ceremonies:
        by_award.setdefault(c["award_code"], []).append(c)
    return render_template(
        "index.html", awards=AWARDS, by_award=by_award, default_years=DEFAULT_YEARS
    )


@app.route("/force-update", methods=["POST"])
def force_update():
    """
    Manually kick the same auto-update cycle the scheduler runs on its own,
    without waiting for the next scheduled run. Runs in a background thread
    so the request doesn't hang while it works through every award/year.
    """
    threading.Thread(target=scheduler.run_cycle, daemon=True).start()
    flash("Update started in the background — refresh this page in a minute or two.", "success")
    return redirect(url_for("index"))


@app.route("/award/<code>")
def award_detail(code):
    if code not in AWARDS:
        return "Unknown award", 404
    ceremonies = db.list_ceremonies(code)
    return render_template("award.html", code=code, award=AWARDS[code], ceremonies=ceremonies)


@app.route("/ceremony/<int:ceremony_id>")
def ceremony_detail(ceremony_id):
    ceremony = db.get_ceremony(ceremony_id)
    if not ceremony:
        return "Not found", 404
    award = AWARDS[ceremony["award_code"]]
    data = db.get_categories_with_nominations(ceremony_id)
    watched = db.watched_normalized_titles()
    for entry in data:
        for n in entry["nominations"]:
            n["watched"] = normalize_title(n["film"]) in watched
    return render_template("ceremony.html", ceremony=ceremony, award=award, data=data)


@app.route("/ceremony/<int:ceremony_id>/scrape", methods=["POST"])
def scrape_ceremony(ceremony_id):
    ceremony = db.get_ceremony(ceremony_id)
    if not ceremony:
        return "Not found", 404
    award = AWARDS[ceremony["award_code"]]
    try:
        html, title = scraper.resolve_page(ceremony["award_code"], ceremony["year"], award["name"])
        categories = scraper.parse_ceremony_html(html)
        if not categories:
            db.set_ceremony_status(ceremony_id, "failed", wiki_title=title,
                                    wiki_url=scraper.WIKI_BASE + title.replace(" ", "_"))
            flash(f"Fetched '{title}' but couldn't find a nominee table to parse. "
                  f"You can add nominations manually.", "warning")
            return redirect(url_for("ceremony_detail", ceremony_id=ceremony_id))

        db.clear_categories(ceremony_id)
        for cat in categories:
            cat_id = db.add_category(ceremony_id, cat["category"])
            for nom in cat["nominations"]:
                db.add_nomination(cat_id, nom["film"], nom["nominee"], nom["is_winner"])

        db.set_ceremony_status(ceremony_id, "scraped", wiki_title=title,
                                wiki_url=scraper.WIKI_BASE + title.replace(" ", "_"))
        flash(f"Scraped {len(categories)} categories from '{title}'.", "success")
    except scraper.ScrapeError as e:
        db.set_ceremony_status(ceremony_id, "failed")
        flash(str(e), "error")
    except Exception as e:
        db.set_ceremony_status(ceremony_id, "failed")
        flash(f"Unexpected error: {e}", "error")
    return redirect(url_for("ceremony_detail", ceremony_id=ceremony_id))


@app.route("/ceremony/<int:ceremony_id>/add", methods=["POST"])
def add_manual(ceremony_id):
    ceremony = db.get_ceremony(ceremony_id)
    if not ceremony:
        return "Not found", 404
    category_name = request.form.get("category", "").strip()
    film = request.form.get("film", "").strip()
    nominee = request.form.get("nominee", "").strip() or None
    is_winner = bool(request.form.get("is_winner"))

    if not category_name or not film:
        flash("Category and film are required.", "error")
        return redirect(url_for("ceremony_detail", ceremony_id=ceremony_id))

    data = db.get_categories_with_nominations(ceremony_id)
    existing = next((d["category"] for d in data if d["category"]["name"] == category_name), None)
    cat_id = existing["id"] if existing else db.add_category(ceremony_id, category_name)
    db.add_nomination(cat_id, film, nominee, is_winner)
    if ceremony["status"] == "pending":
        db.set_ceremony_status(ceremony_id, "manual")
    flash("Added.", "success")
    return redirect(url_for("ceremony_detail", ceremony_id=ceremony_id))


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results = db.search_nominations(q) if q else []
    return render_template("search.html", q=q, results=results, awards=AWARDS)


def _group_nominated_films():
    """Group all nominations by normalized title -> {title, appearances[]}."""
    groups = {}
    for r in db.all_nominations_with_context():
        norm = normalize_title(r["film"])
        if not norm:
            continue
        g = groups.setdefault(norm, {"title": r["film"], "appearances": []})
        g["appearances"].append(r)
    return groups


@app.route("/settings")
def settings_page():
    plex_configured = bool(db.get_setting("plex_base_url") and db.get_setting("plex_token"))
    return render_template(
        "settings.html",
        plex_base_url=db.get_setting("plex_base_url", ""),
        plex_configured=plex_configured,
        plex_last_synced=db.get_setting("plex_last_synced"),
    )


@app.route("/settings/plex", methods=["POST"])
def settings_plex_save():
    import plex_client

    base_url = request.form.get("plex_base_url", "").strip().rstrip("/")
    token = request.form.get("plex_token", "").strip()
    if not base_url or not token:
        flash("Both server URL and token are required.", "error")
        return redirect(url_for("settings_page"))

    try:
        plex_client.test_connection(base_url, token)
    except Exception as e:
        flash(f"Couldn't connect to Plex: {e}", "error")
        return redirect(url_for("settings_page"))

    db.set_setting("plex_base_url", base_url)
    db.set_setting("plex_token", token)
    if db.get_setting("plex_last_synced") is None:
        db.set_setting("plex_last_synced", "0")  # first sync pulls all existing history
    flash("Connected to Plex. Watched movies will sync automatically from now on.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/plex/sync-now", methods=["POST"])
def settings_plex_sync_now():
    threading.Thread(target=scheduler.sync_plex, daemon=True).start()
    flash("Plex sync started in the background.", "success")
    return redirect(url_for("settings_page"))


@app.route("/watched")
def watched_page():
    return render_template("watched.html", watched=db.list_watched())


@app.route("/watched/import", methods=["POST"])
def watched_import():
    file = request.files.get("csvfile")
    text = file.read().decode("utf-8", errors="ignore") if file and file.filename else request.form.get("csv_text", "")
    if not text.strip():
        flash("No data provided — choose a file or paste something first.", "error")
        return redirect(url_for("watched_page"))

    added, skipped, total = imdb_import.import_into_db(db, text)
    flash(f"Imported {added} new film(s) from {total} row(s) ({skipped} were already on your list).", "success")
    return redirect(url_for("watched_page"))


@app.route("/watched/<int:watched_id>/delete", methods=["POST"])
def watched_delete(watched_id):
    db.delete_watched(watched_id)
    return redirect(url_for("watched_page"))


@app.route("/watched/clear", methods=["POST"])
def watched_clear():
    db.clear_watched()
    flash("Watched list cleared.", "success")
    return redirect(url_for("watched_page"))


@app.route("/unwatched")
def unwatched_page():
    groups = _group_nominated_films()
    watched = db.watched_normalized_titles()
    films = sorted(
        (g for norm, g in groups.items() if norm not in watched),
        key=lambda g: g["title"].lower(),
    )
    return render_template(
        "unwatched.html", films=films, total=len(groups), awards=AWARDS
    )


@app.route("/add-year", methods=["POST"])
def add_year():
    code = request.form.get("award_code")
    year = request.form.get("year", type=int)
    if code in AWARDS and year:
        db.get_or_create_ceremony(code, year)
        flash(f"Added {AWARDS[code]['name']} {year}.", "success")
    return redirect(request.referrer or url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
