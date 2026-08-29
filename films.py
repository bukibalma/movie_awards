"""Groups raw nomination rows into one entry per film — shared between the
web routes (app.py) and the background TMDb-resolution job (scheduler.py)
so both use the exact same grouping/dedup logic."""
import db
from utils import normalize_title


def group_nominated_films():
    """{normalized_title: {"title": str, "appearances": [row, ...]}}"""
    groups = {}
    for r in db.all_nominations_with_context():
        norm = normalize_title(r["film"])
        if not norm:
            continue
        g = groups.setdefault(norm, {"title": r["film"], "appearances": []})
        g["appearances"].append(r)
    return groups


def unwatched_groups():
    groups = group_nominated_films()
    watched = db.watched_normalized_titles()
    return {norm: g for norm, g in groups.items() if norm not in watched}


def titles_needing_posters():
    """
    Every normalized title that could use a poster/TMDb match — every
    nominated film (watched or not, since posters show everywhere) plus
    every watched film (many won't overlap with nominees at all). Returns
    {normalized_title: {"title": display_title, "year_hint": int|None}}.
    """
    combined = {}
    for norm, g in group_nominated_films().items():
        combined[norm] = {"title": g["title"], "appearances": g["appearances"]}
    for w in db.list_watched():
        norm = normalize_title(w["title"])
        if norm and norm not in combined:
            combined[norm] = {"title": w["title"], "appearances": [], "watched_year": w["year"]}
    return combined


def poster_url(normalized_title, size="w342"):
    import tmdb_client
    match = db.get_tmdb_match(normalized_title)
    return tmdb_client.poster_url(match["poster_path"], size=size) if match else None


def display_title_for(normalized_title):
    """
    Best available human-readable title for a normalized key, checking
    nominated films first, then the watched list (for titles that only
    exist there, e.g. a film added to the manual list from the Watched
    page that never appeared in any award nomination), then a resolved
    TMDb match, falling back to the normalized key itself as a last resort.
    """
    groups = group_nominated_films()
    if normalized_title in groups:
        return groups[normalized_title]["title"]
    for w in db.list_watched():
        if normalize_title(w["title"]) == normalized_title:
            return w["title"]
    match = db.get_tmdb_match(normalized_title)
    if match and match.get("matched_title"):
        return match["matched_title"]
    return normalized_title.title()


def tmdb_id_for(normalized_title):
    match = db.get_tmdb_match(normalized_title)
    return match["tmdb_id"] if match and match.get("tmdb_id") else None


SHORT_FILM_RUNTIME_MINUTES = 40


def is_short_film(normalized_title):
    """
    True only when we have a confirmed short runtime — unresolved or
    unknown-runtime films are never excluded on this basis, since we'd
    rather show an extra short than silently drop a real feature due to
    missing data.
    """
    match = db.get_tmdb_match(normalized_title)
    runtime = match.get("runtime") if match else None
    return runtime is not None and runtime < SHORT_FILM_RUNTIME_MINUTES


def year_hint(appearances, awards_config):
    """
    Best guess at a film's actual release year, from its ceremony
    appearances. Award shows typically honor the *previous* year's films
    (e.g. the 2026 Oscars cover 2025 releases); festivals screen the same
    year's films. Takes the earliest inferred year across appearances.
    """
    years = []
    for a in appearances:
        cfg = awards_config.get(a["award_code"], {})
        y = a["year"] - 1 if not cfg.get("festival") else a["year"]
        years.append(y)
    return min(years) if years else None
