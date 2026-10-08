"""
classifier/utils.py — Shared regex helpers and text normalization utilities
used across the classifier pipeline.
"""

import re
from typing import Optional


def normalize_for_tag_compare(s: str) -> set:
    """Normalize a string into a set of lowercase alphanumeric tokens."""
    return set(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split())

def _singularize(token: str) -> str:
    """Cheap plural fold so 'mosaics' and 'mosaic' compare equal. Categories
    are conventionally plural in this catalog while tags/attribute values
    are conventionally singular — normalize_for_tag_compare alone doesn't
    account for this, so token-set comparisons across category names vs
    tag/attribute values silently miss matches without this fold."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokens_overlap_loose(tokens_a: set, tokens_b: set) -> bool:
    """Plural-tolerant overlap check between two token sets produced by
    normalize_for_tag_compare."""
    a = {_singularize(t) for t in tokens_a}
    b = {_singularize(t) for t in tokens_b}
    return bool(a & b)

def normalize_dimension(val: str) -> str:
    """Strip quotes, spaces, and unit strings to get the raw dimensional number."""
    clean = re.sub(r'["\'\s]', '', val.lower())
    clean = re.sub(r'(mm|cm|inch|inches|in\.?|thick|weight|lbs?|oz|kg|g)$', '', clean)
    return clean


def label_word_matches(word: str, text: str) -> bool:
    """Check if a word (with plural tolerance) appears in text."""
    w = re.escape(word)
    if re.search(rf"\b{w}s?\b", text) or re.search(rf"\b{w}es?\b", text):
        return True
    if word.endswith("s") and len(word) > 3:
        if re.search(rf"\b{re.escape(word[:-1])}\b", text):
            return True
    return False


_KIND_TAG_WORD = r'(?:tags?|tagged)'
_KIND_CAT_WORD = r'(?:collections?|categor(?:y|ies))'


def explicit_kind_near(text: str, start: int, end: int) -> Optional[str]:
    """What the shopper called the name at text[start:end], if they said.

    Returns "tag" for "interior tag" / "tagged interior", "category" for
    "wall collection" / "wall category", otherwise None. Only the words right
    next to the name count, so "wall collection with interior tag" gives
    "category" for wall and "tag" for interior. Used where one name is both a
    tag and a category/collection in the store.
    """
    after = text[end:]
    before = text[:start]
    if re.match(rf'\s*{_KIND_TAG_WORD}\b', after, re.IGNORECASE):
        return "tag"
    if re.match(rf'\s*{_KIND_CAT_WORD}\b', after, re.IGNORECASE):
        return "category"
    if re.search(rf'\b{_KIND_TAG_WORD}(?:\s+(?:as|with))?\s*$', before, re.IGNORECASE):
        return "tag"
    if re.search(rf'\b{_KIND_CAT_WORD}\s*$', before, re.IGNORECASE):
        return "category"
    return None


def create_flexible_pattern(phrase: str) -> str:
    """Create a regex pattern that handles optional plurals for each word."""
    parts = []
    for w in phrase.split():
        if w.endswith('s') and len(w) > 3:
            parts.append(rf'\b{re.escape(w[:-1])}s?\b')
        else:
            parts.append(rf'\b{re.escape(w)}s?\b')
    return r'\s+'.join(parts)