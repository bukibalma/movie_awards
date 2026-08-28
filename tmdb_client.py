"""
Resolves a film title (plus an approximate year, when we have one) to an
exact TMDb ID via TMDb's free search API. This is what makes the Radarr
export deterministic — Radarr matches on the ID directly instead of
re-guessing from a bare title, which is where ambiguity (wrong film with
the same/similar title) could creep in.
"""
import requests

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w200"
TIMEOUT = 15


def poster_url(poster_path):
    return f"{IMAGE_BASE}{poster_path}" if poster_path else None


def search_movie(api_key, title, year_hint=None):
    """
    Return {"tmdb_id", "title", "year", "poster_path"} for the best match,
    or None if TMDb has nothing for this title. TMDb's own results are
    already ranked by relevance/popularity; when we have a year_hint, we
    prefer whichever result's release year is closest to it, since a
    well-known awards-nominated film should be an exact or near-exact
    year match.
    """
    resp = requests.get(
        f"{API_BASE}/search/movie",
        params={"api_key": api_key, "query": title, "include_adult": "false"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None

    def _year(r):
        d = r.get("release_date") or ""
        return int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None

    best = results[0]  # TMDb's default relevance ranking
    if year_hint:
        with_years = [(r, _year(r)) for r in results if _year(r) is not None]
        close = [r for r, y in with_years if abs(y - year_hint) <= 1]
        if close:
            best = close[0]

    return {
        "tmdb_id": best["id"],
        "title": best.get("title") or title,
        "year": _year(best),
        "poster_path": best.get("poster_path"),
    }


def test_api_key(api_key):
    """Raises on an invalid key; returns True on success."""
    resp = requests.get(
        f"{API_BASE}/authentication",
        params={"api_key": api_key},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return True
