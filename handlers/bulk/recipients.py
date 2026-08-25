"""Recipient, company and roster prompting for the bulk-order flow.

Split verbatim out of handlers/bulk_order_handler.py — pure move, no logic
changes. Function and constant definitions are byte-identical to what they
replaced; the only additions are this module's imports and the deferred
imports (each marked inline) that break the import cycle back into the
handler.
"""

import time
import re

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination

# Variant helpers already live in their own module (stage 1 of this split),
# so these import normally — handlers.bulk.variants does not import this one.
from handlers.bulk.variants import (
    _ask_for_bulk_variant,
    _ensure_missing_axes,
    _is_variable_product,
)

logger = get_logger("miraq_chat")

# How many contact names are offered as tappable chips at the recipient step.
# The full roster goes out in metadata.candidates regardless — this only caps
# the chip row, which becomes unusable past roughly this many. Any message
# quoting a contact COUNT must say how many are actually shown, or the rep
# reads "20 contacts" above 8 chips and assumes the rest are unreachable.
_RECIPIENT_CHIP_LIMIT = 8

# Chip labels for paging the contact list. Matched before any name lookup in
# handle_bulk_recipient_reply — if a real contact were ever called this, the
# rep could not select them, so keep them clearly non-name-like.
_MORE_CONTACTS_CHIP = "▸ More contacts"

_PREV_CONTACTS_CHIP = "◂ Previous contacts"

# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_email_reply ──
# ══════════════════════════════════════════════════════════════

def _build_recipient_queue(lines_as_dicts, split_unnamed: bool = False) -> list:
    """
    One slot per DISTINCT unresolved recipient.

    Lines naming the same person share a slot (one answer settles them).
    Lines naming NOBODY are ambiguous — "Order Harmony Moon, Adams Grey at
    Beck" could be one person taking both or two people taking one each — so
    the rep is asked which, and split_unnamed carries that answer:

        False -> all unnamed lines share one slot (one question, one person)
        True  -> each unnamed line gets its own slot (asked separately)

    Returns [] when every line already has a customer, which is the signal to
    skip the prompt entirely.
    """
    slots: dict = {}
    for idx, l in enumerate(lines_as_dicts):
        if l.get("unresolved_reason") not in ("recipient_required", "recipient_not_found"):
            continue
        raw = (l.get("recipient_name") or "").strip()
        key = re.sub(r'[^a-z0-9]+', ' ', raw.lower()).strip()
        if not key and split_unnamed:
            key = f"__line_{idx}"   # unique per line -> one question each
        slot = slots.setdefault(key, {"name": raw, "line_indices": []})
        slot["line_indices"].append(idx)
    return list(slots.values())

# Button labels for the "one person or several?" step. Defined once so the
# prompt and the reply matcher can never drift — a mismatch here would make a
# tapped button fall through to the keyword fallback, or worse, re-ask forever.
RECIPIENT_MODE_SAME = "Same person"

RECIPIENT_MODE_DIFFERENT = "Different people"

def _ask_recipient_mode(
    lines_as_dicts, queue, conversation, user_context, page, start_time,
):
    """
    Ask whether the unnamed lines all go to ONE person or to different people.

    Only reached when more than one line named nobody — with a single line
    there is nothing to disambiguate. Guessing either way silently misassigns
    goods, so the rep is asked once and the answer shapes the queue.
    """
    slot = next(s for s in queue if not s["name"] and len(s["line_indices"]) > 1)
    scope = user_context.get("bulk_company_scope", "")

    conversation.flow_state = FlowState.AWAITING_BULK_RECIPIENT_MODE.value
    user_context["bulk_recipient_queue"] = queue
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    product_lines = "\n".join(
        f"\u2022 **{lines_as_dicts[i]['quantity']}\u00d7 {lines_as_dicts[i]['product_name']}**"
        for i in slot["line_indices"]
    )

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": (
            f"{product_lines}\n\nAre these all for the same person at "
            f"**{scope}**, or for different people?"
        ),
        "intent": "guided_flow",
        "products": [],
        "suggestions": [RECIPIENT_MODE_SAME, RECIPIENT_MODE_DIFFERENT, "Cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT_MODE.value,
            "company": scope,
            "line_count": len(slot["line_indices"]),
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_RECIPIENT_MODE.value,
        "pagination": default_pagination(page),
    }), 200

def handle_bulk_recipient_mode_reply(
    message, store_loader, conversation, user_context, page, start_time
):
    """
    Called during AWAITING_BULK_RECIPIENT_MODE.

    "Same person"     -> unnamed lines stay in one slot, asked once.
    "Different people" -> each unnamed line becomes its own slot.
    Anything unrecognised re-asks rather than guessing.
    """
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import _continue_after_slots_filled

    reply = (message or "").strip().lower()
    lines = user_context.get("pending_bulk_lines", [])

    # Exact button label first — this is the intended path, and it is decided
    # without interpreting anything. Free text is only a fallback for reps who
    # type instead of tapping.
    if reply == RECIPIENT_MODE_SAME.lower():
        _same, _diff = True, False
    elif reply == RECIPIENT_MODE_DIFFERENT.lower():
        _same, _diff = False, True
    else:
        _same = any(w in reply for w in ("same", "single", "one person", "1 person"))
        _diff = any(w in reply for w in ("different", "separate", "multiple", "each", "various"))

    if _same == _diff:   # both or neither matched -> ambiguous, ask again
        queue = user_context.get("bulk_recipient_queue") or _build_recipient_queue(lines)
        return _ask_recipient_mode(
            lines, queue, conversation, user_context, page, start_time
        )

    mode = "same" if _same else "different"
    user_context["bulk_recipient_mode"] = mode

    queue = _build_recipient_queue(lines, split_unnamed=(mode == "different"))
    user_context["bulk_recipient_queue"] = queue
    user_context["bulk_recipient_pos"] = 0
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    logger.info(
        f"bulk_order | recipient mode = {mode!r} -> {len(queue)} question(s)"
    )

    if not queue:
        return _continue_after_slots_filled(
            lines, store_loader, conversation, user_context, page, start_time
        )

    return _ask_for_bulk_recipient(
        lines, queue, 0, conversation, user_context, page, start_time
    )

def _roster_label(entry: dict, disambiguate: bool = False) -> str:
    """
    Picker label for one roster entry.

    Plain name normally. When two entries share a name — the same person on
    file at two of the company's sites — the name alone is useless, so the
    city (or street, or email) is appended. Without this the rep sees two
    identical buttons and cannot tell which address they are choosing.
    """
    name = (entry.get("display") or "").strip()
    if not disambiguate:
        return name
    detail = (
        entry.get("city")
        or entry.get("address_1")
        or entry.get("email")
        or ""
    ).strip()
    state = (entry.get("state") or "").strip()
    if detail and state and detail != state:
        detail = f"{detail}, {state}"
    return f"{name} \u2014 {detail}" if detail else name

def _recipient_candidates(slot, roster):
    """
    Roster entries to offer for this slot, plus label/wording flags.

    Returns (entries, disambiguate_labels, name_matched).

    For a name that matches roster entries, only THOSE entries are offered —
    showing the whole company would bury the actual choice. When the name
    matches nobody, the full roster is offered as a fallback so the rep can
    still pick, but `name_matched` comes back False so the caller words the
    prompt as "I couldn't find X" instead of claiming those people share the
    requested name.

    `disambiguate_labels` is purely about LABELS (append city when two offered
    entries share a display name) and says nothing about whether the requested
    name matched — conflating the two is what produced "There are 20 people
    named Kelly Fitchett" for a roster of 20 unrelated customers that merely
    happened to contain two Carissa Diazes.
    """
    def _norm(v):
        return re.sub(r'[^a-z0-9]+', ' ', str(v or "").lower()).strip()

    # Tolerate a malformed roster (bad JSONB, partial write, older schema)
    # rather than raising or offering unusable buttons.
    roster = [r for r in (roster or []) if isinstance(r, dict) and r.get("display")]

    needle = _norm(slot.get("name") if isinstance(slot, dict) else "")
    entries = roster
    name_matched = False

    if needle and roster:
        # Every word of the requested name must appear in the display name.
        # The previous test was `needle in _norm(display).split()`, which
        # asked whether the whole "kelly fitchett" string equalled one
        # single-word token — impossible for any first+last name, so this
        # branch was dead and only exact equality could ever match.
        needle_tokens = [t for t in needle.split() if t]
        matched = []
        for r in roster:
            hay = _norm(r.get("display"))
            if not hay:
                continue
            hay_tokens = hay.split()
            if (
                hay == needle
                or (needle_tokens and all(t in hay_tokens for t in needle_tokens))
                or needle in hay
            ):
                matched.append(r)

        # Narrow on ANY match, including exactly one. The old guard was
        # `len(matched) > 1`, so a single match — and, critically, ZERO
        # matches — silently left `entries` as the entire company roster.
        if matched:
            entries = matched
            name_matched = True

    counts = {}
    for r in entries:
        k = _norm(r.get("display"))
        counts[k] = counts.get(k, 0) + 1
    disambiguate = any(c > 1 for c in counts.values())
    return entries, disambiguate, name_matched

def _ask_for_bulk_recipient(
    lines_as_dicts, queue, pos, conversation, user_context, page, start_time,
):
    """Prompt for the recipient of one slot in the queue."""
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import (
        _address_label,
        _company_order_addresses,
    )

    roster = user_context.get("bulk_company_roster", []) or []
    scope  = user_context.get("bulk_company_scope", "")
    _truncated = bool(user_context.get("bulk_company_roster_truncated", False))

    # ── Stage 1: which COMPANY? ─────────────────────────────────────────────
    # Company lookup is fuzzy (substring OR similar_text >= 70), so one name
    # can legitimately return people from SEVERAL companies — "Turner Ceramic
    # Tile" and "Turner Ceramics" are different businesses. Asking for the
    # person first would mix staff from both into one list, and the rep could
    # pick someone from the wrong company without ever seeing that two
    # existed. Company first, then person.
    def _norm_company(v):
        # Same normalisation the plugin uses: punctuation is noise, so
        # "Turner Ceramic & Tile" and "Turner Ceramic Tile" are ONE company.
        # Deduping on the raw string instead would offer a picker between two
        # spellings of the same business.
        return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]+', ' ', str(v or "").lower())).strip()

    _companies = []
    _seen_c = set()
    for r in roster:
        c = (r.get("company") or "").strip()
        key = _norm_company(c)
        if c and key and key not in _seen_c:
            _seen_c.add(key)
            _companies.append(c)

    if len(_companies) > 1 and not user_context.get("bulk_company_choice_made"):
        conversation.flow_state = FlowState.AWAITING_BULK_COMPANY_CHOICE.value
        user_context["bulk_recipient_queue"] = queue
        user_context["bulk_recipient_pos"] = pos
        user_context["pending_company_choice"] = {"companies": _companies}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        _el = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"**{len(_companies)}** companies match **{scope}**. "
                "Which one is this order for?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": _companies[:8] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
                "candidates": _companies,
                "requested_company": scope,
                "response_time_ms": _el,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
            "pagination": default_pagination(page),
        }), 200


    # ── Fail-safe: no usable slot to ask about ──────────────────────────────
    # A lost/corrupt queue (resumed session, partial context write) would
    # otherwise IndexError here. Bail to the company step rather than
    # guessing which line we were asking about.
    if not queue or not (0 <= pos < len(queue)) or not isinstance(queue[pos], dict):
        logger.warning(
            f"bulk_order | recipient prompt aborted — bad queue state "
            f"(pos={pos}, queue_len={len(queue) if queue else 0}) for company '{scope}'"
        )
        conversation.flow_state = FlowState.AWAITING_BULK_COMPANY.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                "I lost track of who this order is for. "
                "Which company is this order for?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    slot = queue[pos]
    entries, _disambiguate, _name_matched = _recipient_candidates(slot, roster)
    names = [_roster_label(r, _disambiguate) for r in entries if r.get("display")]

    # ── Fail-safe: nothing to offer ─────────────────────────────────────────
    # An empty/unusable roster previously produced "**X** has 0 contacts."
    # with a Cancel-only button — a dead end. Send the rep back to the
    # company step, which is the thing that actually needs correcting.
    if not names:
        # ── Fallback: order-history addresses ───────────────────────────────
        # An empty roster means no USER ACCOUNT carries this company name. The
        # company can still be real: /company-order-addresses derives it from
        # ORDER HISTORY, which is the only source that sees a company that has
        # been shipped to but whose customers never had the company field set
        # on their account. Offer those destinations rather than dead-ending
        # the rep on a company that demonstrably exists.
        _addr_rows = _company_order_addresses(scope, user_context)
        if _addr_rows:
            _opts = []
            for _r in _addr_rows:
                _lbl = _address_label(_r)
                if _lbl and _lbl not in _opts:
                    _opts.append(_lbl)
            logger.info(
                f"bulk_order | roster empty for '{scope}' — offering "
                f"{len(_opts)} order-history address(es)"
            )
            if _opts:
                conversation.flow_state = FlowState.AWAITING_BULK_ADDRESS_CHOICE.value
                user_context["bulk_recipient_queue"] = queue
                user_context["bulk_recipient_pos"] = pos
                # Signals the address step is standing in for the recipient
                # step: there is no customer account to attach, so the name is
                # collected at confirmation instead.
                user_context["bulk_address_only_mode"] = True
                user_context["bulk_address_queue"] = [{
                    "name": scope,
                    "company": scope,
                    "options": _addr_rows,
                    "line_indices": list(range(len(lines_as_dicts))),
                }]
                user_context["bulk_address_pos"] = 0
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                _el = round((time.time() - start_time) * 1000)
                return jsonify({
                    "success": True,
                    "bot_message": (
                        f"I don't have contact records for **{scope}**, but it has "
                        f"been shipped to before. Pick a delivery address and I'll "
                        f"use it — you can add the recipient's name at confirmation."
                    ),
                    "intent": "guided_flow",
                    "products": [],
                    "suggestions": _opts[:8] + ["Cancel"],
                    "session_id": str(conversation.id),
                    "metadata": {
                        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
                        "company": scope,
                        "candidates": _opts,
                        "address_only_mode": True,
                        "response_time_ms": _el,
                    },
                    "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
                    "pagination": default_pagination(page),
                }), 200

        logger.warning(
            f"bulk_order | recipient prompt aborted — no usable roster entries "
            f"for company '{scope}' (roster_size={len(roster)})"
        )
        conversation.flow_state = FlowState.AWAITING_BULK_COMPANY.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                # Reached only when the company has NO customer accounts AND has
                # never been shipped to — genuinely unknown to this store.
                f"I can't find **{scope}** — there are no customer records and "
                f"no past deliveries under that name. Check the spelling, or tell "
                f"me which company this order is for."
                if scope else "Which company is this order for?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                "company": scope,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    conversation.flow_state = FlowState.AWAITING_BULK_RECIPIENT.value
    user_context["bulk_recipient_queue"] = queue
    user_context["bulk_recipient_pos"] = pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    # Skip line indices that no longer exist rather than IndexError-ing on a
    # queue built against a different (older) set of lines.
    product_lines = "\r\n".join(
        f"• **{lines_as_dicts[i]['quantity']}× {lines_as_dicts[i]['product_name']}**"
        for i in slot.get("line_indices", [])
        if isinstance(i, int) and 0 <= i < len(lines_as_dicts)
    )

    # ── Chip paging ─────────────────────────────────────────────────────────
    # Paged, not truncated: with 20 contacts the rep saw the first 8 and had
    # no way to reach the other 12 short of typing a name exactly right.
    # Clamped rather than wrapped — a stale page index (roster re-fetched
    # smaller, or the rep backing out of a company choice) would otherwise
    # render an empty chip row with no way forward.
    _chip_page = int(user_context.get("bulk_recipient_chip_page", 0) or 0)
    _max_chip_page = max(0, (len(names) - 1) // _RECIPIENT_CHIP_LIMIT)
    _chip_page = max(0, min(_chip_page, _max_chip_page))
    user_context["bulk_recipient_chip_page"] = _chip_page
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    _chip_lo = _chip_page * _RECIPIENT_CHIP_LIMIT
    _chip_hi = min(_chip_lo + _RECIPIENT_CHIP_LIMIT, len(names))
    _chips = names[_chip_lo:_chip_hi]

    _nav = []
    if _chip_page < _max_chip_page:
        _nav.append(_MORE_CONTACTS_CHIP)
    if _chip_page > 0:
        _nav.append(_PREV_CONTACTS_CHIP)

    # Wording keys off whether the requested name actually matched — never off
    # the label-disambiguation flag, which can be True purely because two
    # UNRELATED people on the roster share a name.
    if slot.get("name") and _name_matched and len(entries) > 1:
        _ask = (
            f"There are {len(entries)} people named **{slot['name']}** at "
            f"**{scope}**, at different addresses. Which one?"
        )
    elif slot.get("name") and _name_matched:
        _ask = (
            f"Is this **{slot['name']}** at **{scope}**? "
            "Confirm or pick someone else."
        )
    elif slot.get("name") and _truncated:
        # The roster is incomplete, so absence was never established — don't
        # tell the rep this person isn't at the company when we only read
        # part of it.
        _ask = (
            f"I couldn't find **{slot['name']}** in the first {len(roster)} "
            f"contacts at **{scope}**, and there may be more I haven't checked. "
            "Who should this go to?"
        )
    elif slot.get("name"):
        _ask = (
            f"I couldn't find **{slot['name']}** at **{scope}**. "
            "Who should this go to?"
        )
    else:
        # Chips are paged rather than truncated: with 20 contacts the rep
        # previously saw 8 and had no way to reach the other 12 except by
        # typing a name exactly right.
        _shown = min(len(names), _RECIPIENT_CHIP_LIMIT)
        if len(names) > _RECIPIENT_CHIP_LIMIT:
            _ask = (
                f"**{scope}** has {len(names)} contacts — showing "
                f"{_chip_lo + 1}–{_chip_hi} of {len(names)}. "
                "Tap a name, or type one."
            )
        else:
            _ask = f"**{scope}** has {len(names)} contacts. Who should this go to?"

    _progress = f" ({pos + 1} of {len(queue)})" if len(queue) > 1 else ""

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": f"{product_lines}\r\n\r\n{_ask}{_progress}",
        "intent": "guided_flow",
        "products": [],
        "suggestions": _chips + _nav + ["Cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
            "company": scope,
            "candidates": names,
            "requested_name": slot.get("name", ""),
            # False means these candidates are a fallback list of everyone at
            # the company, NOT people matching requested_name — the frontend
            # must not label them as such.
            "name_matched": _name_matched,
            # True means these candidates are only part of the company's
            # contacts — absence from this list proves nothing.
            "roster_truncated": _truncated,
            "progress": {"current": pos + 1, "total": len(queue)},
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
        "pagination": default_pagination(page),
    }), 200

def handle_bulk_recipient_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_RECIPIENT when the rep names a person.

    Applies the pick to the CURRENT queue slot only, then advances. Stamping
    every unresolved line at once would silently ship Claire's product to
    Ashlynn whenever the two names failed together.
    """
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import _continue_after_slots_filled

    choice = (message or "").strip()
    roster = user_context.get("bulk_company_roster", []) or []
    scope  = user_context.get("bulk_company_scope", "")
    lines  = user_context.get("pending_bulk_lines", [])
    queue  = user_context.get("bulk_recipient_queue", []) or []
    pos    = user_context.get("bulk_recipient_pos", 0)

    # ── Chip paging, before any name matching ───────────────────────────────
    # These are navigation, not answers: re-ask the SAME slot with the next or
    # previous page of contacts. Matched first so a paging tap can never be
    # fuzzy-matched to a contact whose name happens to look similar, which
    # would silently ship to the wrong person.
    if choice in (_MORE_CONTACTS_CHIP, _PREV_CONTACTS_CHIP):
        _cur = int(user_context.get("bulk_recipient_chip_page", 0) or 0)
        user_context["bulk_recipient_chip_page"] = max(
            0, _cur + (1 if choice == _MORE_CONTACTS_CHIP else -1)
        )
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_recipient(
            lines, queue, pos, conversation, user_context, page, start_time,
        )

    # Queue lost (e.g. resumed session) — rebuild from the lines themselves.
    if not queue or pos >= len(queue):
        queue = _build_recipient_queue(
            lines,
            split_unnamed=(user_context.get("bulk_recipient_mode") == "different"),
        )
        pos = 0
        if not queue:
            return _continue_after_slots_filled(
                lines, store_loader, conversation, user_context, page, start_time
            )

    def _norm(v):
        return re.sub(r'[^a-z0-9]+', ' ', str(v or "").lower()).strip()

    needle = _norm(choice)
    picked = None

    # Match against the labels actually offered for THIS slot. For a
    # duplicate name those read "Raj Chanda — Dallas, TX", so matching on
    # the bare display name would be ambiguous all over again and would
    # silently take the first record — the exact bug this step exists to
    # prevent.
    slot_for_match = queue[pos] if pos < len(queue) else {"name": ""}
    entries, _disambiguate, _name_matched = _recipient_candidates(slot_for_match, roster)

    def _only(pred):
        """The single entry satisfying pred, or None if 0 or 2+ do.

        Correctness here depends on the MATCH being unique, not on the
        roster being unique: "Elizabeth Rhodes" is an unambiguous answer
        even when two unrelated Raj Chandas sit in the same list. Gating on
        a roster-wide ambiguity flag instead would reject perfectly clear
        replies.
        """
        hits = [r for r in entries if pred(r)]
        return hits[0] if len(hits) == 1 else None

    if needle:
        # 1. exact label as offered ("Raj Chanda — Dallas, TX")
        picked = _only(lambda r: _norm(_roster_label(r, _disambiguate)) == needle)
        # 2. exact display name, only if it identifies one person
        if not picked:
            picked = _only(lambda r: _norm(r.get("display")) == needle)
        # 3. partial (first/last name), again only if unique
        if not picked:
            def _loose(r):
                hay = _norm(r.get("display"))
                return bool(hay) and (needle in hay or any(needle == w for w in hay.split()))
            picked = _only(_loose)

    # A roster entry with no id cannot be stamped onto a line — treating it as
    # a match would produce an order with no customer attached. Fall through
    # to the re-ask instead.
    if picked and not picked.get("id"):
        logger.warning(
            f"bulk_order | discarded match '{picked.get('display')}' for "
            f"'{choice}' at '{scope}' — roster entry has no id"
        )
        picked = None

    if not picked:
        names = [_roster_label(r, _disambiguate) for r in entries if r.get("display")]
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"I couldn't match **{choice}** to anyone at **{scope}**."
                + (
                    f" Here are the first {_RECIPIENT_CHIP_LIMIT} of "
                    f"{len(names)} contacts — type a name if it's someone else."
                    if len(names) > _RECIPIENT_CHIP_LIMIT else ""
                )
                if choice else f"Who should this go to at **{scope}**?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": names[:_RECIPIENT_CHIP_LIMIT] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
                "candidates": names,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
            "pagination": default_pagination(page),
        }), 200

    shipping = picked.get("shipping") or {}
    if not shipping.get("address_1"):
        shipping = picked.get("billing") or {}

    slot = queue[pos]
    for idx in slot["line_indices"]:
        if idx >= len(lines):
            continue
        line = lines[idx]
        line["customer_id"]           = picked["id"]
        line["customer_display_name"] = picked["display"]
        line["recipient_name"]        = picked["display"]
        line["shipping_address"]      = shipping
        line["billing_address"]       = picked.get("billing") or {}
        line["is_self_order"]         = False
        line["unresolved"]            = False
        line["unresolved_reason"]     = None

    user_context["pending_bulk_lines"] = lines
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    logger.info(
        f"bulk_order | recipient '{picked['display']}' (id={picked['id']}) "
        f"applied to line(s) {slot['line_indices']} "
        f"(slot {pos + 1}/{len(queue)}, asked for '{slot['name']}') "
        f"for company '{scope}'"
    )

    # More people still to identify?
    if pos + 1 < len(queue):
        # Fresh chip page for the next slot — carrying page 3 over would open
        # the next question part-way down the contact list.
        user_context["bulk_recipient_chip_page"] = 0
        return _ask_for_bulk_recipient(
            lines, queue, pos + 1,
            conversation, user_context, page, start_time,
        )

    user_context.pop("bulk_recipient_chip_page", None)
    user_context.pop("bulk_recipient_queue", None)
    user_context.pop("bulk_recipient_pos", None)
    user_context.pop("bulk_recipient_mode", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    return _continue_after_slots_filled(
        lines, store_loader, conversation, user_context, page, start_time
    )

def handle_bulk_company_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_COMPANY when the rep names the company.

    The company scopes the entire transaction, so rather than patching
    individual lines we re-run the original utterance with the company
    appended — one code path for resolution instead of two.
    """
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import (
        _continue_after_slots_filled,
        handle_bulk_order_input,
    )

    company = (message or "").strip().strip('.,')

    # "Continue anyway" after a company with no records at all — the rep knows
    # the company is real (a brand-new client has neither an account nor
    # delivery history).
    #
    # Rejoin the NORMAL flow rather than diverting to free-text address entry.
    # The address-confirmation step later on already renders an editable panel,
    # validates every required field, and blocks until they are filled — so
    # asking the rep to type a name and address here duplicates that step,
    # loses the structured field-by-field validation, and does it BEFORE the
    # quantity and variant questions instead of at the point the rest of the
    # flow puts it.
    #
    # The lines keep their unresolved customer: with no company records there
    # is nobody to resolve them to, so the confirmation panel opens with blank
    # shipping fields for the rep to complete.
    if company.lower() in ("continue anyway", "continue", "proceed anyway", "proceed"):
        user_context.pop("bulk_manual_address_mode", None)
        # Remember that the company lookup was deliberately bypassed, so no
        # later step re-prompts for a company the rep has already skipped.
        user_context["bulk_company_skipped"] = True
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        logger.info(
            "bulk_order | rep chose to continue with no company records — "
            "resuming normal flow; address collected at the confirmation step"
        )
        _lines = user_context.get("pending_bulk_lines", [])
        return _continue_after_slots_filled(
            _lines, store_loader, conversation, user_context, page, start_time
        )

    if company.lower() in ("enter a different company", "different company"):
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "Which company is this order for?",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    if not company:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "Please provide the company name for this order.",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    lines = user_context.get("pending_bulk_lines", [])
    original = user_context.get("pending_bulk_utterance", "") or " , ".join(
        l.get("raw_fragment", "") for l in lines if l.get("raw_fragment")
    )

    replay = f"{original} for company {company}"
    logger.info(f"bulk_order | company supplied → replaying with scope '{company}'")

    return handle_bulk_order_input(
        replay, store_loader, conversation, user_context, page, start_time
    )

def handle_bulk_email_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_EMAIL when the rep provides customer email(s).
    Extracts email(s), resolves customers via API, stamps the pending lines,
    then resumes the normal bulk flow (variant selection → confirmation).
    """
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import (
        _build_bulk_confirmation_response,
        _prompt_for_quantity,
    )

    import re as _re
    _EMAIL_RE = _re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', _re.I)

    emails = _EMAIL_RE.findall(message)
    if not emails:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "I couldn't find a valid email address. Please provide the customer's email.",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
            "pagination": default_pagination(page),
        }), 200

    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    missing_indices = [
        i for i, l in enumerate(lines_as_dicts)
        if l.get("unresolved_reason") == "email_not_provided"
    ]

    # Multiple emails + single unresolved line → clone the line for each email
    if len(emails) > 1 and len(missing_indices) == 1:
        base_idx = missing_indices[0]
        base = lines_as_dicts[base_idx]
        clones = [{**base, "email": email} for email in emails]
        lines_as_dicts = lines_as_dicts[:base_idx] + clones + lines_as_dicts[base_idx + 1:]
        missing_indices = list(range(base_idx, base_idx + len(emails)))
    else:
        # Assign emails round-robin to unresolved lines
        for i, line_idx in enumerate(missing_indices):
            lines_as_dicts[line_idx]["email"] = emails[i % len(emails)]

    # Resolve each newly assigned email
    email_cache: dict = {}
    for line_idx in missing_indices:
        line = lines_as_dicts[line_idx]
        email = line.get("email", "")
        if not email:
            continue

        if email not in email_cache:
            call = endpoints.search_customers_by_email(
                email=email,
                per_page=1,
                description=f"Bulk email reply lookup: '{email}'",
            )
            result = woo_client.execute(call)
            customers = result.get("data", []) if result.get("success") else []
            if isinstance(customers, list) and customers:
                c = customers[0]
                company = c.get("company") or c.get("billing", {}).get("company", "")
                full_name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
                billing = c.get("billing", {}) or {}
                shipping = c.get("shipping", {}) or {}
                if not shipping.get("address_1"):
                    shipping = billing
                email_cache[email] = {
                    "id": str(c["id"]),
                    "display": company or full_name or f"Customer #{c['id']}",
                    "billing": billing,
                    "shipping": shipping,
                }
            else:
                email_cache[email] = None

        resolution = email_cache.get(email)
        if resolution:
            line["customer_id"] = resolution["id"]
            line["customer_display_name"] = resolution["display"]
            line["billing_address"] = resolution["billing"]
            line["shipping_address"] = resolution["shipping"]
            line["unresolved"] = line.get("product_id") is None
            line["unresolved_reason"] = "product_not_found" if line["unresolved"] else None
        else:
            line["unresolved"] = True
            line["unresolved_reason"] = "email_not_found"
            line["customer_display_name"] = "⚠️ Not found"

    user_context["pending_bulk_lines"] = lines_as_dicts
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    
    blank_after_email = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("product_name", "").strip()
    ]
    if blank_after_email:
        user_context["bulk_product_missing_indices"] = blank_after_email
        user_context["bulk_product_current_pos"] = 0
        conversation.flow_state = FlowState.AWAITING_BULK_PRODUCT.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        first_line = lines_as_dicts[blank_after_email[0]]
        customer_hint = (
            "" if first_line.get("is_self_order")
            else f" for **{_line_recipient_display(first_line)}**"
            if first_line.get("customer_id") else ""
        )
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": f"What product and quantity would you like to order{customer_hint}?",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
            "pagination": default_pagination(page),
        }), 200
        
    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )

    # Check for variable products still needing variant selection
    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        # See the note at the first gate: a matched variation can still leave
        # axes as "Any", which the rep must still choose.
        if l.get("product_id")
        and _is_variable_product(l["product_id"], store_loader)
        and (not l.get("variation_id")
             or l.get("blank_variant_axes")
             or _ensure_missing_axes(l, user_context))
    ]

    if needs_variant_indices:
        user_context["bulk_variant_line_indices"] = needs_variant_indices
        user_context["bulk_variant_current_pos"] = 0
        user_context["bulk_variant_cache"] = {}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, 0,
            conversation, user_context, page, start_time,
        )

    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )

def _line_recipient_display(line) -> str:
    """Name to SHOW for a line.

    customer_display_name doubles as a status label when no customer account
    resolved ("⚠️ No customers for X", "⚠️ Not found"). That is useful on the
    unresolved-lines table, but once the rep has supplied a shipping address
    the recipient IS known, and continuing to show the warning makes a
    complete order look broken.

    Order of preference: the shipping name the rep entered, then the company
    on the address, then the stored display name (which may still be a ⚠️
    label — correct when nothing has been supplied yet).
    """
    if not isinstance(line, dict):
        return ""
    ship = line.get("shipping_address") or {}
    name = " ".join(
        v for v in (ship.get("first_name"), ship.get("last_name")) if v
    ).strip()
    if name:
        return name
    if ship.get("company"):
        return ship["company"]
    bill = line.get("billing_address") or {}
    if bill.get("company"):
        return bill["company"]
    return line.get("customer_display_name", "") or ""

def handle_bulk_company_choice_reply(
    message, store_loader, conversation, user_context, page, start_time,
):
    """Rep picked which company the bulk order is for (stage 1 of 2).

    Narrows the roster to that company, then falls through to the recipient
    question. Matching is exact-then-unique-partial: an ambiguous reply
    re-asks rather than guessing, since picking the wrong company here would
    silently ship to a different business.
    """
    pending   = (user_context or {}).get("pending_company_choice") or {}
    companies = pending.get("companies") or []
    roster    = user_context.get("bulk_company_roster", []) or []
    queue     = user_context.get("bulk_recipient_queue") or []
    pos       = user_context.get("bulk_recipient_pos", 0)

    def _norm(v):
        return re.sub(r'[^a-z0-9]+', ' ', str(v or "").lower()).strip()

    reply = _norm(message)


    picked = None
    if reply:
        exact = [c for c in companies if _norm(c) == reply]
        if len(exact) == 1:
            picked = exact[0]
        else:
            partial = [c for c in companies if reply in _norm(c)]
            if len(partial) == 1:
                picked = partial[0]

    if not picked:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "I couldn't tell which company you meant. Please pick one:",
            "intent": "guided_flow",
            "products": [],
            "suggestions": companies[:8] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
                "candidates": companies,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
            "pagination": default_pagination(page),
        }), 200

    # Narrow the roster to the chosen company. The flag stops the question
    # being asked again for later recipients in the same order.
    _p = _norm(picked)
    filtered = [r for r in roster if _norm(r.get("company")) == _p]

    logger.info(
        f"bulk_order | company choice '{picked}' → "
        f"{len(filtered)} of {len(roster)} roster entries"
    )

    user_context["bulk_company_roster"] = filtered or roster
    user_context["bulk_company_scope"]  = picked
    user_context["bulk_company_choice_made"] = True
    user_context.pop("pending_company_choice", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    lines_as_dicts = user_context.get("pending_bulk_lines", []) or []
    return _ask_for_bulk_recipient(
        lines_as_dicts, queue, pos, conversation, user_context, page, start_time,
    )