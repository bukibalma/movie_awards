import re
import time
import requests
from bs4 import BeautifulSoup

from config import guess_title

HEADERS = {"User-Agent": "MovieAwardsTracker/1.0 (personal self-hosted project; contact: none)"}
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_BASE = "https://en.wikipedia.org/wiki/"

# A shared, connection-pooled session — reusing one TCP connection across
# requests instead of opening a fresh one each time is both faster and
# considerably more polite to Wikipedia's servers.
_session = requests.Session()
_session.headers.update(HEADERS)


class ScrapeError(Exception):
    pass


def _request(params, retries=3):
    """GET against the MediaWiki API with basic 429/5xx backoff."""
    for attempt in range(retries):
        resp = _session.get(WIKI_API, params=params, timeout=20)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("Retry-After", 5 * (attempt + 1)))
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()  # out of retries — surface whatever the last error was
    return resp


def _page_html(title):
    """Fetch rendered HTML for a Wikipedia page title, or None if missing."""
    resp = _request({
        "action": "parse",
        "page": title,
        "format": "json",
        "prop": "text",
        "redirects": 1,
    })
    data = resp.json()
    if "error" in data:
        return None
    return data["parse"]["text"]["*"], data["parse"]["title"]


def _search_title(query):
    """Fall back to Wikipedia's search API to find the right page."""
    resp = _request({
        "action": "opensearch",
        "search": query,
        "limit": 5,
        "namespace": 0,
        "format": "json",
    })
    data = resp.json()
    titles = data[1] if len(data) > 1 else []
    return titles[0] if titles else None


def resolve_page(award_code, year, award_name):
    """Return (html, title) for the ceremony page, guessing then falling back."""
    guess = guess_title(award_code, year)
    result = _page_html(guess)
    if result:
        return result[0], result[1]

    time.sleep(1)  # a small courtesy pause before the second request
    fallback_query = f"{year} {award_name}"
    found_title = _search_title(fallback_query)
    if not found_title:
        raise ScrapeError(f"Could not find a Wikipedia page for {award_name} {year}")
    time.sleep(1)
    result = _page_html(found_title)
    if not result:
        raise ScrapeError(f"Found title '{found_title}' but could not fetch it")
    return result[0], result[1]


WINNER_HINTS = ("background:#faeb86", "background:#eedd82", "style=\"background:#faeb86")

# Sections that are never actual nominations, no matter the category:
# summary "films with N nominations" tables (a bare number column gets
# read as a film title if not excluded), In Memoriam tributes, cast/crew/
# soundtrack/box-office listings, and meta content like references.
# Deliberately NOT excluding festival "selection"/"in competition" tables —
# being in competition is effectively being nominated for that prize, and
# the goal is "every movie nominated for anything", not just prize winners.
HEADING_DENY = (
    "multiple nomination", "multiple award", "multiple win",
    "in memoriam", "memoriam", "ceremony information", "presenter",
    "performer", "performance", "musical performance",
    "cast", "crew", "soundtrack", "reception", "box office",
    "sidebar", "selection committee", "jury members",
    "see also", "notes", "references", "external links", "further reading",
)

# Column headers that are clearly NOT a nomination category (attribute
# columns on a flat film-listing table) — these get skipped as a whole
# column, since e.g. "Director(s)" holds names, not films.
NON_CATEGORY_HEADERS = {
    "year", "country", "language", "notes", "no.", "id", "ref", "refs",
    "runtime", "director", "director(s)", "producer(s)",
    "production company", "genre", "date",
}

# Column headers that name a title column directly — when present, we take
# the film straight from that column's text rather than guessing from
# italics/position, since it's unambiguous.
TITLE_HEADERS = {"english title", "original title", "title", "film", "films"}


def _nearest_heading(table):
    """Walk backwards to find the closest preceding h2/h3/h4 heading text."""
    for el in table.find_all_previous(["h2", "h3", "h4"]):
        text = el.get_text(" ", strip=True).lower()
        return re.sub(r"\[edit\]", "", text).strip()
    return ""


def _section_allowed(heading):
    return not any(bad in heading for bad in HEADING_DENY)


def _row_is_winner(cell) -> bool:
    style = (cell.get("style") or "").lower()
    cls = " ".join(cell.get("class") or []).lower()
    if "background" in style and any(h.split(":")[1] in style for h in WINNER_HINTS):
        return True
    if "winner" in cls:
        return True
    b = cell.find("b")
    return b is not None


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _looks_like_junk_film(film: str) -> bool:
    if not film:
        return True
    if film.isdigit():           # the "multiple nominations" summary-table bug
        return True
    if len(film) > 150:          # concatenated tribute/performance text, etc.
        return True
    return False


def _films_from_block(block):
    """
    Extract film title(s) from one nomination entry (a <li>, or a whole
    cell with no <li>s). Film titles are conventionally italicized on
    Wikipedia regardless of what the category is — Best Picture, Best
    Actor, Best Original Song, festival selections — so that's the most
    reliable signal, far more so than guessing from word order (which
    flips between "Film – Person" and "Person – Film" depending on the
    category). Falls back to the whole block's text if no italics exist.
    """
    italics = [_clean(i.get_text(" ", strip=True)) for i in block.find_all("i")]
    italics = [t for t in italics if t and not _looks_like_junk_film(t)]
    if italics:
        return italics

    text = _clean(block.get_text(" ", strip=True))
    if not text:
        return []
    # No italics — often means the cell IS just a film title with no
    # separate person credited. If there's a dash, we can't reliably tell
    # which side is the film, so just take the whole text; it's still
    # usually right for single-entry cells (e.g. festival selection rows).
    return [text]


def parse_ceremony_html(html):
    """
    Parser for the 'wikitable' nomination tables used across WikiProject
    Film Awards / festival pages. Pulls every plausible film entry —
    regardless of what it was nominated for — while skipping sections that
    are never nominations at all (cast lists, In Memoriam, summary
    "multiple nominations" tables, etc.). Returns:
      [{"category": str, "nominations": [{"film","nominee","is_winner"}]}]
    Best-effort: Wikipedia formatting varies by page, so results should be
    spot-checked and can be corrected manually in the UI.
    """
    soup = BeautifulSoup(html, "lxml")
    categories = []

    for table in soup.find_all("table", class_=re.compile("wikitable")):
        heading = _nearest_heading(table)
        if not _section_allowed(heading):
            continue

        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [_clean(th.get_text(" ", strip=True)) for th in rows[0].find_all("th")]
        if not headers or not any(headers):
            continue

        data_rows = rows[1:]
        col_cells = {i: [] for i in range(len(headers))}
        for r in data_rows:
            cells = r.find_all(["td", "th"])
            for i, c in enumerate(cells):
                if i in col_cells:
                    col_cells[i].append(c)

        # A flat film-listing table (festival selection lists etc.) has a
        # dedicated title column — use it directly and treat the whole
        # table as one category (the section heading), one film per row,
        # instead of column-as-category (which would misread the
        # Director/Country columns as separate "categories").
        title_col = next(
            (i for i, h in enumerate(headers) if h.strip().lower() in TITLE_HEADERS), None
        )
        if title_col is not None:
            noms = []
            for cell in col_cells.get(title_col, []):
                for film in _films_from_block(cell):
                    noms.append({"film": film, "nominee": None, "is_winner": _row_is_winner(cell)})
            if noms:
                categories.append({"category": heading.title() or "Selection", "nominations": noms})
            continue

        for i, cat_name in enumerate(headers):
            if not cat_name or len(cat_name) > 80:
                continue
            if cat_name.strip().lower() in NON_CATEGORY_HEADERS:
                continue
            noms = []
            for cell in col_cells.get(i, []):
                is_winner = _row_is_winner(cell)
                for li in (cell.find_all("li") or [cell]):
                    for film in _films_from_block(li):
                        noms.append({"film": film, "nominee": None, "is_winner": is_winner})
                        is_winner = False  # only the first entry in a winner cell is the winner
            if noms:
                categories.append({"category": cat_name, "nominations": noms})

    return categories
