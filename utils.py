import re

_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_title(text):
    """
    Normalize a film title for matching across sources (Wikipedia vs IMDb
    exports differ in punctuation, articles, casing). Not foolproof — two
    different films that happen to share a bare title will collide — but
    good enough for a personal watched/unwatched cross-reference.
    """
    if not text:
        return ""
    t = text.lower().strip()
    t = _LEADING_ARTICLE.sub("", t)
    t = _NON_ALNUM.sub("", t)
    t = _WHITESPACE.sub(" ", t).strip()
    return t
