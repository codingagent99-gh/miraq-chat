"""
classifier/keywords.py — the vocabulary the intent classifier depends on.

WHY THIS EXISTS
───────────────
The fuzzy typo corrector (utils/typo_correction.py) rewrites any out-of-
vocabulary token >= 4 chars to the nearest catalog term within 2-3 edits.
Conversational control words are OOV against a product catalog, and they sit
alarmingly close to real product vocabulary:

    "bulk"   -> tied with ['bark', 'back', 'dusk']   (distance 2)
    "cancel" -> "panel"                              (distance 2)
    "browse" -> "brown"                              (distance 2)
    "view"   -> "new"                                (distance 2)
    "skip"   -> "ship"                               (distance 1)

When that happens the classifier regex that was supposed to fire never sees
its trigger word, and an entire flow becomes unreachable. A sales rep typing
"bulk order" got a "did you mean bark, back, or dusk?" chip instead of the
bulk-order flow.

Hand-maintaining a protected-word list does not work: it drifts the moment
someone adds an evaluator. So each evaluator declares KEYWORDS, this module
unions them, and audit_keyword_drift() diffs the declarations against the
regex literals actually present in the source. Adding a pattern without
updating KEYWORDS fails the audit instead of silently breaking a flow months
later.
"""

import ast
import inspect
import re
from typing import Dict, Set

from chat_logger import get_logger

logger = get_logger("miraq_chat")


# ── Regex fragments that are stems or structural noise, not real tokens ──
# "categor" comes from `categor(?:y|ies)`; it can never appear as a whole
# token, so it needs no protection and must not be reported as drift.
_STEM_ALIASES: Dict[str, Set[str]] = {
    "categor": {"category", "categories"},
}

_IGNORE = {
    "the", "and", "for", "with", "you", "this", "that", "from", "not",
    "ies", "ymal",
}


def _words_in_regex_literals(source: str) -> Set[str]:
    """Harvest alpha tokens from every raw-string regex literal in `source`."""
    out: Set[str] = set()
    # (?<![\w]) so an apostrophe after a word ending in "r" — as in
    # "evaluator's regexes" — isn't mistaken for the start of an r'' literal
    # and swallowed into a giant cross-line match.
    for m in re.finditer(r"(?<![\w])r['\"](.*?)['\"]", source, re.S):
        lit = m.group(1)
        # Strip escapes so `\bbulk` yields "bulk", not "bbulk".
        lit = re.sub(r"\\[a-zA-Z]", " ", lit)
        lit = re.sub(r"\\.", " ", lit)
        for w in re.findall(r"[a-zA-Z]{3,}", lit):
            w = w.lower()
            if w not in _IGNORE:
                out.add(w)
    return out


def build_classifier_keywords() -> frozenset:
    """Union of every evaluator's declared KEYWORDS."""
    from classifier.evaluators import DEFAULT_EVALUATORS

    words: Set[str] = set()
    for ev in DEFAULT_EVALUATORS:
        words.update(w.lower() for w in getattr(ev, "KEYWORDS", frozenset()))
    return frozenset(words)


def audit_keyword_drift(strict: bool = False) -> Dict[str, Set[str]]:
    """
    Diff each evaluator's declared KEYWORDS against the regex literals in its
    own source. Returns {evaluator_name: {undeclared words}} — empty when
    everything is in sync.

    Call at startup (logs an error) and from the test suite with strict=True
    (raises). Cheap: parses one file, once.
    """
    from classifier import evaluators as _ev_mod
    from classifier.evaluators import DEFAULT_EVALUATORS

    try:
        src = inspect.getsource(_ev_mod)
        tree = ast.parse(src)
        lines = src.splitlines()
    except (OSError, SyntaxError) as exc:  # frozen/compiled deploys
        logger.warning(f"[KeywordAudit] source unavailable, skipping: {exc}")
        return {}

    by_name = {type(ev).__name__: ev for ev in DEFAULT_EVALUATORS}
    drift: Dict[str, Set[str]] = {}

    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in by_name:
            continue

        segment = "\n".join(lines[node.lineno - 1: node.end_lineno])
        # Drop the KEYWORDS block itself so declarations can't self-satisfy.
        segment = re.sub(
            r"KEYWORDS\s*=\s*frozenset\(\{.*?\}\)", "", segment, flags=re.S
        )

        found = _words_in_regex_literals(segment)
        declared = {w.lower() for w in getattr(by_name[node.name], "KEYWORDS", set())}

        missing = set()
        for w in found:
            if w in declared:
                continue
            if w in _STEM_ALIASES and _STEM_ALIASES[w] <= declared:
                continue
            missing.add(w)

        if missing:
            drift[node.name] = missing

    if drift:
        detail = " | ".join(
            f"{cls}: {sorted(ws)}" for cls, ws in sorted(drift.items())
        )
        msg = (
            f"[KeywordAudit] {sum(len(v) for v in drift.values())} regex "
            f"literal(s) not declared in KEYWORDS — these can be silently "
            f"typo-corrected and break their flow. {detail}"
        )
        if strict:
            raise AssertionError(msg)
        logger.error(msg)
    else:
        logger.debug("[KeywordAudit] all evaluator KEYWORDS in sync.")

    return drift


# Built once at import. Imported by store_loader.lookup_builder.build_fuzzy_vocab.
CLASSIFIER_KEYWORDS: frozenset = frozenset()


def get_classifier_keywords() -> frozenset:
    """Lazily build and cache the union (avoids an import cycle at module load)."""
    global CLASSIFIER_KEYWORDS
    if not CLASSIFIER_KEYWORDS:
        CLASSIFIER_KEYWORDS = build_classifier_keywords()
        logger.info(
            f"[KeywordAudit] protecting {len(CLASSIFIER_KEYWORDS)} classifier "
            f"keyword(s) from typo correction."
        )
    return CLASSIFIER_KEYWORDS