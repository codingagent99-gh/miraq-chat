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


def _clean_attr_name(name) -> str:
    s = str(name or "").strip().lower()
    for p in ("attribute_pa_", "attribute_", "pa_"):
        s = s.removeprefix(p)
    return _SPACES.sub(" ", s.replace("-", " ").replace("_", " ")).strip()


def attr_name_matches(requested, variant_name) -> bool:
    """Does the shopper's attribute (``requested``) refer to the variant's
    attribute (``variant_name``)?

    Shopify: only the SAME name. Color, Colors and Colous are three different
    options set by the merchant, and a value resolved under one must not be
    tested against another. The old substring test ("color" in "colors")
    paired them, so a stray Color value was checked against Allspice's Colors
    option and failed every variant.

    WooCommerce: unchanged loose match (either name contains the other) — its
    attribute label and slug can differ (label "Colour", slug "pa_color").
    """
    a, b = _clean_attr_name(requested), _clean_attr_name(variant_name)
    if not a or not b:
        return False
    from platform_config import current_backend
    if current_backend() == "shopify":
        return a == b
    return a in b or b in a

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def is_size_attr(name) -> bool:
    """True for size-style attributes (config FUZZY_SIZE_ATTRIBUTE_KEYS:
    sample-size, tile-size by default) — the only attributes where the
    catalog already treats spacing/punctuation as insignificant."""
    from config.store_config import FUZZY_SIZE_ATTRIBUTE_KEYS
    return _clean_attr_name(name).replace(" ", "-") in FUZZY_SIZE_ATTRIBUTE_KEYS


def attr_values_equal(attr_name, requested, actual) -> bool:
    """Exact value match, after normalize_attr_value on both sides.

    Size attributes also match when they differ only in spacing/punctuation:
    the merchant spells the same chip-card sample "Chip Card" on one product
    and "Chipcard" on another. StoreLoader.resolve_attribute_term() and the
    cart-side matcher (_variation_matches_resolved_neutral) already ignore
    that difference for sizes, so the classifier hands over 'chip-card' for a
    message saying "Chipcard" — and the variant matcher, comparing
    'chip card' to 'chipcard' literally, reported "no variation satisfies
    ['sample-size']" on Zelda Mosaic. Same scope as the resolver: sizes only,
    because elsewhere punctuation can matter ("2.0" vs "20" in colour names).
    """
    a, b = normalize_attr_value(requested), normalize_attr_value(actual)
    if a == b:
        return True
    if a and b and is_size_attr(attr_name):
        return _NON_ALNUM.sub("", a) == _NON_ALNUM.sub("", b)
    return False