"""
handlers/no_results_choice_handler.py — Resolves the AWAITING_NO_RESULTS_CHOICE
flow state, shown when a refined search (this turn's new filter(s) ANDed with
filters carried over from active_search) returns zero products.

Design ("Pattern A"):
  Separates "what the user just asked this turn" from "what's carried over
  from prior turns," and offers a one-tap resolution instead of a flat dump
  of every active filter:

    I don't see any products matching {new_thing} along with your current
    filters ({carried_filters}).

    [ Search that filter only ]
    [ Remove it, keep my filters ]
    [ New Search ]

  Button labels are intentionally fixed/generic rather than embedding the
  (potentially long, markdown-heavy, multi-part) filter description — that
  description is shown in the prose instead. Embedding variable text in a
  button label means resolve_no_results_choice() would need to re-derive
  and exact-match that same text later, which is fragile (whitespace,
  markdown, or duplicate-attribute-key bugs can change the label between
  renders) and was the cause of an earlier bug where tapping a button fell
  through to normal classification instead of being recognized.

  The pending stash stored in user_context["pending_no_results_choice"] must
  contain:
    turn_new : dict with attributes, tags, categories, min_price, max_price,
               attr_tag_or_pairs — this turn's filters, captured BEFORE the
               merge_into_active_search() call.
    active   : the active_search "slots" dict (same shape) — the carried-over
               filters from prior turns.
"""

import time
from typing import Optional

from flask import jsonify

from models import ExtractedEntities
from conversation_flow import FlowState
from handlers.chat_utils import default_pagination
from handlers.search_refinement import describe_active_filters_labeled, describe_active_filters

# ── Internal helpers ──────────────────────────────────────────────────────────

def _entities_from_snapshot(snapshot: dict) -> ExtractedEntities:
    """Build a lightweight ExtractedEntities from a turn_new/active snapshot
    dict, so we can reuse describe_active_filters_labeled() for display text
    instead of writing separate label-formatting logic."""
    e = ExtractedEntities()
    e.attributes          = dict(snapshot.get("attributes", {}))
    e.tag_slugs            = list(snapshot.get("tags", []))
    e.target_category_slugs = set(snapshot.get("categories", []))
    e.min_price            = snapshot.get("min_price")
    e.max_price             = snapshot.get("max_price")
    e.attr_tag_or_pairs    = list(snapshot.get("attr_tag_or_pairs", []))
    return e


def _is_snapshot_empty(snapshot: dict) -> bool:
    return not (
        snapshot.get("attributes")
        or snapshot.get("tags")
        or snapshot.get("categories")
        or snapshot.get("attr_tag_or_pairs")
        or snapshot.get("min_price")
        or snapshot.get("max_price")
    )


# ── Public API ────────────────────────────────────────────────────────────────

def build_no_results_prompt(
    turn_new: dict,
    active: dict,
    session_id: str,
    page: int,
    start_time: float,
) -> tuple:
    """
    Build a Flask JSON response offering the shopper a one-tap fix for a
    zero-result refined search.
    """
    new_label     = describe_active_filters_labeled(_entities_from_snapshot(turn_new))
    carried_label = describe_active_filters_labeled(_entities_from_snapshot(active))
    new_summary     = describe_active_filters(_entities_from_snapshot(turn_new))
    carried_summary = describe_active_filters(_entities_from_snapshot(active))

    if carried_label:
        bot_message = (
            f"I don't see any products matching {new_label} along with "
            f"your current filters ({carried_label})."
        )
        suggestion_buttons = [
            f"Search {new_summary}",
            f"Search {carried_summary}",
            "New Search",
        ]
    else:
        # No carried-over filters to blame — just this turn's filter alone
        # returned nothing. Skip the second option since there's nothing
        # else to fall back to besides a fresh search.
        bot_message = f"I don't see any products matching {new_label}."
        suggestion_buttons = [
            "New Search",
        ]

    elapsed = time.time() - start_time
    return jsonify({
        "success":    True,
        "bot_message": bot_message,
        "intent":     "guided_flow",
        "products":   [],
        "suggestions": suggestion_buttons,
        "session_id": session_id,
        "metadata": {
            "flow_state":       FlowState.AWAITING_NO_RESULTS_CHOICE.value,
            "response_time_ms": round(elapsed * 1000),
        },
        "flow_state":  FlowState.AWAITING_NO_RESULTS_CHOICE.value,
        "pagination":  default_pagination(page),
    }), 200

def resolve_no_results_choice(
    message: str,
    pending: dict,
) -> Optional[ExtractedEntities]:
    """
    Parse the shopper's chip response and return ExtractedEntities ready to
    re-search with. Returns None if the message doesn't match the expected
    chip labels (caller falls through to normal classification).

    Both buttons are now phrased as "Search {options}" with a different
    option set each (this turn's new filter(s) vs. the carried-over
    filter(s)) — so they can no longer be told apart by a fixed prefix the
    way "Search..." vs "Remove..." could be. Matching falls back to
    reconstructing both possible labels from the same snapshots used to
    build them, and comparing the full string.
    """
    msg      = message.strip()
    turn_new = pending.get("turn_new", {})
    active   = pending.get("active", {})

    new_summary     = describe_active_filters(_entities_from_snapshot(turn_new))
    carried_summary = describe_active_filters(_entities_from_snapshot(active))

    mode = None
    if msg.lower() == f"search {new_summary}".lower():
        mode = "new_only"
    elif msg.lower() == f"search {carried_summary}".lower():
        mode = "active_only"

    if mode is None:
        return None

    snapshot = turn_new if mode == "new_only" else active

    entities = ExtractedEntities()
    entities.attributes           = dict(snapshot.get("attributes", {}))
    entities.tag_slugs            = list(snapshot.get("tags", []))
    entities.target_category_slugs = set(snapshot.get("categories", []))
    entities.min_price            = snapshot.get("min_price")
    entities.max_price            = snapshot.get("max_price")
    entities.attr_tag_or_pairs    = list(snapshot.get("attr_tag_or_pairs", []))

    return entities
