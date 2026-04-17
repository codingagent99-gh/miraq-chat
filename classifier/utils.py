"""
classifier/utils.py — Shared regex helpers and text normalization utilities
used across the classifier pipeline.
"""

import re


def normalize_for_tag_compare(s: str) -> set:
    """Normalize a string into a set of lowercase alphanumeric tokens."""
    return set(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split())


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


def create_flexible_pattern(phrase: str) -> str:
    """Create a regex pattern that handles optional plurals for each word."""
    parts = []
    for w in phrase.split():
        if w.endswith('s') and len(w) > 3:
            parts.append(rf'\b{re.escape(w[:-1])}s?\b')
        else:
            parts.append(rf'\b{re.escape(w)}s?\b')
    return r'\s+'.join(parts)