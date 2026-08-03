"""
handlers/typo_clarification_handler.py — Builds and resolves the clarification
prompt for typo-correction ties: two-or-more catalog terms sitting at the
same edit distance from a misspelled token, where guessing wrong would
misfile the search (e.g. "shelfs" -> "shelf" vs "shelves" vs "shelf tile",
all equidistant).

Deliberately mirrors the semantic-clarification chip UX in
handlers/semantic_clarification_handler.py / filter_clarification_handler.py
so the two flows feel identical to the shopper, but this one stays
text-in/text-out rather than mutating entities directly: typo correction
runs BEFORE Phase 1 classification and only has raw vocab terms, not
resolved slugs/taxonomy, so the correct move is to splice the shopper's
chosen word back into the message and let the normal pipeline
(typo-correction -> Phase 1 classification -> semantic clarification if
still needed) run again on it, exactly as if they'd typed it correctly.

Limitation (v1): only the first pending ambiguity in a message is surfaced
per turn. A message with two separately-misspelled, separately-ambiguous
tokens will chip on the first; the second is left uncorrected for that turn
(falls through to Phase 3 semantic search) rather than queued the way
build_semantic_clarification queues `pending_other_semantics`. Revisit if
this turns out to be common in practice.
"""

import re
import time

from flask import jsonify

from conversation_flow import FlowState
from handlers.chat_utils import default_pagination

_CANCEL_WORDS = {"cancel", "exit", "stop", "nevermind", "never mind", "abort", "start over"}

# Sentinel: the shopper backed out of the chip entirely. Distinct from the
# reject path, which still wants a search run on the original spelling.
# Returning base_message for BOTH is what caused the infinite re-prompt:
# the caller reset flow_state to IDLE and fed the still-ambiguous token
# straight back into correct_message(), which re-raised the same tie.
TYPO_CLARIFICATION_CANCELLED = object()


def build_typo_clarification(ambiguity, corrected_message, user_context, session_id, page, start_time):
    """
    Build the chip prompt for one pending typo ambiguity.

    `ambiguity` is one entry from correct_message()'s third return value:
    {"original": token, "candidates": [...], "distance": n}.
    `corrected_message` is the message with all *unambiguous* corrections
    already applied and this token still in its original, uncorrected form
    — this is what gets stashed and spliced into on resolution.
    """
    original = ambiguity["original"]
    candidates = ambiguity["candidates"]

    # Single-candidate guards (e.g. the "mos" confirmation) read as a
    # confirmation, not a disambiguation, so they carry their own wording.
    bot_message = ambiguity.get("prompt") or (
        f"I found multiple matches for '{original}'. Which did you mean?"
    )

    reject_label = f"Search '{original}'"
    suggestion_buttons = list(candidates) + [reject_label, "Cancel"]

    pending = {
        "original_token": original,
        "candidates": candidates,
        "base_message": corrected_message,
        "reject_label": reject_label,
    }
    user_context["pending_typo_clarification"] = pending

    elapsed = time.time() - start_time
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": suggestion_buttons,
        "session_id": session_id,
        "metadata": {
            "flow_state": FlowState.AWAITING_FILTER_CLARIFICATION.value,
            "pending_typo_clarification": pending,
            "response_time_ms": round(elapsed * 1000),
        },
        "flow_state": FlowState.AWAITING_FILTER_CLARIFICATION.value,
        "pagination": default_pagination(page),
    })


def _word_boundary_sub(token: str, replacement: str, text: str) -> str:
    """Replace the first whole-word, case-insensitive occurrence of token in text."""
    pattern = re.compile(r"\b" + re.escape(token) + r"\b", re.IGNORECASE)
    return pattern.sub(replacement, text, count=1)


def resolve_typo_clarification(message: str, user_context: dict, pending_typo: dict):
    """
    Resolve the shopper's response to a typo-ambiguity chip prompt.

    Returns one of:
      - str                          — message text to re-run through typo
                                       correction + Phase 1 classification:
                                       `base_message` with the chosen
                                       candidate spliced in, or (on reject)
                                       `base_message` unchanged so Phase 3
                                       semantic search can take a shot at
                                       the original spelling.
      - TYPO_CLARIFICATION_CANCELLED — shopper backed out; the caller must
                                       NOT reprocess the text, just return
                                       to idle.
      - None                         — message doesn't match any expected
                                       response; caller leaves pending state
                                       untouched and falls through to normal
                                       handling (same contract as
                                       resolve_filter_clarification).

    On reject, `original_token` is recorded in
    user_context["typo_suppressed_tokens"] so the next correction pass
    leaves it alone instead of re-raising the identical chip.
    """
    msg_lower = message.lower().strip()
    original = pending_typo["original_token"]
    candidates = pending_typo["candidates"]
    base_message = pending_typo["base_message"]
    reject_label = pending_typo.get("reject_label", "")

    is_cancel = msg_lower in _CANCEL_WORDS
    is_reject = bool(reject_label) and msg_lower == reject_label.lower()

    selected = None
    for c in candidates:
        if c.lower() == msg_lower:
            selected = c
            break

    if not (selected or is_reject or is_cancel):
        return None

    user_context.pop("pending_typo_clarification", None)

    if is_cancel:
        return TYPO_CLARIFICATION_CANCELLED

    if selected:
        return _word_boundary_sub(original, selected, base_message)

    # Reject: run the search on the original spelling, and remember that this
    # token was declined so the corrector doesn't immediately re-chip it.
    _suppressed = user_context.setdefault("typo_suppressed_tokens", [])
    if original.lower() not in _suppressed:
        _suppressed.append(original.lower())
    return base_message