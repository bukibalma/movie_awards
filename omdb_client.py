"""
OMDb (omdbapi.com) is a free, documented API that aggregates ratings from
IMDb, Rotten Tomatoes, and Metacritic given an IMDb ID. This is how the
app shows an "IMDb rating" without scraping IMDb directly, which its
robots.txt disallows — same reasoning as everywhere else IMDb comes up in
this project. Free tier: 1,000 requests/day, get a key at
https://www.omdbapi.com/apikey.aspx
"""
import requests

API_BASE = "https://www.omdbapi.com/"
TIMEOUT = 15


def get_ratings(api_key, imdb_id):
    """Return {"imdb_rating", "rotten_tomatoes", "metacritic"} — any of
    which may be None if that source has nothing for this title."""
    resp = requests.get(
        API_BASE, params={"apikey": api_key, "i": imdb_id}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("Response") != "True":
        return None

    ratings_by_source = {r["Source"]: r["Value"] for r in data.get("Ratings", [])}
    imdb_rating = data.get("imdbRating")
    return {
        "imdb_rating": imdb_rating if imdb_rating and imdb_rating != "N/A" else None,
        "rotten_tomatoes": ratings_by_source.get("Rotten Tomatoes"),
        "metacritic": ratings_by_source.get("Metacritic"),
    }


def test_api_key(api_key):
    """Raises on an invalid key; returns True on success."""
    resp = requests.get(API_BASE, params={"apikey": api_key, "i": "tt0111161"}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("Response") == "False" and "key" in (data.get("Error") or "").lower():
        raise ValueError(data.get("Error"))
    return True
