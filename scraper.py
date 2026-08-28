import re
import requests
from bs4 import BeautifulSoup

from config import guess_title

HEADERS = {"User-Agent": "MovieAwardsTracker/1.0 (personal self-hosted project)"}
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_BASE = "https://en.wikipedia.org/wiki/"


class ScrapeError(Exception):
    pass


def _page_html(title):
    """Fetch rendered HTML for a Wikipedia page title, or None if missing."""
    resp = requests.get(
        WIKI_API,
        params={
            "action": "parse",
            "page": title,
            "format": "json",
            "prop": "text",
            "redirects": 1,
        },
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        return None
    return data["parse"]["text"]["*"], data["parse"]["title"]


def _search_title(query):
    """Fall back to Wikipedia's search API to find the right page."""
    resp = requests.get(
        WIKI_API,
        params={
            "action": "opensearch",
            "search": query,
            "limit": 5,
            "namespace": 0,
            "format": "json",
        },
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    titles = data[1] if len(data) > 1 else []
    return titles[0] if titles else None


def resolve_page(award_code, year, award_name):
    """Return (html, title) for the ceremony page, guessing then falling back."""
    guess = guess_title(award_code, year)
    result = _page_html(guess)
    if result:
        return result[0], result[1]

    fallback_query = f"{year} {award_name}"
    found_title = _search_title(fallback_query)
    if not found_title:
        raise ScrapeError(f"Could not find a Wikipedia page for {award_name} {year}")
    result = _page_html(found_title)
    if not result:
        raise ScrapeError(f"Found title '{found_title}' but could not fetch it")
    return result[0], result[1]


WINNER_HINTS = ("background:#faeb86", "background:#eedd82", "style=\"background:#faeb86")


def _row_is_winner(cell) -> bool:
    style = (cell.get("style") or "").lower()
    cls = " ".join(cell.get("class") or []).lower()
    if "background" in style and any(h.split(":")[1] in style for h in WINNER_HINTS):
        return True
    if "winner" in cls:
        return True
    # Bold text at the top of the cell is the common convention for the winner
    b = cell.find("b")
    return b is not None


def _cell_entries(cell):
    """
    Extract (film, nominee) pairs from a table cell. Wikipedia award tables
    commonly list entries as either:
      Film Title – Person Name
    or as a nested <ul><li> list of such lines, sometimes with the film
    linked and the person in plain text (or vice versa).
    """
    entries = []
    lis = cell.find_all("li")
    blocks = lis if lis else [cell]
    for block in blocks:
        text = block.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        # Common separators between film and nominee/person
        parts = re.split(r"\s[–—-]\s", text, maxsplit=1)
        if len(parts) == 2:
            film, nominee = parts[0].strip(), parts[1].strip()
        else:
            film, nominee = text, None
        entries.append((film, nominee))
    return entries


def parse_ceremony_html(html):
    """
    Generic parser for the 'wikitable' award-category tables used across
    most WikiProject Film Awards pages. Returns a list of:
      {"category": str, "nominations": [{"film","nominee","is_winner"}]}
    Best-effort: Wikipedia formatting varies by page, so results should be
    spot-checked and can be corrected manually in the UI.
    """
    soup = BeautifulSoup(html, "lxml")
    categories = []

    for table in soup.find_all("table", class_=re.compile("wikitable")):
        header_cells = table.find_all("th")
        if not header_cells:
            continue
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_row = rows[0]
        headers = [th.get_text(" ", strip=True) for th in header_row.find_all("th")]
        if not headers or not any(headers):
            continue

        data_rows = rows[1:]
        col_cells = {i: [] for i in range(len(headers))}
        for r in data_rows:
            cells = r.find_all(["td", "th"])
            for i, c in enumerate(cells):
                if i in col_cells:
                    col_cells[i].append(c)

        for i, cat_name in enumerate(headers):
            if not cat_name or len(cat_name) > 80:
                continue
            noms = []
            for cell in col_cells.get(i, []):
                is_winner = _row_is_winner(cell)
                for film, nominee in _cell_entries(cell):
                    noms.append({"film": film, "nominee": nominee, "is_winner": is_winner})
                    is_winner = False  # only the first entry in a winner cell is the winner
            if noms:
                categories.append({"category": cat_name, "nominations": noms})

    return categories
