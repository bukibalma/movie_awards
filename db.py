import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "/data/awards.db")


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS ceremonies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    award_code TEXT NOT NULL,
    year INTEGER NOT NULL,
    wiki_title TEXT,
    wiki_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | scraped | failed | manual
    scraped_at TEXT,
    last_attempt_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE(award_code, year)
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ceremony_id INTEGER NOT NULL REFERENCES ceremonies(id) ON DELETE CASCADE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    film TEXT NOT NULL,
    nominee TEXT,
    is_winner INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watched_films (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id TEXT,
    title TEXT NOT NULL,
    year INTEGER,
    normalized_title TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'import'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tmdb_matches (
    normalized_title TEXT PRIMARY KEY,
    tmdb_id INTEGER,
    year INTEGER,
    matched_title TEXT,
    poster_path TEXT,
    runtime INTEGER,
    overview TEXT,
    vote_average REAL,
    imdb_id TEXT,
    resolved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS film_flags (
    normalized_title TEXT PRIMARY KEY,
    radarr_manual INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS omdb_ratings (
    imdb_id TEXT PRIMARY KEY,
    imdb_rating TEXT,
    rotten_tomatoes TEXT,
    metacritic TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Migration guard for DBs created before last_attempt_at/attempts existed.
    for stmt in (
        "ALTER TABLE ceremonies ADD COLUMN last_attempt_at TEXT",
        "ALTER TABLE ceremonies ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE watched_films ADD COLUMN source TEXT NOT NULL DEFAULT 'import'",
        "ALTER TABLE tmdb_matches ADD COLUMN poster_path TEXT",
        "ALTER TABLE tmdb_matches ADD COLUMN runtime INTEGER",
        "ALTER TABLE tmdb_matches ADD COLUMN overview TEXT",
        "ALTER TABLE tmdb_matches ADD COLUMN vote_average REAL",
        "ALTER TABLE tmdb_matches ADD COLUMN imdb_id TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


def get_or_create_ceremony(award_code, year, wiki_title=None, wiki_url=None):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM ceremonies WHERE award_code=? AND year=?",
        (award_code, year),
    ).fetchone()
    if row:
        conn.close()
        return dict(row)
    cur = conn.execute(
        "INSERT INTO ceremonies (award_code, year, wiki_title, wiki_url) VALUES (?,?,?,?)",
        (award_code, year, wiki_title, wiki_url),
    )
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute("SELECT * FROM ceremonies WHERE id=?", (new_id,)).fetchone()
    conn.close()
    return dict(row)


def list_ceremonies(award_code=None):
    conn = get_conn()
    if award_code:
        rows = conn.execute(
            "SELECT * FROM ceremonies WHERE award_code=? ORDER BY year DESC", (award_code,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM ceremonies ORDER BY award_code, year DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ceremony(ceremony_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM ceremonies WHERE id=?", (ceremony_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_ceremony_status(ceremony_id, status, wiki_title=None, wiki_url=None, count_attempt=True):
    conn = get_conn()
    conn.execute(
        """UPDATE ceremonies SET status=?,
           wiki_title=COALESCE(?, wiki_title),
           wiki_url=COALESCE(?, wiki_url),
           scraped_at=CASE WHEN ?='scraped' THEN datetime('now') ELSE scraped_at END,
           last_attempt_at=datetime('now'),
           attempts=attempts + ?
           WHERE id=?""",
        (status, wiki_title, wiki_url, status, 1 if count_attempt else 0, ceremony_id),
    )
    conn.commit()
    conn.close()


def ceremonies_due_for_scrape(recent_years, max_year, stale_attempt_cap=60):
    """
    Ceremonies the scheduler should (re)try this run:
      - any never-successfully-scraped ceremony (pending/failed) at or below
        max_year, up to stale_attempt_cap tries (so long-dead future years
        with no page yet don't get hammered forever)
      - any ceremony within `recent_years` of today, even if already
        'scraped', so winner updates get picked up after the ceremony airs
    'manual' ceremonies are left alone — the user curated those by hand.
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM ceremonies
           WHERE status != 'manual' AND (
             (status IN ('pending','failed') AND year <= ? AND attempts < ?)
             OR (year >= ? AND year <= ?)
           )
           ORDER BY year DESC""",
        (max_year, stale_attempt_cap, recent_years[0], recent_years[1]),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_categories(ceremony_id):
    conn = get_conn()
    conn.execute("DELETE FROM categories WHERE ceremony_id=?", (ceremony_id,))
    conn.commit()
    conn.close()


def add_category(ceremony_id, name):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO categories (ceremony_id, name) VALUES (?,?)", (ceremony_id, name)
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def add_nomination(category_id, film, nominee, is_winner):
    conn = get_conn()
    conn.execute(
        "INSERT INTO nominations (category_id, film, nominee, is_winner) VALUES (?,?,?,?)",
        (category_id, film, nominee, 1 if is_winner else 0),
    )
    conn.commit()
    conn.close()


def get_categories_with_nominations(ceremony_id):
    conn = get_conn()
    cats = conn.execute(
        "SELECT * FROM categories WHERE ceremony_id=? ORDER BY id", (ceremony_id,)
    ).fetchall()
    result = []
    for cat in cats:
        noms = conn.execute(
            "SELECT * FROM nominations WHERE category_id=? ORDER BY is_winner DESC, id",
            (cat["id"],),
        ).fetchall()
        result.append({"category": dict(cat), "nominations": [dict(n) for n in noms]})
    conn.close()
    return result


def search_nominations(query):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT n.film, n.nominee, n.is_winner, c.name AS category,
               ce.award_code, ce.year
        FROM nominations n
        JOIN categories c ON n.category_id = c.id
        JOIN ceremonies ce ON c.ceremony_id = ce.id
        WHERE n.film LIKE ?
        ORDER BY ce.year DESC, ce.award_code
        """,
        (f"%{query}%",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_nominations_with_context():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT n.film, n.is_winner, c.name AS category, ce.award_code, ce.year
        FROM nominations n
        JOIN categories c ON n.category_id = c.id
        JOIN ceremonies ce ON c.ceremony_id = ce.id
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- watched list ----

def add_watched(title, year=None, imdb_id=None, source="import"):
    """Insert a watched film. Returns True if newly added, False if a film
    with the same normalized title was already recorded."""
    from utils import normalize_title

    norm = normalize_title(title)
    if not norm:
        return False
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO watched_films (imdb_id, title, year, normalized_title, source) VALUES (?,?,?,?,?)",
            (imdb_id, title, year, norm, source),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def list_watched():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM watched_films ORDER BY title").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def watched_normalized_titles():
    conn = get_conn()
    rows = conn.execute("SELECT normalized_title FROM watched_films").fetchall()
    conn.close()
    return {r[0] for r in rows}


def delete_watched(watched_id):
    conn = get_conn()
    conn.execute("DELETE FROM watched_films WHERE id=?", (watched_id,))
    conn.commit()
    conn.close()


def delete_watched_by_normalized_title(normalized_title):
    conn = get_conn()
    conn.execute("DELETE FROM watched_films WHERE normalized_title=?", (normalized_title,))
    conn.commit()
    conn.close()


def clear_watched():
    conn = get_conn()
    conn.execute("DELETE FROM watched_films")
    conn.commit()
    conn.close()


# ---- settings (key/value) ----

def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


# ---- TMDb match cache ----

def get_tmdb_match(normalized_title):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM tmdb_matches WHERE normalized_title=?", (normalized_title,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_tmdb_match_by_id(tmdb_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM tmdb_matches WHERE tmdb_id=?", (tmdb_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_tmdb_match(normalized_title, tmdb_id, year, matched_title, poster_path=None,
                    runtime=None, overview=None, vote_average=None, imdb_id=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO tmdb_matches (normalized_title, tmdb_id, year, matched_title, "
        "poster_path, runtime, overview, vote_average, imdb_id, resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?,?, datetime('now')) "
        "ON CONFLICT(normalized_title) DO UPDATE SET "
        "tmdb_id=excluded.tmdb_id, year=excluded.year, "
        "matched_title=excluded.matched_title, poster_path=excluded.poster_path, "
        "runtime=excluded.runtime, overview=excluded.overview, "
        "vote_average=excluded.vote_average, imdb_id=excluded.imdb_id, "
        "resolved_at=excluded.resolved_at",
        (normalized_title, tmdb_id, year, matched_title, poster_path,
         runtime, overview, vote_average, imdb_id),
    )
    conn.commit()
    conn.close()


# ---- manual Radarr-list flag ----

def get_radarr_manual(normalized_title):
    conn = get_conn()
    row = conn.execute(
        "SELECT radarr_manual FROM film_flags WHERE normalized_title=?", (normalized_title,)
    ).fetchone()
    conn.close()
    return bool(row and row["radarr_manual"])


def set_radarr_manual(normalized_title, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO film_flags (normalized_title, radarr_manual) VALUES (?,?) "
        "ON CONFLICT(normalized_title) DO UPDATE SET radarr_manual=excluded.radarr_manual",
        (normalized_title, 1 if value else 0),
    )
    conn.commit()
    conn.close()


def list_radarr_manual_titles():
    conn = get_conn()
    rows = conn.execute(
        "SELECT normalized_title FROM film_flags WHERE radarr_manual=1"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


# ---- OMDb rating cache ----

def get_omdb_ratings(imdb_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM omdb_ratings WHERE imdb_id=?", (imdb_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_omdb_ratings(imdb_id, imdb_rating, rotten_tomatoes, metacritic):
    conn = get_conn()
    conn.execute(
        "INSERT INTO omdb_ratings (imdb_id, imdb_rating, rotten_tomatoes, metacritic, fetched_at) "
        "VALUES (?,?,?,?, datetime('now')) "
        "ON CONFLICT(imdb_id) DO UPDATE SET "
        "imdb_rating=excluded.imdb_rating, rotten_tomatoes=excluded.rotten_tomatoes, "
        "metacritic=excluded.metacritic, fetched_at=excluded.fetched_at",
        (imdb_id, imdb_rating, rotten_tomatoes, metacritic),
    )
    conn.commit()
    conn.close()
