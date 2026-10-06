"""
attr_values.py — One normalisation for variant attribute VALUES, used on both
sides of every "does this variant match what the shopper asked for" check.

Why: the shopper's side and the variant's side used to be cleaned differently.
The shopper's value had quotes stripped ('12"x71"' -> '12x71') while the
variant's option kept them ('12"x71"'), so '12x71' == '12"x71"' was never
true and any inch-marked size reported "no variation" even when the variant
existed. WooCommerce variations often carry the slug ('12x71') so it went
unnoticed there; Shopify variants carry the option name ('12"x71"').
"""

import re

# Straight and typographic quotes, primes (inch/foot marks) and backticks.
_QUOTE_CHARS = re.compile(r"[\"'`\u2018\u2019\u201c\u201d\u2032\u2033]")
# "12 x 71", "12X71", "12×71" -> "12x71" (only between digits).
_DIM_SEPARATOR = re.compile(r"(?<=\d)\s*[x\u00d7]\s*(?=\d)", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


def normalize_attr_value(value) -> str:
    """Lowercase, quote/inch-mark-free, hyphens as spaces, '12 x 71' -> '12x71'."""
    s = _QUOTE_CHARS.sub("", str(value or "")).lower().replace("-", " ")
    s = _DIM_SEPARATOR.sub("x", s)
    return _SPACES.sub(" ", s).strip()