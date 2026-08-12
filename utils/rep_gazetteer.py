"""
utils/rep_gazetteer.py — resolve a sales rep from free text by matching
against the KNOWN rep roster, rather than parsing sentence structure.

Why this exists
---------------
The first implementation extracted the rep name with regexes anchored to
specific phrasings ("ordered by X", "did X order"). Two problems:

  1. Phrasing. "Jen Bullock's sample count for Q3" matches none of those
     patterns, so no name was found at all.

  2. Typo corruption. Correction runs BEFORE classification and rewrites
     tokens toward catalog vocabulary — "Bullock" became "Block" (a
     mosaic-type attribute), so the lookup searched for a person who does
     not exist. Protecting names positionally only works for the phrasings
     the regex already knows about, which is the same hole as (1).

Reps are a closed, known set — a couple of dozen names. So this matches the
message against the roster the way get_product_for_text matches against the
product catalog. Phrasing stops mattering, and matching the RAW message
sidesteps typo corruption entirely rather than trying to prevent it.

The roster is fetched from GET /reps and cached per process.
"""

import re
import time
import difflib
from typing import Optional, List, Dict

from woo_client import woo_client
from ecommerce import endpoints
from chat_logger import get_logger

logger = get_logger("miraq_chat")

_CACHE: dict = {"reps": None, "fetched_at": 0.0}
_CACHE_TTL_SECONDS = 600

# Words that must never resolve to a rep on their own. Common first names are
# fine — the roster is small enough that a bare "kelly" is almost certainly
# that rep — but query scaffolding is not.
_NEVER_MATCH = frozenset({
    "a", "all", "an", "and", "any", "anyone", "by", "did", "each", "every",
    "for", "from", "how", "i", "in", "many", "me", "my", "of", "order",
    "ordered", "orders", "our", "placed", "rep", "reps", "sample", "samples",
    "the", "their", "them", "they", "to", "us", "we", "who", "whom", "you",
})

# Minimum token length for a fuzzy (non-exact) match. Short tokens produce
# false positives against short surnames.
_MIN_FUZZY_LEN = 4


def _norm(value) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9@. ]+", " ", str(value or "").lower())).strip()


def _norm_name(value) -> str:
    """Normalisation for NAMES — also drops dots, so 'Ram R.' == 'Ram R'."""
    return re.sub(r"\s+", " ", _norm(value).replace(".", " ")).strip()


def load_reps(force: bool = False) -> List[Dict]:
    """Rep roster from GET /reps, cached per process.

    Returns [] on failure — callers must treat that as "cannot resolve",
    never as "no such rep", so a transient API error is not reported to the
    user as a missing person.
    """
    now = time.time()
    if (
        not force
        and _CACHE["reps"] is not None
        and (now - _CACHE["fetched_at"]) < _CACHE_TTL_SECONDS
    ):
        return _CACHE["reps"]

    try:
        call = endpoints.fetch_reps(description="Rep roster for name resolution")
        result = woo_client.execute(call)
        data = result.get("data")
        if not result.get("success") or not isinstance(data, list):
            logger.warning(f"rep_gazetteer | roster fetch failed: {result.get('error')}")
            return _CACHE["reps"] or []
    except Exception as exc:
        logger.warning(f"rep_gazetteer | roster fetch raised: {exc}")
        return _CACHE["reps"] or []

    reps = []
    for row in data:
        if not isinstance(row, dict):
            continue
        email = (row.get("value") or "").strip()
        label = (row.get("label") or "").strip()
        if not email:
            continue

        # /reps returns the REAL name in `label` (first_name + last_name),
        # plus `username` and `display_name` separately — display_name is
        # often just the username on this site, so it cannot be trusted as
        # the person's name.
        username     = (row.get("username") or "").strip()
        display_name = (row.get("display_name") or "").strip()
        first        = (row.get("first_name") or "").strip()
        last         = (row.get("last_name") or "").strip()

        full = _norm_name(label)
        aliases = set()
        if full:
            aliases.add(full)

        # First and last names as separate handles ("bullock", "jennifer").
        for part in (_norm_name(first), _norm_name(last)):
            if part:
                aliases.add(part)
        # Fall back to splitting the label when the meta fields are empty.
        if not first and not last:
            for part in full.split():
                aliases.add(part)

        # Username — people refer to reps by login as often as by name.
        if username:
            aliases.add(_norm_name(username))
        # display_name too, when it differs from the real name.
        if display_name:
            aliases.add(_norm_name(display_name))
        # Email local part, e.g. "kellyf" from kellyf@…
        local = _norm(email.split("@")[0])
        if local:
            aliases.add(local)

        reps.append({
            "email": email,
            "label": label or email,
            "full": full,
            "aliases": {a for a in aliases if a and a not in _NEVER_MATCH},
        })

    _CACHE["reps"] = reps
    _CACHE["fetched_at"] = now
    logger.info(f"rep_gazetteer | loaded {len(reps)} rep(s)")
    return reps


def _match(text: str, reps: List[Dict]) -> List[Dict]:
    """Score `text` against `reps`. Split out so a cache miss can retry."""
    hay = _norm_name(text)
    if not hay:
        return []
    tokens = [t for t in hay.split() if t]

    scored = []
    for rep in reps:
        best = 0.0

        # 1. Full name appears verbatim — strongest signal.
        if rep["full"] and re.search(rf"\b{re.escape(rep['full'])}\b", hay):
            best = 1.0
        else:
            for alias in rep["aliases"]:
                # 2. Alias appears as a whole word.
                if re.search(rf"\b{re.escape(alias)}\b", hay):
                    best = max(best, 0.9)
                    continue
                # 3. Fuzzy — catches typos and truncations ("bullok").
                if len(alias) >= _MIN_FUZZY_LEN:
                    for tok in tokens:
                        if len(tok) < _MIN_FUZZY_LEN or tok in _NEVER_MATCH:
                            continue
                        ratio = difflib.SequenceMatcher(None, alias, tok).ratio()
                        if ratio >= 0.85:
                            best = max(best, ratio * 0.8)

        if best > 0:
            scored.append((best, rep))

    if not scored:
        return []

    scored.sort(key=lambda pair: (-pair[0], pair[1]["label"]))
    top = scored[0][0]
    # Keep only rivals of comparable strength, so an exact hit is not shown
    # alongside weak fuzzy noise.
    return [rep for score, rep in scored if score >= top - 0.05]


def find_reps_in_text(text: str) -> List[Dict]:
    """Reps mentioned anywhere in `text`, best match first.

    Pass the RAW message (pre-typo-correction) so catalog vocabulary cannot
    have rewritten the name before this runs.

    Returns [] when nothing matches AND when the roster is unavailable — the
    caller distinguishes those via load_reps().
    """
    reps = load_reps()
    if not reps or not text:
        return []

    out = _match(text, reps)

    # A miss may just mean the roster is stale — a rep added since the last
    # fetch is invisible until the TTL expires. Refetch once and retry rather
    # than telling the user a real rep does not exist. Only on a miss, so the
    # cache still absorbs the common case.
    if not out:
        refreshed = load_reps(force=True)
        if len(refreshed) != len(reps):
            logger.info(
                f"rep_gazetteer | roster changed on refetch "
                f"({len(reps)} -> {len(refreshed)}); retrying match"
            )
            out = _match(text, refreshed)

    if not out:
        return []

    logger.info(
        f"rep_gazetteer | matched {len(out)} rep(s) in text "
        f"(top={out[0]['label']!r})"
    )
    return out