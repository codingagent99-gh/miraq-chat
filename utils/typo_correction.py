"""
utils/typo_correction.py — Pre-classification typo correction for chat messages.

Corrects misspelled catalog terms (categories, tags, attribute values, product
name words) in the user's message BEFORE Phase 1 catalog matching runs, so the
entire existing pipeline (longest-match, collision handling, OR pairs,
classifier regexes) operates on corrected text exactly as if the user had
typed it correctly.

Design principles (see chat for full rationale):
  - Two-dictionary guard: a token already in the protected set (stop words,
    noise words, synonym keys, catalog vocabulary) is NEVER touched. OOV
    tokens are corrected against catalog vocab AND protected words together,
    so a misspelled glue word ("shwo") corrects to "show" — which downstream
    stop-word stripping then removes — instead of being force-fitted to the
    nearest catalog term ("shower").
  - Damerau-Levenshtein distance (transposition = 1 edit — the most common
    typo type), with length-scaled budgets (Elasticsearch AUTO rule):
    len < 4 → never corrected; len 4-6 → 1 edit; len 7+ → 2 edits.
  - Ambiguity refusal: if two candidates tie at the same distance, do NOT
    guess — leave the token alone for Phase 3 semantic search / the
    clarification-chip flow to handle.
  - KNOWN_QUERY_TYPO_CORRECTIONS (store_config.py) is applied FIRST as a
    manual override — it exists for real-word typos ("quick chip" → "quick
    ship") that edit distance structurally cannot catch, because the typo is
    itself a valid word.

Skipped tokens: protected words, tokens containing digits (dimensions like
12x24, order numbers, quantities), tokens containing @ (emails), tokens
shorter than 4 characters.
"""

import re
from typing import Optional

from rapidfuzz import process
from rapidfuzz.distance import DamerauLevenshtein

from chat_logger import get_logger
from config.store_config import KNOWN_QUERY_TYPO_CORRECTIONS

logger = get_logger("miraq_chat")

# Tokens matching any of these are never candidates for correction.
_HAS_DIGIT_RE = re.compile(r"\d")
_TOKEN_SPLIT_RE = re.compile(r"(\W+)")  # keep separators so text reassembles exactly

_MIN_CORRECTABLE_LEN = 4


def _max_edits_for(token: str) -> int:
    """Length-scaled edit budget (Elasticsearch AUTO fuzziness rule)."""
    n = len(token)
    if n < _MIN_CORRECTABLE_LEN:
        return 0
    if n <= 6:
        return 1
    return 2


def _correct_token(token: str, loader) -> Optional[tuple]:
    """
    Return (corrected_term, distance, vocab_type) for one OOV token,
    or None if no unambiguous correction within the edit budget exists.
    vocab_type is "protected" when the winner is a glue/stop word (the
    caller substitutes it and lets normal stop-word stripping remove it),
    else the catalog type ("category" | "tag" | "attribute" | "product_word").
    """
    max_edits = _max_edits_for(token)
    if max_edits == 0:
        return None

    # extract() over the combined search space; we need the top TWO to
    # detect ties, which extractOne can't reveal.
    matches = process.extract(
        token,
        loader.fuzzy_vocab_terms,
        scorer=DamerauLevenshtein.distance,
        score_cutoff=max_edits,
        limit=2,
    )
    if not matches:
        return None

    best_term, best_dist, _ = matches[0]

    # Ambiguity refusal: a second candidate at the SAME distance means we
    # can't know which the user meant — don't guess, let Phase 3 /
    # clarification chips handle it.
    if len(matches) > 1 and matches[1][1] == best_dist:
        logger.debug(
            f"[TypoFix] ambiguous — not correcting | token={token!r} | "
            f"tied candidates={[m[0] for m in matches]} at distance {best_dist}"
        )
        return None

    vocab_type = loader.fuzzy_vocab_types.get(best_term, "protected")
    return best_term, best_dist, vocab_type


def correct_message(message: str, loader) -> tuple[str, list]:
    """
    Correct misspelled catalog/glue words in `message`.

    Returns (corrected_message, corrections) where corrections is a list of
    {"original", "corrected", "distance", "type"} dicts — empty when nothing
    was changed. The original message is returned untouched when the loader
    is missing or has no fuzzy vocabulary (e.g. degraded/still-warming
    tenant), so this is always safe to call.
    """
    if not message or loader is None or not getattr(loader, "fuzzy_vocab_terms", None):
        return message, []

    corrections: list = []
    working = message

    # ── Manual override pass (real-word typos edit distance can't see) ──
    _lower = working.lower()
    for typo, corr in KNOWN_QUERY_TYPO_CORRECTIONS.items():
        if typo in _lower:
            working = re.sub(re.escape(typo), corr, working, flags=re.IGNORECASE)
            _lower = working.lower()
            corrections.append(
                {"original": typo, "corrected": corr, "distance": None, "type": "manual_override"}
            )

    # ── Token-level fuzzy pass ──
    parts = _TOKEN_SPLIT_RE.split(working)
    changed = False
    for i, part in enumerate(parts):
        token = part.lower()
        if (
            not token
            or not token.isalpha()          # skips digits, mixed, @, punctuation-glued
            or len(token) < _MIN_CORRECTABLE_LEN
            or token in loader.fuzzy_protected_words
            # Plural/singular of a protected word is NOT a typo — Phase 1's
            # matcher is already plural-tolerant; "tiles" must not become "tile".
            or (token.endswith("s") and token[:-1] in loader.fuzzy_protected_words)
            or (token + "s") in loader.fuzzy_protected_words
        ):
            continue

        result = _correct_token(token, loader)
        if result is None:
            continue

        corrected_term, dist, vocab_type = result
        parts[i] = corrected_term
        changed = True
        corrections.append(
            {"original": token, "corrected": corrected_term, "distance": dist, "type": vocab_type}
        )

    corrected_message = "".join(parts) if changed else working

    if corrections:
        logger.info(
            f"[TypoFix] applied {len(corrections)} correction(s) | "
            + " ; ".join(f"{c['original']!r}→{c['corrected']!r}({c['type']})" for c in corrections)
        )

    return corrected_message, corrections