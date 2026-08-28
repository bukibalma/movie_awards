"""
Configuration for every award tracked by the app.

Two kinds of Wikipedia page naming conventions are used by editors:

  - "ordinal"  -> pages like "98th Academy Awards". We compute a guessed
                  ordinal from a known (base_year -> base_ordinal) anchor,
                  then let the scraper confirm/correct it via Wikipedia's
                  search API if the guess doesn't resolve.
  - "year"     -> pages like "2026 Cannes Film Festival". No ordinal needed.

base_year/base_ordinal are reference points used only to *guess* a title.
The scraper always verifies the guess and falls back to a Wikipedia
search if it's wrong, so an off-by-one anchor here just costs one extra
lookup, not incorrect data.
"""

AWARDS = {
    "oscars": {
        "name": "Academy Awards (Oscars)",
        "kind": "ordinal",
        "title_template": "{ordinal} Academy Awards",
        "base_year": 2026,
        "base_ordinal": 98,
    },
    "golden_globes": {
        "name": "Golden Globe Awards",
        "kind": "ordinal",
        "title_template": "{ordinal} Golden Globe Awards",
        "base_year": 2026,
        "base_ordinal": 83,
    },
    "bafta": {
        "name": "BAFTA Film Awards",
        "kind": "ordinal",
        "title_template": "{ordinal} British Academy Film Awards",
        "base_year": 2026,
        "base_ordinal": 79,
    },
    "cannes": {
        "name": "Cannes Film Festival",
        "kind": "year",
        "title_template": "{year} Cannes Film Festival",
        "festival": True,
    },
    "berlinale": {
        "name": "Berlin International Film Festival (Berlinale)",
        "kind": "ordinal",
        "title_template": "{ordinal} Berlin International Film Festival",
        "base_year": 2026,
        "base_ordinal": 76,
        "festival": True,
    },
    "venice": {
        "name": "Venice International Film Festival",
        "kind": "ordinal",
        "title_template": "{ordinal} Venice International Film Festival",
        "base_year": 2026,
        "base_ordinal": 83,
        "festival": True,
    },
    "sundance": {
        "name": "Sundance Film Festival",
        "kind": "year",
        "title_template": "{year} Sundance Film Festival",
        "festival": True,
    },
    "spirit": {
        "name": "Independent Spirit Awards",
        "kind": "ordinal",
        "title_template": "{ordinal} Independent Spirit Awards",
        "base_year": 2026,
        "base_ordinal": 41,
    },
    "cesar": {
        "name": "César Awards",
        "kind": "ordinal",
        "title_template": "{ordinal} César Awards",
        "base_year": 2026,
        "base_ordinal": 51,
    },
    "choice": {
        "name": "Critics' Choice Awards",
        "kind": "ordinal",
        "title_template": "{ordinal} Critics' Choice Awards",
        "base_year": 2026,
        "base_ordinal": 31,
    },
}


def ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def guess_title(award_code: str, year: int) -> str:
    """Best-guess Wikipedia page title for an award's ceremony year."""
    cfg = AWARDS[award_code]
    if cfg["kind"] == "year":
        return cfg["title_template"].format(year=year)
    ordinal_num = cfg["base_ordinal"] + (year - cfg["base_year"])
    return cfg["title_template"].format(ordinal=ordinal_suffix(ordinal_num))
