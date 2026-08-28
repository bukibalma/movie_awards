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


def poster_url(normalized_title):
    import tmdb_client
    match = db.get_tmdb_match(normalized_title)
    return tmdb_client.poster_url(match["poster_path"]) if match else None


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
