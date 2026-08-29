import datetime
import os
import threading
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify

import db
import scraper
import scheduler
import imdb_import
import films
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
            norm = normalize_title(n["film"])
            n["watched"] = norm in watched
            n["poster"] = films.poster_url(norm)
            n["tmdb_id"] = films.tmdb_id_for(norm)
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


@app.route("/unwatched")
def unwatched_page():
    groups = films.group_nominated_films()
    unwatched = films.unwatched_groups()
    manual_flags = db.list_radarr_manual_titles()
    film_list = []
    for norm, g in unwatched.items():
        match = db.get_tmdb_match(norm)
        g["tmdb_matched"] = bool(match and match.get("tmdb_id"))
        g["tmdb_id"] = films.tmdb_id_for(norm)
        g["poster"] = films.poster_url(norm)
        g["is_short"] = films.is_short_film(norm)
        g["radarr_manual"] = norm in manual_flags
        g["normalized_title"] = norm
        film_list.append(g)
    film_list.sort(key=lambda g: g["title"].lower())

    tmdb_configured = bool(db.get_setting("tmdb_api_key"))
    return render_template(
        "unwatched.html", films=film_list, total=len(groups), awards=AWARDS,
        radarr_url=url_for("radarr_export", _external=True),
        radarr_manual_url=url_for("radarr_export_manual", _external=True),
        tmdb_configured=tmdb_configured,
    )


@app.route("/film/toggle-radarr", methods=["POST"])
def toggle_radarr_manual():
    norm = request.form.get("normalized_title", "")
    value = request.form.get("value") == "1"
    if norm:
        db.set_radarr_manual(norm, value)
    return redirect(request.referrer or url_for("unwatched_page"))


@app.route("/film/<int:tmdb_id>")
def film_detail(tmdb_id):
    match = db.get_tmdb_match_by_id(tmdb_id)
    if not match:
        return "Not found", 404
    norm = match["normalized_title"]

    groups = films.group_nominated_films()
    appearances = groups.get(norm, {}).get("appearances", [])
    watched = norm in db.watched_normalized_titles()
    radarr_manual = norm in db.list_radarr_manual_titles()

    ratings = None
    omdb_key = db.get_setting("omdb_api_key")
    if match.get("imdb_id") and omdb_key:
        ratings = db.get_omdb_ratings(match["imdb_id"])
        if ratings is None:
            import omdb_client
            try:
                fetched = omdb_client.get_ratings(omdb_key, match["imdb_id"])
                if fetched:
                    db.set_omdb_ratings(match["imdb_id"], fetched["imdb_rating"],
                                         fetched["rotten_tomatoes"], fetched["metacritic"])
                    ratings = fetched
            except Exception:
                pass

    return render_template(
        "film.html", match=match, appearances=appearances, watched=watched,
        radarr_manual=radarr_manual, ratings=ratings,
        poster=films.poster_url(norm, size="w500"),
        omdb_configured=bool(omdb_key), awards=AWARDS,
    )


@app.route("/radarr.json")
def radarr_export():
    """
    Fully automatic feed: every unwatched nominated film, excluding
    confirmed short films, in the plain array format Radarr's 'Custom
    List' import type expects. Films resolved to a TMDb ID get sent with
    that ID for an exact match; otherwise Radarr looks the title up itself.
    """
    unwatched = films.unwatched_groups()
    payload = []
    for norm, g in unwatched.items():
        if films.is_short_film(norm):
            continue
        match = db.get_tmdb_match(norm)
        if match and match.get("tmdb_id"):
            payload.append({
                "tmdbId": match["tmdb_id"],
                "title": match["matched_title"] or g["title"],
                "year": match["year"],
            })
        else:
            payload.append({"title": g["title"]})
    payload.sort(key=lambda d: d["title"].lower())
    return jsonify(payload)


@app.route("/radarr-manual.json")
def radarr_export_manual():
    """Curated feed: only films explicitly checked via the manual-Radarr
    checkbox, regardless of watched/short-film status — the checkbox is
    treated as an explicit override of the automatic filters."""
    manual_titles = db.list_radarr_manual_titles()
    groups = films.group_nominated_films()
    payload = []
    for norm in manual_titles:
        title = groups.get(norm, {}).get("title")
        match = db.get_tmdb_match(norm)
        if match and match.get("tmdb_id"):
            payload.append({
                "tmdbId": match["tmdb_id"],
                "title": match["matched_title"] or title or norm,
                "year": match["year"],
            })
        elif title:
            payload.append({"title": title})
    payload.sort(key=lambda d: d["title"].lower())
    return jsonify(payload)


@app.route("/settings")
def settings_page():
    plex_configured = bool(db.get_setting("plex_base_url") and db.get_setting("plex_token"))
    tmdb_configured = bool(db.get_setting("tmdb_api_key"))
    omdb_configured = bool(db.get_setting("omdb_api_key"))
    unwatched_count = len(films.unwatched_groups())
    resolved_count = sum(
        1 for norm in films.unwatched_groups()
        if (db.get_tmdb_match(norm) or {}).get("tmdb_id")
    ) if tmdb_configured else 0
    return render_template(
        "settings.html",
        plex_base_url=db.get_setting("plex_base_url", ""),
        plex_configured=plex_configured,
        plex_last_synced=db.get_setting("plex_last_synced"),
        tmdb_configured=tmdb_configured,
        omdb_configured=omdb_configured,
        unwatched_count=unwatched_count,
        resolved_count=resolved_count,
        radarr_url=url_for("radarr_export", _external=True),
        radarr_manual_url=url_for("radarr_export_manual", _external=True),
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


@app.route("/settings/tmdb", methods=["POST"])
def settings_tmdb_save():
    import tmdb_client

    api_key = request.form.get("tmdb_api_key", "").strip()
    if not api_key:
        flash("TMDb API key is required.", "error")
        return redirect(url_for("settings_page"))

    try:
        tmdb_client.test_api_key(api_key)
    except Exception as e:
        flash(f"Couldn't verify that TMDb key: {e}", "error")
        return redirect(url_for("settings_page"))

    db.set_setting("tmdb_api_key", api_key)
    flash("TMDb key saved. Resolving film IDs in the background — this can take a few minutes.", "success")
    threading.Thread(target=scheduler.resolve_tmdb_matches, daemon=True).start()
    return redirect(url_for("settings_page"))


@app.route("/settings/tmdb/resolve-now", methods=["POST"])
def settings_tmdb_resolve_now():
    threading.Thread(target=scheduler.resolve_tmdb_matches, daemon=True).start()
    flash("TMDb resolution started in the background.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/omdb", methods=["POST"])
def settings_omdb_save():
    import omdb_client

    api_key = request.form.get("omdb_api_key", "").strip()
    if not api_key:
        flash("OMDb API key is required.", "error")
        return redirect(url_for("settings_page"))

    try:
        omdb_client.test_api_key(api_key)
    except Exception as e:
        flash(f"Couldn't verify that OMDb key: {e}", "error")
        return redirect(url_for("settings_page"))

    db.set_setting("omdb_api_key", api_key)
    flash("OMDb key saved. Ratings will now show on movie detail pages.", "success")
    return redirect(url_for("settings_page"))


@app.route("/watched")
def watched_page():
    watched = db.list_watched()
    for w in watched:
        norm = normalize_title(w["title"])
        w["poster"] = films.poster_url(norm)
        w["tmdb_id"] = films.tmdb_id_for(norm)
    return render_template("watched.html", watched=watched)


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
