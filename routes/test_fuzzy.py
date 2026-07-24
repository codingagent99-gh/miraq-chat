"""
routes/test_fuzzy.py — Test endpoint for typo_correction.py.

Two vocab sources, picked automatically:

  1. TENANT (default) — if the request carries X-MiraQ-License-Id and that
     tenant's real store_loader is healthy, correct_message() runs against
     the loader's actual fuzzy_vocab_terms / fuzzy_vocab_types /
     fuzzy_protected_words — the same object chat.py uses at line ~1354.
     This is what you want for "does this typo get fixed against tenant X's
     real catalog" questions.

  2. FAKE (opt-in) — pass a "vocab" block in the body to bypass the tenant
     entirely and test against a throwaway vocab. No X-MiraQ-License-Id
     needed for this path. Useful for isolating typo_correction.py's logic
     from any particular tenant's data (e.g. reproducing the tile/tiles tie
     deterministically without depending on what's actually in a catalog).

correct_message() only ever reads three things off the "loader" it's given,
so both paths just need to supply an object with these three attributes:

    loader.fuzzy_vocab_terms     -> list[str]      candidates for fuzzy matching
    loader.fuzzy_vocab_types     -> dict[str, str]  term -> "category"|"tag"|
                                                     "attribute"|"product_word"
    loader.fuzzy_protected_words -> set[str]        never-correct words (stop
                                                     words, glue words, synonym
                                                     keys, and the vocab terms
                                                     themselves)

    POST /test/fuzzy-search
    Headers: X-MiraQ-License-Id: <license_id>   (only used if no "vocab" body key)
    Body:
        {
          "message": "showme tilse in kicthen",
          "debug": true,

          // optional — if present, bypasses the tenant loader entirely
          "vocab": {
            "category": ["kitchen", "bathroom", "living room"],
            "product_word": ["tile", "tiles"]
          },
          "protected_words": ["show", "me", "in", "the", "a"]
        }
"""

from flask import Blueprint, request, jsonify
from rapidfuzz import process
from rapidfuzz.distance import DamerauLevenshtein

from store_registry import get_store_loader
from utils.typo_correction import (
    correct_message,
    _TOKEN_SPLIT_RE,
    _MIN_CORRECTABLE_LEN,
    _max_edits_for,
)

test_fuzzy_bp = Blueprint("test_fuzzy", __name__)

# Minimal built-in vocab so the endpoint still works with no tenant and no
# "vocab" override — kept as an explicit last-resort fallback, never silent.
DEFAULT_TEST_VOCAB = {
    "category": ["kitchen", "bathroom", "living room", "outdoor"],
    "tag": ["glossy", "matte", "textured", "polished"],
    "attribute": ["white", "black", "grey", "beige"],
    "product_word": ["tile", "tiles", "slab", "slabs"],
}
DEFAULT_PROTECTED_WORDS = {
    "show", "me", "in", "the", "a", "an", "of", "for", "with",
}


class _FakeLoader:
    """Bare object exposing only what correct_message() reads."""

    def __init__(self, vocab: dict, protected_words: set):
        terms = []
        types = {}
        for vocab_type, words in vocab.items():
            for w in words:
                w = w.lower()
                terms.append(w)
                types[w] = vocab_type

        # Vocab terms are themselves protected (a token that's already a
        # correct catalog word should never be "corrected" to another one) —
        # mirrors how the real loader builds fuzzy_protected_words.
        self.fuzzy_vocab_terms = terms
        self.fuzzy_vocab_types = types
        self.fuzzy_protected_words = {w.lower() for w in protected_words} | set(terms)


def _debug_tokens(message: str, loader) -> list:
    """
    Re-walk the same tokens correct_message() would, but report every
    candidate rapidfuzz found (not just the winner) so ties/near-misses that
    get silently refused are visible. Mirrors the skip conditions in
    correct_message()'s token loop exactly — this is diagnostics only, it
    never mutates the message.
    """
    out = []
    for part in _TOKEN_SPLIT_RE.split(message):
        token = part.lower()
        if not token or not token.isalpha():
            continue

        entry = {"token": token}

        if len(token) < _MIN_CORRECTABLE_LEN:
            entry["skipped_reason"] = "too_short"
            out.append(entry)
            continue
        if token in loader.fuzzy_protected_words:
            entry["skipped_reason"] = "protected"
            out.append(entry)
            continue
        if (token.endswith("s") and token[:-1] in loader.fuzzy_protected_words) or (
            (token + "s") in loader.fuzzy_protected_words
        ):
            entry["skipped_reason"] = "plural_of_protected"
            out.append(entry)
            continue

        max_edits = _max_edits_for(token)
        matches = process.extract(
            token,
            loader.fuzzy_vocab_terms,
            scorer=DamerauLevenshtein.distance,
            score_cutoff=max_edits,
            limit=5,
        )
        entry["max_edits"] = max_edits
        entry["candidates"] = [{"term": m[0], "distance": m[1]} for m in matches]
        entry["ambiguous"] = len(matches) > 1 and matches[0][1] == matches[1][1]
        out.append(entry)

    return out


@test_fuzzy_bp.route("/test/fuzzy-search", methods=["POST"])
def test_fuzzy_search():
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "Send JSON with a 'message' field."}), 400

    vocab_source = None
    loader = None

    if "vocab" in body:
        # Explicit opt-in: bypass the tenant entirely.
        protected_words = body.get("protected_words")
        if protected_words is None:
            protected_words = DEFAULT_PROTECTED_WORDS
        loader = _FakeLoader(body["vocab"], protected_words)
        vocab_source = "custom"
    else:
        # Default path: real tenant loader, resolved from X-MiraQ-License-Id
        # by register_before_request() same as every other route.
        real_loader = get_store_loader()
        if real_loader is not None and getattr(real_loader, "fuzzy_vocab_terms", None):
            loader = real_loader
            vocab_source = "tenant"

    if loader is None:
        # No usable tenant loader (missing/invalid license header, degraded
        # tenant, or empty vocab) and no explicit override given — fall back
        # to the built-in vocab, but say so plainly rather than silently
        # testing against data the caller didn't ask for.
        protected_words = body.get("protected_words") or DEFAULT_PROTECTED_WORDS
        loader = _FakeLoader(DEFAULT_TEST_VOCAB, protected_words)
        vocab_source = "default_fallback"

    corrected, corrections = correct_message(message, loader)

    response = {
        "success": True,
        "original_message": message,
        "corrected_message": corrected,
        "changed": corrected != message,
        "corrections": corrections,
        "vocab_used": vocab_source,
    }
    if vocab_source == "tenant":
        response["vocab_term_count"] = len(loader.fuzzy_vocab_terms)
    if vocab_source == "default_fallback":
        response["warning"] = (
            "No tenant loader available (missing/invalid X-MiraQ-License-Id, "
            "or tenant is degraded with no fuzzy vocab) — tested against the "
            "built-in default vocab instead. Pass a valid license header to "
            "test real catalog data."
        )

    if body.get("debug"):
        response["debug"] = _debug_tokens(message, loader)

    return jsonify(response)