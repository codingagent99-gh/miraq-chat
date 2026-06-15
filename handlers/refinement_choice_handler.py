"""
handlers/refinement_choice_handler.py — Resolves the AWAITING_REFINEMENT_CHOICE
flow state when a shopper is asked to clarify whether a new attribute value should
be added alongside the existing one (multi-value append) or replace it.

Design:
  Triggered when detect_slot_conflicts() finds a multi-value attribute slot that
  already holds a value and the current turn extracted a *different* value for it.
  The shopper is shown two chips ("Add {incoming}" / "Switch to {incoming}") and
  their tap is intercepted in chat.py Step 1 — BEFORE classification — so the
  pipeline continues normally with fully-resolved entities.

  The pending stash stored in user_context["pending_refinement"] must contain:
    conflicts         : list of {key, existing, incoming}
    active_search     : the active_search snapshot at the time of conflict
    incoming_attributes, incoming_tags, incoming_categories,
    incoming_min_price, incoming_max_price, incoming_or_pairs
      — the current turn's entity fields, for carrying forward non-conflict slots.
"""

import time
from typing import Optional

from flask import jsonify

from models import ExtractedEntities
from conversation_flow import FlowState
from handlers.chat_utils import default_pagination


# ── Internal helpers ──────────────────────────────────────────────────────────

def _csv_append(existing: str, incoming: str) -> str:
    """Append CSV values, de-duplicating, preserving order."""
    seen = [v.strip() for v in existing.split(",") if v.strip()]
    for v in (x.strip() for x in incoming.split(",") if x.strip()):
        if v not in seen:
            seen.append(v)
    return ",".join(seen)


def _display(val: str) -> str:
    """Make a raw attribute value (slug or CSV) human-readable."""
    return val.replace(",", " & ").replace("-", " ")


# ── Public API ────────────────────────────────────────────────────────────────

def build_refinement_prompt(
    conflicts: list,
    session_id: str,
    page: int,
    start_time: float,
) -> tuple:
    """
    Build a Flask JSON response asking the shopper to choose add-vs-replace.

    Single conflict  → "You're currently looking at **beige** color products.
                         Would you like to add **white** alongside, or switch to white only?"
                         Chips: [Add white] [Switch to white] [New Search]

    Multiple conflicts → summary + [Add to current] [Replace current] [New Search]
    """
    suggestion_buttons = []

    if len(conflicts) == 1:
        c = conflicts[0]
        existing_disp = _display(c["existing"])
        incoming_disp = _display(c["incoming"])
        attr_disp     = c["key"].replace("-", " ").title()

        bot_message = (
            f"You're currently looking at **{existing_disp}** {attr_disp.lower()} products. "
            f"Would you like to narrow to products available in "
            f"**both {existing_disp} and {incoming_disp}**, or switch to {incoming_disp} only?"
        )
        suggestion_buttons = [
            "Show both",
            f"Switch to {incoming_disp}",
            "New Search",
        ]
    else:
        existing_summary = "; ".join(
            f"{c['key'].replace('-', ' ')}: {_display(c['existing'])}" for c in conflicts
        )
        incoming_summary = "; ".join(
            f"{c['key'].replace('-', ' ')}: {_display(c['incoming'])}" for c in conflicts
        )
        bot_message = (
            f"You're currently filtering by **{existing_summary}**. "
            f"Would you like to add **{incoming_summary}** to your current filters, "
            f"or replace them?"
        )
        suggestion_buttons = [
            "Add to current",
            "Replace current",
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
            "flow_state":       FlowState.AWAITING_REFINEMENT_CHOICE.value,
            "response_time_ms": round(elapsed * 1000),
        },
        "flow_state":  FlowState.AWAITING_REFINEMENT_CHOICE.value,
        "pagination":  default_pagination(page),
    })


def resolve_refinement_choice(
    message: str,
    pending: dict,
) -> Optional[ExtractedEntities]:
    """
    Parse the shopper's chip response and return fully-merged ExtractedEntities
    ready to search with.  Returns None if the message doesn't match the expected
    chip labels (caller treats this as an unrecognized input and falls through to
    normal classification).

    The returned entities include:
      - Conflicting attributes resolved per mode (add/replace)
      - All non-conflicting active_search slots carried forward
      - New non-conflicting attrs from the current turn added
      - Tags, categories, price, OR pairs merged from both sources
    """
    msg      = message.strip()
    conflicts = pending.get("conflicts", [])

    # ── Parse mode from chip label ──
    mode = None
    if len(conflicts) == 1:
        c             = conflicts[0]
        incoming_disp = _display(c["incoming"])
        if msg.lower() == "show both":
            mode = "add"
        elif msg.lower() == f"switch to {incoming_disp}".lower():
            mode = "replace"
    else:
        if msg.lower() == "add to current":
            mode = "add"
        elif msg.lower() == "replace current":
            mode = "replace"

    if mode is None:
        return None

    # ── Rebuild fully-merged entities ────────────────────────────────────────
    entities = ExtractedEntities()
    entities.target_category_slugs = set()

    active_slots      = pending.get("active_search", {}).get("slots", {})
    conflicts_by_key  = {c["key"]: c for c in conflicts}

    # Attributes: resolve conflicts, carry the rest from active_search
    for key, val in active_slots.get("attributes", {}).items():
        if key in conflicts_by_key:
            incoming = conflicts_by_key[key]["incoming"]
            entities.attributes[key] = (
                _csv_append(str(val), incoming) if mode == "add" else incoming
            )
        else:
            entities.attributes[key] = val

    # Add any NEW attrs from the current turn that weren't in active_search at all
    for key, val in pending.get("incoming_attributes", {}).items():
        if key not in conflicts_by_key and key not in entities.attributes:
            entities.attributes[key] = val

    # Tags: union of active + incoming
    seen_tags          = set(active_slots.get("tags", []))
    entities.tag_slugs = list(active_slots.get("tags", []))
    for t in pending.get("incoming_tags", []):
        if t not in seen_tags:
            entities.tag_slugs.append(t)

    # Categories: union
    entities.target_category_slugs = set(active_slots.get("categories", []))
    entities.target_category_slugs.update(pending.get("incoming_categories", []))

    # Price: incoming wins if set, else carry from active
    entities.min_price = pending.get("incoming_min_price") or active_slots.get("min_price")
    entities.max_price = pending.get("incoming_max_price") or active_slots.get("max_price")

    # OR pairs: handle conflicts by taxonomy
    # Add mode    → union of active + incoming (same as normal merge)
    # Replace mode → drop active pairs for conflicting taxonomies, keep the rest,
    #                then add all incoming pairs (including the replacement)
    or_pair_conflicts_by_tax = {
        c["attr_taxonomy"]: c
        for c in conflicts
        if c.get("type") == "or_pair"
    }

    seen_pairs    = set()
    combined_pairs = []

    if or_pair_conflicts_by_tax and mode == "replace":
        for op in active_slots.get("attr_tag_or_pairs", []):
            tax = op.get("attr_key") or op.get("attr_taxonomy")
            if tax in or_pair_conflicts_by_tax:
                continue           # drop: user chose to replace this taxonomy's value
            k = str(op)
            if k not in seen_pairs:
                seen_pairs.add(k)
                combined_pairs.append(op)
        for op in pending.get("incoming_or_pairs", []):
            k = str(op)
            if k not in seen_pairs:
                seen_pairs.add(k)
                combined_pairs.append(op)
    else:
        # Add mode (or no OR-pair conflicts): union of active + incoming
        for op in (
            list(active_slots.get("attr_tag_or_pairs", []))
            + pending.get("incoming_or_pairs", [])
        ):
            k = str(op)
            if k not in seen_pairs:
                seen_pairs.add(k)
                combined_pairs.append(op)

    entities.attr_tag_or_pairs = combined_pairs

    return entities