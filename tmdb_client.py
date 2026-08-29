"""
Resolves a film title (plus an approximate year, when we have one) to an
exact TMDb ID via TMDb's free search API, and pulls the extra detail
(runtime, overview, rating, IMDb ID) needed for the movie detail page and
for filtering out short films from the automatic Radarr feed.
"""
import requests

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"
TIMEOUT = 15


def poster_url(poster_path, size="w342"):
    return f"{IMAGE_BASE}/{size}{poster_path}" if poster_path else None


def get_movie_details(api_key, tmdb_id):
    """Runtime/overview/rating/imdb_id — only available via the full
    movie-details endpoint, not the search results."""
    resp = requests.get(
        f"{API_BASE}/movie/{tmdb_id}",
        params={"api_key": api_key},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "runtime": data.get("runtime"),
        "overview": data.get("overview"),
        "vote_average": data.get("vote_average"),
        "imdb_id": data.get("imdb_id"),
    }


def search_movie(api_key, title, year_hint=None):
    """
    Return {"tmdb_id", "title", "year", "poster_path", "runtime",
    "overview", "vote_average", "imdb_id"} for the best match, or None if
    TMDb has nothing for this title. TMDb's own results are already
    ranked by relevance/popularity; when we have a year_hint, we prefer
    whichever result's release year is closest to it.
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

    result = {
        "tmdb_id": best["id"],
        "title": best.get("title") or title,
        "year": _year(best),
        "poster_path": best.get("poster_path"),
        "runtime": None,
        "overview": best.get("overview"),
        "vote_average": best.get("vote_average"),
        "imdb_id": None,
    }
    try:
        result.update(get_movie_details(api_key, best["id"]))
    except Exception:
        pass  # search-result fields above are still usable without the detail call
    return result


def test_api_key(api_key):
    """Raises on an invalid key; returns True on success."""
    resp = requests.get(
        f"{API_BASE}/authentication",
        params={"api_key": api_key},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return True
