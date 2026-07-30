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
    len < 4 → never corrected; len 4-6 → 2 edits; len 7+ → 3 edits.
  - Ambiguity escalation: if two-or-more CATALOG terms tie at the same
    distance, do NOT guess — the token is left as-is in corrected_message
    and reported via the `ambiguities` return value so the caller
    (handlers/typo_clarification_handler.py) can ask "did you mean X or
    Y?" as a clarification chip and splice the answer back in. Ties among
    protected/glue words only are still silently skipped — not worth
    interrupting the user for.
  - KNOWN_QUERY_TYPO_CORRECTIONS (store_config.py) is applied FIRST as a
    manual override — it exists for real-word typos ("quick chip" → "quick
    ship") that edit distance structurally cannot catch, because the typo is
    itself a valid word.

Skipped tokens: protected words, tokens containing digits (dimensions like
12x24, order numbers, quantities), tokens containing @ (emails), tokens
shorter than 4 characters.
"""

import re
from collections import namedtuple
from typing import Optional, Union

from rapidfuzz import process
from rapidfuzz.distance import DamerauLevenshtein

from chat_logger import get_logger
from config.store_config import KNOWN_QUERY_TYPO_CORRECTIONS

logger = get_logger("miraq_chat")

# Tokens matching any of these are never candidates for correction.
_HAS_DIGIT_RE = re.compile(r"\d")
_TOKEN_SPLIT_RE = re.compile(r"(\W+)")  # keep separators so text reassembles exactly

_MIN_CORRECTABLE_LEN = 4

# A single unambiguous winner — applied immediately, no user involved.
_Correction = namedtuple("_Correction", "term distance vocab_type")

# Two or more CATALOG terms (category/tag/attribute/product_word) tied at the
# same distance. Ties that only involve protected/glue words are NOT surfaced
# here — asking "did you mean 'show' or 'shoe'?" for a stop word isn't worth
# interrupting the user for, so those still resolve to a silent no-op.
_Ambiguity = namedtuple("_Ambiguity", "candidates distance")


def _max_edits_for(token: str) -> int:
    """Length-scaled edit budget (Elasticsearch AUTO fuzziness rule)."""
    n = len(token)
    if n < _MIN_CORRECTABLE_LEN:
        return 0
    if n <= 6:
        return 2
    return 3


def _correct_token(token: str, loader) -> Optional[Union[_Correction, _Ambiguity]]:
    """
    Return a _Correction for one OOV token when there's a single unambiguous
    winner, an _Ambiguity when two-or-more CATALOG terms tie for best at the
    same distance (caller can turn this into a clarification chip), or None
    when there's nothing worth acting on (no match, or the tie is only among
    protected/glue words).
    """
    max_edits = _max_edits_for(token)
    if max_edits == 0:
        return None

    # extract() over the combined search space; pull more than 1 so we can
    # detect ties, which extractOne can't reveal. limit=4 caps how many chip
    # options a runaway tie could produce.
    matches = process.extract(
        token,
        loader.fuzzy_vocab_terms,
        scorer=DamerauLevenshtein.distance,
        score_cutoff=max_edits,
        limit=4,
    )
    if not matches:
        return None

    best_term, best_dist, _ = matches[0]
    tied_terms = [m[0] for m in matches if m[1] == best_dist]

    if len(tied_terms) > 1:
        catalog_tied = [
            t for t in tied_terms
            if loader.fuzzy_vocab_types.get(t, "protected") != "protected"
        ]
        if len(catalog_tied) < 2:
            # Tie is only among glue/stop words (or one catalog + one glue
            # word, where the glue word isn't a real competing meaning) —
            # not worth a clarification chip. Don't guess; just skip it.
            logger.debug(
                f"[TypoFix] ambiguous (non-catalog) — not correcting | token={token!r} | "
                f"tied candidates={tied_terms} at distance {best_dist}"
            )
            return None
        logger.debug(
            f"[TypoFix] ambiguous catalog tie — deferring to clarification | "
            f"token={token!r} | tied candidates={catalog_tied} at distance {best_dist}"
        )
        return _Ambiguity(candidates=catalog_tied, distance=best_dist)

    vocab_type = loader.fuzzy_vocab_types.get(best_term, "protected")
    return _Correction(term=best_term, distance=best_dist, vocab_type=vocab_type)


def correct_message(message: str, loader) -> tuple[str, list, list]:
    """
    Correct misspelled catalog/glue words in `message`.

    Returns (corrected_message, corrections, ambiguities):
      - corrections: list of {"original", "corrected", "distance", "type"}
        dicts for every unambiguous fix that was applied — empty when
        nothing changed.
      - ambiguities: list of {"original", "candidates", "distance"} dicts,
        one per token that tied between two-or-more catalog terms. These
        tokens are left AS-IS in corrected_message (not corrected, not
        removed) so the caller can build a clarification chip and splice
        the user's chosen candidate into corrected_message afterward.

    The original message is returned untouched (with both lists empty) when
    the loader is missing or has no fuzzy vocabulary (e.g. degraded/
    still-warming tenant), so this is always safe to call.
    """
    if not message or loader is None or not getattr(loader, "fuzzy_vocab_terms", None):
        return message, [], []

    corrections: list = []
    ambiguities: list = []
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

        if isinstance(result, _Ambiguity):
            # Leave parts[i] untouched — the token stays exactly as the user
            # typed it in corrected_message, ready to be substituted once
            # the clarification chip is answered.
            ambiguities.append(
                {"original": token, "candidates": result.candidates, "distance": result.distance}
            )
            continue

        parts[i] = result.term
        changed = True
        corrections.append(
            {"original": token, "corrected": result.term, "distance": result.distance, "type": result.vocab_type}
        )

    corrected_message = "".join(parts) if changed else working

    if corrections:
        logger.info(
            f"[TypoFix] applied {len(corrections)} correction(s) | "
            + " ; ".join(f"{c['original']!r}→{c['corrected']!r}({c['type']})" for c in corrections)
        )
    if ambiguities:
        logger.info(
            f"[TypoFix] {len(ambiguities)} ambiguous catalog tie(s) deferred to clarification | "
            + " ; ".join(f"{a['original']!r}→{a['candidates']}" for a in ambiguities)
        )

    return corrected_message, corrections, ambiguities