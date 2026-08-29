"""
Fetches award nominations from Wikidata's structured data instead of
parsing Wikipedia's prose tables. Wikidata models this cleanly:

  - a film carries "nominated for" (P1411) / "award received" (P166)
    pointing directly at an award category (e.g. "Academy Award for Best
    Picture") for work-level categories
  - a person carries the same properties for person-level categories
    (Best Actor, Best Director...), with a "for work" (P1686) qualifier
    pointing at the film
  - both carry a "statement is subject of" (P805) qualifier pointing at
    the specific ceremony (e.g. "95th Academy Awards")

So a single SPARQL query per ceremony gets every film nominated for
anything at that ceremony, correctly attributed, with no HTML parsing.
"""
import requests

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {
    "User-Agent": "MovieAwardsTracker/1.0 (personal self-hosted project; contact: none)",
    "Accept": "application/sparql-results+json",
}
TIMEOUT = 30


def search_entity(label):
    """Find a Wikidata item QID by exact (preferred) or best-guess label match."""
    resp = requests.get(
        WIKIDATA_API,
        params={
            "action": "wbsearchentities", "search": label, "language": "en",
            "format": "json", "type": "item", "limit": 5,
        },
        headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json().get("search") or []
    if not results:
        return None
    for r in results:
        if (r.get("label") or "").lower() == label.lower():
            return r["id"]
    return results[0]["id"]


def qid_from_wikipedia_title(title, lang="en"):
    """Look up the Wikidata item linked to a given English Wikipedia article."""
    resp = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "titles": title, "prop": "pageprops", "format": "json"},
        headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    for page in pages.values():
        qid = page.get("pageprops", {}).get("wikibase_item")
        if qid:
            return qid
    return None


_QUERY_TEMPLATE = """
SELECT DISTINCT ?filmLabel ?categoryLabel ?won WHERE {{
  {{
    ?film p:P1411 ?stmt . ?stmt ps:P1411 ?category . BIND(false AS ?won)
    ?stmt pq:P805 wd:{qid} .
    ?film wdt:P31/wdt:P279* wd:Q11424 .
  }} UNION {{
    ?film p:P166 ?stmt . ?stmt ps:P166 ?category . BIND(true AS ?won)
    ?stmt pq:P805 wd:{qid} .
    ?film wdt:P31/wdt:P279* wd:Q11424 .
  }} UNION {{
    ?person p:P1411 ?stmt . ?stmt ps:P1411 ?category . BIND(false AS ?won)
    ?stmt pq:P805 wd:{qid} .
    ?stmt pq:P1686 ?film .
  }} UNION {{
    ?person p:P166 ?stmt . ?stmt ps:P166 ?category . BIND(true AS ?won)
    ?stmt pq:P805 wd:{qid} .
    ?stmt pq:P1686 ?film .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def get_nominations(ceremony_qid):
    """Return [{"film", "category", "is_winner"}] for everything nominated
    at this ceremony, work-level or person-level, deduplicated."""
    query = _QUERY_TEMPLATE.format(qid=ceremony_qid)
    resp = requests.get(
        SPARQL_ENDPOINT, params={"query": query, "format": "json"},
        headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    bindings = resp.json().get("results", {}).get("bindings", [])

    seen = set()
    results = []
    for b in bindings:
        film = (b.get("filmLabel") or {}).get("value")
        category = (b.get("categoryLabel") or {}).get("value")
        won = (b.get("won") or {}).get("value") == "true"
        if not film or not category:
            continue
        key = (film, category, won)
        if key in seen:
            continue
        seen.add(key)
        results.append({"film": film, "category": category, "is_winner": won})
    return results
