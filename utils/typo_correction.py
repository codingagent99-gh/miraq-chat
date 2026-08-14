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

# ── Control / chip vocabulary — NEVER corrected ────────────────────────────
# These are the words that carry conversational control meaning: the exit
# words, the flow verbs the classifier regexes key off ("browse", "view",
# "track", "checkout"), and the words used in our own suggestion chips.
#
# They are OOV against catalog vocab, >=4 chars, and frequently sit within
# 1-2 edits of a real catalog term ("cancel"->"panel", "browse"->"brown",
# "skip"->"ship", "back"->"black", "view"->"new"), so without this set the
# corrector silently rewrites them and the downstream regex/flow match
# never fires. Adding them here ALSO makes them correction *targets*, so a
# genuinely misspelled control word ("cancle") now resolves to "cancel"
# instead of being force-fitted to the nearest product term.
CONTROL_PHRASE_WORDS = frozenset({
    # exit / cancel
    "cancel", "cancelled", "exit", "stop", "quit", "nevermind", "abort",
    "start", "over", "close", "reset", "clear",
    # cart / checkout / order lifecycle
    "cart", "checkout", "check", "place", "order", "orders", "ordering",
    "reorder", "purchase", "buy", "confirm", "confirmed", "track",
    "tracking", "status", "history", "invoice", "receipt", "refund",
    "return", "returns", "cancelation", "cancellation",
    # navigation / chips
    "browse", "view", "load", "more", "back", "next", "previous", "skip",
    "done", "continue", "select", "choose", "change", "edit", "update",
    "remove", "delete", "add", "again", "here", "there",
    # our own chip labels
    "filters", "filter", "original", "text", "exclude", "use", "using",
    "these", "categories", "category",
    # account / support
    "email", "address", "shipping", "billing", "payment", "account",
    "help", "support", "agent", "human", "quantity", "price", "total",
    # quantity / distribution glue — "each" sits 2 edits from 'patch', 'echo'
    # and 'back', so "1 chip card each of Harmony, Adams…" opened with a
    # "did you mean patch / echo / back?" clarification before the order was
    # read at all. These words carry meaning for the bulk parser ("N each of")
    # and must never be rewritten to a catalog term.
    "each", "every", "per", "both", "all", "some", "any", "of",
})

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


# ── "mos" confusion guard ─────────────────────────────────────────────────
# Hard rule, independent of edit distance: any word containing "mos" that
# isn't already a "mosaic" word gets a confirmation chip before entity
# extraction runs.
#
# Why this can't be left to the fuzzy corrector: the length-scaled edit
# budget is computed against the WHOLE token, so a long misspelling that
# happens to share a short prefix ("mosiah" -> "mosaic" is 2 edits, but
# "piazza mosiah" tokenised and scored against a vocab full of 2-edit
# neighbours) either loses to a closer unrelated term or never clears
# score_cutoff at all. This guard fires on the substring, so recall is
# 100% for the "mos-" family and the cost of a false positive is one
# tappable chip the shopper can dismiss.
_MOS_TARGET = "mosaic"
_MOS_WORD_RE = re.compile(r"[A-Za-z]+")

# Words that contain "mos" but should never prompt. Empty by default so the
# rule behaves exactly as specified; add ordinary English carriers here
# ("most", "almost", "mostly") if the chip starts firing on normal prose.
MOS_CONFUSION_EXEMPT = frozenset()


def find_mos_confusions(message: str, suppressed_tokens=None) -> list:
    """
    Return ambiguity-shaped dicts (same contract as correct_message()'s third
    return value) for every distinct word in `message` that contains "mos"
    but is not itself a mosaic word.

    Words containing "mosaic" as a substring — "mosaic", "mosaics",
    "mosaic-tile" — are already correct and skipped.

    `suppressed_tokens` is honoured for the same reason correct_message()
    honours it: once the shopper has pressed "Search 'mosiah'", re-raising
    the identical chip on the next turn is an infinite prompt loop, not a
    confirmation. This is the one bound on "always".
    """
    if not message:
        return []

    _suppressed = {t.lower() for t in (suppressed_tokens or ())}
    seen: set = set()
    confusions: list = []

    for word in _MOS_WORD_RE.findall(message):
        token = word.lower()
        if (
            "mos" not in token
            or _MOS_TARGET in token          # mosaic / mosaics / mosaic-anything
            or token in MOS_CONFUSION_EXEMPT
            or token in _suppressed          # shopper already declined this one
            or token in seen                 # same word twice in one message
        ):
            continue
        seen.add(token)
        confusions.append({
            "original": token,
            "candidates": [_MOS_TARGET],
            "distance": None,
            "prompt": f"Just to confirm — did you mean '{_MOS_TARGET}'?",
        })

    if confusions:
        logger.info(
            "[TypoFix] mos-guard raised %d confirmation(s) | %s",
            len(confusions),
            " ; ".join(repr(c["original"]) for c in confusions),
        )

    return confusions


# Words that can trail a person's name in a reporting query — part of the
# date phrase, not the name, so they stay correctable.
_DATE_TAIL_WORDS = frozenset({
    "a", "an", "the", "this", "last", "past", "in", "for", "at", "on",
    "since", "between", "month", "months", "quarter", "quarters", "year",
    "years", "week", "weeks", "day", "days", "today", "yesterday",
    "mtd", "qtd", "ytd", "to", "date", "so", "far",
})


def correct_message(message: str, loader, suppressed_tokens=None) -> tuple[str, list, list]:
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

    `suppressed_tokens` is an iterable of lowercase tokens the shopper has
    already declined correction for this session (they pressed "Search
    'urah'"). Those tokens are passed through verbatim and are NOT
    re-surfaced as ambiguities — without this, rejecting a chip just
    re-triggers the same chip on the next turn, forever.

    The original message is returned untouched (with both lists empty) when
    the loader is missing or has no fuzzy vocabulary (e.g. degraded/
    still-warming tenant), so this is always safe to call.
    """
    if not message or loader is None or not getattr(loader, "fuzzy_vocab_terms", None):
        return message, [], []

    _suppressed = {t.lower() for t in (suppressed_tokens or ())}

    # ── Protect PERSON NAMES from catalog-vocabulary correction ──────────────
    # A surname resembling a catalog term gets "corrected" into it: "ordered
    # by Jennifer Bullock" became "Jennifer Block" ("block" is a mosaic-type
    # attribute), so the rep lookup searched for someone who does not exist.
    # Catalog vocabulary must not rewrite a name the user typed.
    #
    # Names sit in known positions — after "ordered/placed by", or between
    # "did" and "order" — so protect those spans rather than trying to detect
    # names in general.
    for _m in re.finditer(
        r'\b(?:ordered|placed)\s+by\s+((?:[A-Za-z][\w.\-]*\s*){1,3})'
        r'|\bdid\s+((?:[A-Za-z][\w.\-]*\s*){1,3}?)\s+order\b',
        message, re.I,
    ):
        _span = next((g for g in _m.groups() if g), "") or ""
        for _w in _span.split():
            _w = _w.strip(".,?!;:").lower()
            # Date words trailing the name are not part of it — leave those
            # correctable.
            if _w and _w.isalpha() and _w not in _DATE_TAIL_WORDS:
                _suppressed.add(_w)


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
            or token in _suppressed          # shopper already declined this one
            or token in CONTROL_PHRASE_WORDS  # conversational control word
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