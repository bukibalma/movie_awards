"""
Parses whatever the user gives us for their watched list:

- IMDb's own export format (Account -> Your Ratings / a list -> Export),
  which is a CSV with a header row including columns like "Const", "Title",
  "Year". We match those headers case-insensitively and don't care about
  the rest of the columns.
- A plain list of titles, one per line, as a fallback for anyone who'd
  rather just paste titles instead of exporting a CSV.
"""
import csv
import io

TITLE_KEYS = ("title", "original title", "primary title")
YEAR_KEYS = ("year", "release year", "start year")
ID_KEYS = ("const", "imdb id", "tconst", "id")


def parse_watched_text(text):
    """Return a list of {"title", "year", "imdb_id"} dicts."""
    text = (text or "").strip()
    if not text:
        return []

    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        lower_map = {fn.lower().strip(): fn for fn in fieldnames if fn}
        title_field = next((lower_map[k] for k in TITLE_KEYS if k in lower_map), None)
        if title_field:
            year_field = next((lower_map[k] for k in YEAR_KEYS if k in lower_map), None)
            id_field = next((lower_map[k] for k in ID_KEYS if k in lower_map), None)
            results = []
            for row in reader:
                title = (row.get(title_field) or "").strip()
                if not title:
                    continue
                year = None
                if year_field:
                    raw_year = (row.get(year_field) or "").strip()[:4]
                    if raw_year.isdigit():
                        year = int(raw_year)
                imdb_id = (row.get(id_field) or "").strip() if id_field else None
                results.append({"title": title, "year": year, "imdb_id": imdb_id or None})
            if results:
                return results
    except Exception:
        pass  # fall through to plain-text handling

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return [{"title": l, "year": None, "imdb_id": None} for l in lines]


def import_into_db(db_module, text):
    """Parse + insert. Returns (added, already_present, total_rows)."""
    entries = parse_watched_text(text)
    added = 0
    skipped = 0
    for e in entries:
        if db_module.add_watched(e["title"], e["year"], e["imdb_id"]):
            added += 1
        else:
            skipped += 1
    return added, skipped, len(entries)
