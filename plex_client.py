"""
Talks to a Plex Media Server's own API (not plex.tv) to find movies that
have been watched, using the session-history endpoint. Unlike IMDb, this
is a documented, working API — no scraping, no login-wall workarounds.
"""
import requests

HEADERS = {"Accept": "application/json"}
TIMEOUT = 20


def test_connection(base_url, token):
    """Raises on failure; returns the server's friendly name on success."""
    resp = requests.get(
        f"{base_url.rstrip('/')}/identity",
        params={"X-Plex-Token": token},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return True


def get_new_watched_movies(base_url, token, since_epoch=0):
    """
    Return [{"title", "year", "viewed_at"}] for movies watched after
    since_epoch (unix seconds). year may be None — Plex's history entries
    don't always include it; that's fine, matching is by title.
    """
    resp = requests.get(
        f"{base_url.rstrip('/')}/status/sessions/history/all",
        params={
            "X-Plex-Token": token,
            "sort": "viewedAt:desc",
            "viewedAt>": since_epoch,
            "X-Plex-Container-Size": 200,
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("MediaContainer", {}).get("Metadata", [])

    results = []
    for item in items:
        if item.get("type") != "movie":
            continue
        title = item.get("title")
        viewed_at = item.get("viewedAt")
        if not title or not viewed_at:
            continue
        results.append({"title": title, "year": item.get("year"), "viewed_at": viewed_at})
    return results
