"""Address resolution, prompting and confirmation for the bulk-order flow.

Split verbatim out of handlers/bulk_order_handler.py — pure move, no logic
changes. Definitions are byte-identical to what they replaced; the only
additions are this module's imports and the deferred imports (each marked
inline) that break the import cycle back into the handler.
"""

import time
import re
import json
import unicodedata

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from app_config import BULK_ORDER_FULL_SCOPE_ROLES
from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination
from utils.checkout_fields import (
    count_missing,
    format_missing_fields,
    get_required_fields,
    has_errors,
    is_known_rep,
    validate_bulk_address,
)

# Earlier stages of this split. Import chain is one-directional
# (handler -> addresses -> recipients -> variants), so these are plain
# top-level imports with no cycle.
from handlers.bulk.variants import (
    _ask_for_bulk_variant,
    _ensure_missing_axes,
    _is_variable_product,
)
from handlers.bulk.recipients import _line_recipient_display

logger = get_logger("miraq_chat")

# ══════════════════════════════════════════════════════════════
# ── Helper: effective address for a bulk line ──
# ══════════════════════════════════════════════════════════════

def _merge_address_block(base, override):
    """
    Merge a panel override onto a base address block.

    A key ABSENT from the override keeps its base value; a key PRESENT with an
    empty string CLEARS it.

    The previous behaviour skipped empty override values entirely ("don't let
    blank panel fields wipe real data"), which meant a rep could not blank a
    field at all: clearing it in the panel silently restored the stale value,
    and the validation gate would then pass on data the rep had deliberately
    removed. Honouring empties is safe because the panel prefills from the same
    _pick()-generated payload the base came from, so unedited fields round-trip
    to identical values. If _pick is ever narrowed to emit fewer keys than the
    raw address block holds, revisit this.

    None is treated as "absent" rather than "clear", so a malformed payload
    can't wipe an address.
    """
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if value is None:
            continue
        merged[key] = value
    return merged

def _rep_billing_address(conversation, user_context):
    """
    Fetch the LOGGED-IN user's billing block, cached in user_context.

    Requirement: bulk order billing comes from the logged-in user, not from the
    customer the goods ship to. Shipping still comes from the resolved
    company/person — see _effective_address_for_line, which combines them.

    Cached because it is identical for every line in the transaction. A FAILED
    fetch is deliberately not cached: user_context is persisted on the
    conversation, so caching {} after one dropped connection would starve
    billing for the rest of the session with no way to recover. A successful
    fetch that happens to be empty IS cached — that is a real answer.
    """
    if user_context.get("rep_billing_fetched"):
        return user_context.get("rep_billing_address") or {}

    billing = {}
    fetched = False
    rep_id = getattr(conversation, "customer_id", None)
    if rep_id:
        try:
            call = endpoints.fetch_customer(
                customer_id=int(rep_id),
                description="Fetch logged-in user billing for bulk order",
            )
            result = woo_client.execute(call)
            if result.get("success") and isinstance(result.get("data"), dict):
                billing = result["data"].get("billing", {}) or {}
                fetched = True
            else:
                logger.warning(
                    f"bulk_order | rep billing fetch unsuccessful for {rep_id} | "
                    f"error={result.get('error')}"
                )
        except Exception as exc:
            logger.warning(f"bulk_order | rep billing fetch failed | error={exc}")

    if fetched:
        user_context["rep_billing_address"] = billing
        user_context["rep_billing_fetched"] = True

    logger.info(
        f"bulk_order | rep billing for user {rep_id} | fetched={fetched} | "
        f"company={billing.get('company')!r} | keys={sorted(billing.keys())}"
    )
    return billing

def _effective_address_for_line(line, address_overrides, line_idx, rep_email,
                                rep_billing=None, user_context=None):
    """
    Return (billing, shipping) for one bulk line.

    Billing is the LOGGED-IN user's billing block (rep_billing) when available,
    since bulk orders are billed to the rep placing them; the line's own
    billing_address is the fallback. Shipping always comes from the resolved
    company/person. The per-line panel override is merged in last, so anything
    the rep edits by hand still wins.

    This is the ONLY place bulk address merging happens. The validation gate,
    the card prefill and _create_all_confirmed_orders all call it, so they
    cannot drift apart — a line that passes validation is guaranteed to be the
    same line that gets posted to WooCommerce.

    The project_rep default is applied HERE, before validation, because order
    creation auto-fills it from the logged-in rep. Validating before applying it
    would block on a field that would have been populated anyway.
    """
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import _get

    override = (address_overrides or {}).get(str(line_idx)) or {}

    _billing_base = rep_billing if (rep_billing and rep_billing.get("address_1")) else _get(line, "billing_address")
    billing = _merge_address_block(_billing_base, override.get("billing"))
    shipping = _merge_address_block(_get(line, "shipping_address"), override.get("shipping"))

    # Auto-fill project_rep with the logged-in user — but ONLY when that email
    # is one of the options the project_rep field actually offers.
    #
    # This used to seed rep_email unconditionally. rep_email is whatever
    # payload_context["email"] carried, so a non-rep login (an admin, a dev
    # account) produced a project_rep matching no <option>: the widget's select
    # rendered BLANK while the value still passed .strip(), so it satisfied the
    # required-field gate without the rep ever seeing it and rode through to
    # _billing_project_rep. It also made "orders by rep" reporting track
    # whoever was logged in rather than a rep.
    #
    # Leaving it blank is the correct outcome for a non-rep: project_rep is in
    # BULK_ADDRESS_REQUIRED_FLOOR["meta"], so the existing gate blocks and asks
    # for it by name instead of silently inventing an answer.
    # Values the user typed as explicit clauses ("rep John Smith",
    # "order type new deal"). Applied BEFORE the auto-fill below so an
    # explicitly named rep always beats the logged-in-user default — the
    # whole point of typing it. Still only fills a BLANK field, so a value
    # the user edited on the card in a previous turn is never overwritten.
    #
    # These are already validated: the parser stores the option VALUE
    # (project_rep holds the rep's email, billing_field_type holds e.g.
    # "new_deal"), never the raw typed label, so the widget's <select> can
    # render them. Anything that failed validation was left out entirely and
    # reported as a notice, which is what routes it to the missing-field
    # prompt below instead of writing an unrenderable string.
    _clause_values = (user_context or {}).get("bulk_field_clause_values") or {}
    for _slot in ("project_rep", "billing_field_type", "billing_project"):
        _typed = str(_clause_values.get(_slot) or "").strip()
        if _typed and not str(billing.get(_slot) or "").strip():
            billing[_slot] = _typed

    if not str(billing.get("project_rep") or "").strip():
        billing["project_rep"] = rep_email if is_known_rep(rep_email) else ""

    return billing, shipping

# Cache TTL for _company_order_addresses — mirrors checkout_fields.py's
# _CACHE_TTL_SECONDS (900s / 15 min) for the same reason: bounded staleness
# instead of caching forever for the life of a chat session.
_ORDER_ADDRESS_CACHE_TTL_SECONDS = 900

def _company_order_addresses(company, user_context):
    """
    Shipping destinations this company has actually had goods sent to.

    Derived from ORDER HISTORY (GET /company-order-addresses) — the same
    source the storefront's own company address picker uses, and the only one
    that shows several addresses for a single person: /customers/by-company
    returns one account address each, and the THWMA address book is empty on
    this store.

    Cached per company, with a 15-minute TTL (same convention as the
    /checkout-fields cache in utils/checkout_fields.py). A forever-cache within
    the session was wrong here specifically because this data backs the
    shipping-email fallback: a new order placed mid-session (on the storefront
    or through a prior bulk order) needs to become visible without the rep
    having to start a fresh chat session.
    """
    if not company:
        return []
    cache = user_context.setdefault("bulk_order_address_cache", {})
    key = str(company).strip().lower()
    entry = cache.get(key)
    # A session whose cache was populated before this TTL change deployed
    # still has the OLD shape persisted (a bare list, not {"rows", "ts"}).
    # Treat anything that isn't the new dict shape as a miss rather than
    # crashing on .get() — it just re-fetches once and overwrites the stale
    # entry with the correct shape.
    if isinstance(entry, dict) and (time.time() - entry.get("ts", 0)) < _ORDER_ADDRESS_CACHE_TTL_SECONDS:
        return entry["rows"]

    rows = []
    fetched = False
    try:
        call = endpoints.fetch_company_order_addresses(
            company_name=company,
            description=f"Order-history addresses for {company!r}",
        )
        res = woo_client.execute(call)
        data = res.get("data") if res.get("success") else None
        if isinstance(data, dict):
            data = data.get("data", [])
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
            fetched = True
    except Exception as exc:
        logger.warning(
            f"bulk_order | order-address lookup failed for {company!r} | error={exc}"
        )

    # Cache SUCCESS only. user_context is persisted on the conversation, so
    # caching [] after a failed call silently disables the address step for
    # the rest of the TTL window — the next turn short-circuits and never even
    # retries the API. A successful empty result IS cached; that is a real
    # answer.
    if fetched:
        cache[key] = {"rows": rows, "ts": time.time()}
        user_context["bulk_order_address_cache"] = cache
    logger.info(
        f"bulk_order | company {company!r} → {len(rows)} historical "
        f"address(es) | fetched={fetched}"
    )
    return rows

def _addresses_for_person(rows, first_name, last_name, customer_id=None):
    """
    Historical addresses belonging to one person, de-duplicated.

    Matched on NAME first. An order-history row's customer_id is whoever
    PLACED that order, not who received it — storefront order #1066561 is
    owned by sovan (272754865) and shipped to Ashlynn Archer — so an id that
    disagrees means "a rep placed this for them", not "this is someone else".
    Treating a mismatch as a rejection hid real addresses. customer_id is used
    only when there is no name to match on.

    Only rows with no street at all are dropped — a partial address is still
    shown. These come from past checkouts, so the list includes whatever was
    typed at the time; an entry like "ASDF" with no city or postcode WILL be
    offered. That is deliberate: a stricter filter silently hid a real second
    address for people whose other orders were sloppily entered, and the rep
    can tell junk from real. The label shows every field present, so a bad
    entry is visibly bad at the point of choosing.
    """
    def _n(v):
        return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()

    want_name = f"{_n(first_name)} {_n(last_name)}".strip()
    out, seen = [], set()
    for r in rows:
        rid = int(r.get("customer_id") or 0)
        row_name = f'{_n(r.get("shipping_first_name"))} {_n(r.get("shipping_last_name"))}'.strip()
        if want_name:
            if row_name != want_name:
                continue
        elif customer_id and rid:
            if str(rid) != str(customer_id):
                continue

        a1 = str(r.get("shipping_address_1") or "").strip()
        if not a1:
            continue

        sig = _n("|".join([
            a1,
            str(r.get("shipping_address_2") or ""),
            str(r.get("shipping_city") or ""),
            str(r.get("shipping_state") or ""),
            str(r.get("shipping_postcode") or ""),
        ]))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out

def _address_label(row) -> str:
    """One-line address label for the picker."""
    parts = [
        str(row.get("shipping_address_1") or "").strip(),
        str(row.get("shipping_address_2") or "").strip(),
        str(row.get("shipping_city") or "").strip(),
        str(row.get("shipping_state") or "").strip(),
        str(row.get("shipping_postcode") or "").strip(),
    ]
    return ", ".join(p for p in parts if p)

def _build_address_queue(lines_as_dicts, user_context):
    """
    One slot per resolved customer that has MORE THAN ONE historical address.

    A customer with a single address needs no question — their line already
    carries it. Lines for the same customer share a slot, so the rep is asked
    once per person, not once per product.
    """
    company = user_context.get("bulk_company_scope", "")
    if not company:
        return []
    rows = _company_order_addresses(company, user_context)
    if not rows:
        return []

    slots = {}
    for idx, l in enumerate(lines_as_dicts):
        cid = l.get("customer_id")
        if not cid or l.get("unresolved"):
            continue
        if l.get("address_choice_made"):
            # Already asked and answered. Without this the queue rebuilds on
            # every pass through the shared exit — the quantity reply routes
            # back through it — and the rep is asked the same question twice.
            continue
        ship = l.get("shipping_address") or {}
        options = _addresses_for_person(
            rows, ship.get("first_name"), ship.get("last_name"), cid
        )
        if len(options) < 2:
            continue
        slot = slots.setdefault(str(cid), {
            "customer_id": str(cid),
            "name": l.get("customer_display_name", ""),
            "options": options,
            "line_indices": [],
        })
        slot["line_indices"].append(idx)
    return list(slots.values())

def _ask_for_bulk_address(
    lines_as_dicts, queue, pos, conversation, user_context, page, start_time,
):
    """Prompt for which of a person's known addresses to ship to."""
    slot = queue[pos]
    conversation.flow_state = FlowState.AWAITING_BULK_ADDRESS_CHOICE.value
    user_context["bulk_address_queue"] = queue
    user_context["bulk_address_pos"] = pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    labels = [_address_label(o) for o in slot["options"]]
    product_lines = "\r\n".join(
        f"• **{lines_as_dicts[i]['quantity']}× {lines_as_dicts[i]['product_name']}**"
        for i in slot["line_indices"]
    )
    _progress = f" ({pos + 1} of {len(queue)})" if len(queue) > 1 else ""

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": (
            f"{product_lines}\r\n\r\n**{slot['name']}** has "
            f"{len(labels)} addresses on file. Which one should this ship to?{_progress}"
        ),
        "intent": "guided_flow",
        "products": [],
        "suggestions": labels[:8] + ["Cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
            "customer": slot["name"],
            "candidates": labels,
            "progress": {"current": pos + 1, "total": len(queue)},
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
        "pagination": default_pagination(page),
    }), 200

# Blockers that picking a delivery address actually clears.
#
# In the order-history-address path (Step 4.55 / _ask_for_bulk_recipient
# fallback) the company has no customer ACCOUNTS, so every line arrives
# unresolved with one of these reasons and the chosen address is the only
# resolution there will ever be — the order is placed as a guest order against
# that address.
#
# Deliberately excludes "product_not_found" / "both_not_found" (an address
# cannot conjure a product) and "recipient_ambiguous" (several people share the
# name — that is a real ambiguity and must still be asked, not silently closed
# by an address choice).
_ADDRESS_RESOLVABLE_REASONS = frozenset({
    "company_not_provided",
    "company_not_found",
    "recipient_required",
    "recipient_not_found",
})

def handle_bulk_address_choice_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_ADDRESS_CHOICE.

    Applies the chosen address to the CURRENT slot only, then advances — same
    per-slot discipline as the recipient queue, so one person's choice never
    leaks onto another's lines.
    """
    choice = (message or "").strip()
    lines  = user_context.get("pending_bulk_lines", [])
    queue  = user_context.get("bulk_address_queue", []) or []
    pos    = user_context.get("bulk_address_pos", 0)

    if not queue or pos >= len(queue):
        return _continue_after_addresses_chosen(
            lines, store_loader, conversation, user_context, page, start_time
        )

    slot = queue[pos]

    def _n(v):
        return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()

    needle = _n(choice)
    picked = None
    if needle:
        for o in slot["options"]:
            if _n(_address_label(o)) == needle:
                picked = o
                break
        if not picked:
            # Partial: enough of the street to identify exactly one option.
            hits = [o for o in slot["options"] if needle in _n(_address_label(o))]
            if len(hits) == 1:
                picked = hits[0]

    if not picked:
        labels = [_address_label(o) for o in slot["options"]]
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"I couldn't match **{choice}** to one of "
                f"**{slot['name']}**'s addresses. Please pick one."
                if choice else "Please pick an address."
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": labels[:8] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
                "candidates": labels,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
            "pagination": default_pagination(page),
        }), 200

    _address_resolved = []
    for idx in slot["line_indices"]:
        if idx >= len(lines):
            continue
        ship = dict(lines[idx].get("shipping_address") or {})

        # The rep's OWN stated recipient wins over the name attached to a
        # historical address. Order-history rows carry whoever that delivery
        # went to, so copying their name across overwrote the people the rep
        # actually named — "1x London for Tamra Smith" silently became Kevin
        # Shuker's order because his name rode along with the address.
        # The address supplies the DESTINATION; the recipient comes from the
        # request.
        _stated = (lines[idx].get("recipient_name") or "").strip()
        if _stated:
            _parts = _stated.split()
            _first, _last = _parts[0], " ".join(_parts[1:])
        else:
            _first = picked.get("shipping_first_name", "") or ship.get("first_name", "")
            _last  = picked.get("shipping_last_name", "")  or ship.get("last_name", "")

        ship.update({
            "first_name": _first,
            "last_name":  _last,
            "company":    picked.get("company", "") or ship.get("company", ""),
            "address_1":  picked.get("shipping_address_1", ""),
            "address_2":  picked.get("shipping_address_2", ""),
            "city":       picked.get("shipping_city", ""),
            "state":      picked.get("shipping_state", ""),
            "postcode":   picked.get("shipping_postcode", ""),
            "country":    picked.get("shipping_country", ""),
            "email":      picked.get("shipping_email", "") or ship.get("email", ""),
        })
        lines[idx]["shipping_address"] = ship
        lines[idx]["address_choice_made"] = True

        # The chosen address IS the resolution for these lines.
        #
        # Nothing downstream cleared it: _create_all_confirmed_orders filters
        # on `unresolved`, so lines that had just been given a destination were
        # dropped from the order. They only survived when they happened to need
        # a variant prompt, because the variant reply cleared the flag as a
        # side effect — which meant a chip-card line (self-contained sample
        # form, never prompted) was ALWAYS dropped. Live: order #1066565 kept
        # Lager and Marigold and silently lost Harmony and Adams.
        #
        # On the normal path (_build_address_queue) this is a no-op: that queue
        # only ever contains lines that already resolved to a customer.
        if (
            lines[idx].get("unresolved")
            and lines[idx].get("unresolved_reason") in _ADDRESS_RESOLVABLE_REASONS
        ):
            lines[idx]["unresolved"] = False
            lines[idx]["unresolved_reason"] = None
            _address_resolved.append(idx)

    user_context["pending_bulk_lines"] = lines
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    logger.info(
        f"bulk_order | address {_address_label(picked)!r} applied to "
        f"line(s) {slot['line_indices']} for {slot['name']} "
        f"(slot {pos + 1}/{len(queue)})"
    )
    if _address_resolved:
        logger.info(
            f"bulk_order | chosen address resolved line(s) {_address_resolved} "
            f"— no customer account for {slot['name']!r}, order(s) will be "
            f"placed against the address"
        )

    if pos + 1 < len(queue):
        return _ask_for_bulk_address(
            lines, queue, pos + 1, conversation, user_context, page, start_time
        )

    user_context.pop("bulk_address_queue", None)
    user_context.pop("bulk_address_pos", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    return _continue_after_addresses_chosen(
        lines, store_loader, conversation, user_context, page, start_time
    )

def _continue_after_addresses_chosen(lines_as_dicts, store_loader, conversation, user_context, page, start_time):
    """
    Shared exit point after all blank-product slots are filled.
    Cleans up product-tracking keys, then checks for missing emails,
    variable products, and finally builds the confirmation response.
    """
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import (
        _build_bulk_confirmation_response,
        _prompt_for_quantity,
    )

    role = user_context.get("role", "")

    # Clean up product-slot tracking
    user_context.pop("bulk_product_missing_indices", None)
    user_context.pop("bulk_product_current_pos", None)
    
    # Quantity unset on any resolved line?       ← ADD
    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )

    # Email still missing on any line?
    if role in BULK_ORDER_FULL_SCOPE_ROLES:
        email_missing = [l for l in lines_as_dicts if l.get("unresolved_reason") == "email_not_provided"]
        if email_missing:
            conversation.flow_state = FlowState.AWAITING_BULK_EMAIL.value
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

            product_lines = "\r\n".join(
                f"• **{l['quantity']}× {l['product_name']}**" for l in email_missing
            )
            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Got it:\r\n{product_lines}\r\n\r\n"
                    "Please provide the customer's email address."
                ),
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

    # Variable products needing variant selection?
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

    # Everything the rep can supply is now supplied EXCEPT the address, so
    # collect that next — the summary comes after, not before.
    #
    # This used to render the summary here and start the address loop only
    # once the rep pressed "Yes, confirm". That asked them to approve a table
    # marked "Ready / N orders ready to place" while no address existed, and
    # made "Yes, confirm" mean "begin collecting addresses" rather than
    # "place the order". Address first, then a summary showing the real
    # destination, then a confirm that actually places.
    _resolved = [l for l in lines_as_dicts if not l.get("unresolved")]
    if _resolved:
        user_context["bulk_current_line_index"] = 0
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            _resolved, 0, conversation, user_context, page, start_time,
        )

    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )

# ══════════════════════════════════════════════════════════════
# ── Function 5: handle_bulk_address_confirmation_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_address_confirmation_reply(action, message, conversation, user_context, page, start_time):
    """
    Routes address-step actions during AWAITING_BULK_ADDRESS_CONFIRMATION.
    action is one of:
        "bulk_address_confirmed"         — use the address shown
        "bulk_address_change"            — user wants to type a different address (legacy)
        "bulk_address_override_text"     — user has typed a new address (legacy)
        "bulk_address_override_structured" — user saved edited billing+shipping via the panel
        "bulk_address_skip"              — skip this order entirely
    Returns a Flask response.
    """
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import _create_all_confirmed_orders

    lines = user_context.get("pending_bulk_lines", [])
    resolved_lines = [l for l in lines if not l["unresolved"]]
    idx = user_context.get("bulk_current_line_index", 0)

    # Step 1: All lines already processed
    if idx >= len(resolved_lines):
        return _create_all_confirmed_orders(user_context, conversation, page, start_time)

    current_line = resolved_lines[idx]

    # Step 3: Address confirmed — use address as-is
    if action == "bulk_address_confirmed":
        # ── Validation gate ──
        # POST /wc/v3/orders performs no address validation of its own, so this
        # is the only thing standing between "Confirm" on a card reading "No
        # address on file" and a live order with an empty billing block.
        billing, shipping = _effective_address_for_line(
            current_line,
            user_context.get("bulk_address_overrides", {}),
            idx,
            user_context.get("rep_email", ""),
            user_context.get("rep_billing_address"),
            user_context=user_context,
        )
        errors = validate_bulk_address(billing, shipping, get_required_fields())
        if has_errors(errors):
            return _reprompt_address_with_errors(
                resolved_lines, idx, conversation, user_context, page, start_time, errors,
            )

        current_line["address_confirmed"] = True
        _propagate_address_decision(resolved_lines, idx, user_context, "address_confirmed")
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 3b: Structured save from the inline edit panel.
    # message is "__BULK_ADDR__<json>" with {"billing": {...}, "shipping": {...}}.
    # Override is keyed by line index so repeated companies stay independent.
    elif action == "bulk_address_override_structured":
        raw = message.strip()
        if raw.startswith("__BULK_ADDR__"):
            raw = raw[len("__BULK_ADDR__"):]
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError) as exc:
            logger.warning(
                f"bulk_address_override_structured | bad JSON | error={exc} | raw={raw[:200]!r}"
            )
            parsed = {}

        edited_billing = parsed.get("billing") or {}
        edited_shipping = parsed.get("shipping") or {}

        # Persist the override BEFORE validating, so a rejected save doesn't
        # throw away what the rep just typed — the re-prompt prefills from the
        # effective address, which reads through this override.
        overrides = user_context.setdefault("bulk_address_overrides", {})
        overrides[str(idx)] = {
            "billing": edited_billing,
            "shipping": edited_shipping,
        }
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        # ── Validation gate ──
        billing, shipping = _effective_address_for_line(
            current_line, overrides, idx, user_context.get("rep_email", ""),
            user_context.get("rep_billing_address"),
            user_context=user_context,
        )
        errors = validate_bulk_address(billing, shipping, get_required_fields())
        if has_errors(errors):
            return _reprompt_address_with_errors(
                resolved_lines, idx, conversation, user_context, page, start_time, errors,
            )

        current_line["address_confirmed"] = True
        _propagate_address_decision(resolved_lines, idx, user_context, "address_confirmed")
        user_context.pop("bulk_awaiting_address_text", None)
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 4: Rep wants to change the address — re-show the card so the inline
    # edit panel is the entry point.
    #
    # This used to set bulk_awaiting_address_text and ask the rep to type a
    # free-text address. That path could not produce a valid address by
    # construction — it wrote the whole typed string into address_1 and left
    # city/state/postcode/country empty — so with required-field validation in
    # place it would reject every time. The structured panel is now the only
    # edit surface.
    elif action == "bulk_address_change":
        user_context.pop("bulk_awaiting_address_text", None)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _build_address_card_response(
            resolved_lines, idx, conversation, user_context, page, start_time,
        )

    # Step 5: Legacy free-text override.
    #
    # Retired (see Step 4) but kept reachable so any session already in the
    # bulk_awaiting_address_text sub-state when this shipped can still complete
    # rather than dead-ending. It runs through the same validation gate as every
    # other path, so it cannot create a blank-address order; in practice it will
    # reject and route the rep to the panel.
    elif action == "bulk_address_override_text":
        user_context.pop("bulk_awaiting_address_text", None)
        overrides = user_context.setdefault("bulk_address_overrides", {})
        overrides[str(idx)] = {
            "shipping": {
                "address_1": message.strip(),
            },
        }
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        billing, shipping = _effective_address_for_line(
            current_line, overrides, idx, user_context.get("rep_email", ""),
            user_context.get("rep_billing_address"),
            user_context=user_context,
        )
        errors = validate_bulk_address(billing, shipping, get_required_fields())
        if has_errors(errors):
            return _reprompt_address_with_errors(
                resolved_lines, idx, conversation, user_context, page, start_time, errors,
            )

        current_line["address_confirmed"] = True
        _propagate_address_decision(resolved_lines, idx, user_context, "address_confirmed")
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 6: Skip this customer's order
    elif action == "bulk_address_skip":
        current_line["address_skipped"] = True
        _propagate_address_decision(resolved_lines, idx, user_context, "address_skipped")
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Unexpected action — fall through safely
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": "Please reply **Yes** to confirm the address, **Change address** to update it, or **Skip** to skip this order.",
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm", "Change address", "Skip this order"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Function 6: _advance_to_next_address_confirmation (private) ──
# ══════════════════════════════════════════════════════════════

def _address_identity_key(line, line_idx, user_context):
    """
    Identity used to decide that two lines are the same delivery.

    Deliberately mirrors the grouping key in _create_all_confirmed_orders:
    customer_id AND recipient name AND destination. customer_id alone is not
    enough — in address-only mode every line carries customer_id=None, so two
    different people would look identical and one person's confirmation would
    silently settle another's address.

    The address is computed WITHOUT per-line overrides so a line the rep has
    just edited still matches its unedited siblings; the override is copied
    across separately by _propagate_address_decision.
    """
    _billing, _shipping = _effective_address_for_line(
        line, {}, line_idx, user_context.get("rep_email", ""),
        user_context.get("rep_billing_address"),
        user_context=user_context,
    )
    return (
        str(line.get("customer_id")),
        (
            line.get("recipient_name")
            or line.get("customer_display_name")
            or ""
        ).strip().lower(),
        _address_group_key(_billing),
        _address_group_key(_shipping),
    )

def _propagate_address_decision(resolved_lines, idx, user_context, field):
    """
    Apply this line's address decision to every other unsettled line for the
    same recipient and destination, so the rep confirms an address ONCE per
    person instead of once per line.

    Ordering "1 each of A, B, C, D for one person" asked the rep to confirm
    the identical address four times, then merged all four into a single order
    anyway. Lines that share an identity key are exactly the lines that will be
    merged, so settling them together cannot change what gets created.

    Returns the number of sibling lines settled.
    """
    src = resolved_lines[idx]
    _has_identity = bool(
        str(src.get("customer_id") or "").strip()
        or (
            src.get("recipient_name") or src.get("customer_display_name") or ""
        ).strip()
    )
    if not _has_identity:
        # Nothing to prove two lines are the same person — confirm one at a
        # time rather than risk settling a stranger's address.
        return 0

    src_key = _address_identity_key(src, idx, user_context)
    overrides = user_context.get("bulk_address_overrides", {}) or {}
    src_override = overrides.get(str(idx))

    settled = 0
    for j, line in enumerate(resolved_lines):
        if j == idx or line.get("address_confirmed") or line.get("address_skipped"):
            continue
        if _address_identity_key(line, j, user_context) != src_key:
            continue
        line[field] = True
        if src_override is not None:
            # Overrides are keyed by line index, so an inline edit must be
            # copied onto each sibling index too — otherwise those lines would
            # quietly fall back to the address on file while the rep believes
            # they confirmed the edited one.
            user_context.setdefault("bulk_address_overrides", {})[str(j)] = json.loads(
                json.dumps(src_override)
            )
        settled += 1

    if settled:
        logger.info(
            f"bulk_order | {field} on line {idx} applied to {settled} other "
            f"line(s) for the same recipient — not asking again"
        )
    return settled

def _advance_to_next_address_confirmation(resolved_lines, idx, conversation, user_context, page, start_time):
    """
    Walk forward to the next line still needing address confirmation and show
    its card. When every line has been confirmed or skipped, place the orders.
    """
    # Deferred import: lives in bulk_order_handler, which imports this
    # module — a top-level import here would be circular.
    from handlers.bulk_order_handler import (
        _build_bulk_cart_response,
        _build_bulk_confirmation_response,
    )

    # An order with nothing but self-scoped lines goes to the CART, not to
    # order creation — same rule and same reasoning as the fork in
    # _build_bulk_confirmation_response (see its comment for the full
    # rationale, including why this is gated on is_self_order rather than
    # role). That fork alone was NOT enough: this function is called
    # from 8 separate sites (trigger parse, quantity reply, variant reply,
    # and several address-flow continuations), and the "every route funnels
    # through _build_bulk_confirmation_response" comment there stopped being
    # true the moment any of those sites started calling this function
    # directly instead. A customer whose order needed a variant prompt
    # reached this function with every line still unconfirmed and got shown
    # a "No address on file — 18 required fields missing" card before the
    # role check downstream ever ran.
    #
    # Checked here, at the top, so it fires before EITHER branch below ever
    # builds an address-confirmation card — not just in the terminal branch
    # that already happened to call _build_bulk_confirmation_response.
    _live_lines = [
        l for l in resolved_lines
        if not l.get("unresolved") and not l.get("address_skipped")
    ]
    _all_self_order = bool(_live_lines) and all(l.get("is_self_order") for l in _live_lines)
    if _all_self_order:
        lines = user_context.get("pending_bulk_lines", [])
        return _build_bulk_cart_response(
            lines, conversation, user_context, page, start_time
        )

    # Step 1: Skip already-processed lines
    while idx < len(resolved_lines):
        line = resolved_lines[idx]
        if not line.get("address_confirmed") and not line.get("address_skipped"):
            break
        idx += 1

    if idx >= len(resolved_lines):
        # Every address settled — NOW show the summary. It can finally state a
        # real recipient and destination, and its confirm button places the
        # orders rather than starting another data-collection step.
        lines = user_context.get("pending_bulk_lines", [])
        return _build_bulk_confirmation_response(
            lines, conversation, user_context, page, start_time,
        )

    return _build_address_card_response(
        resolved_lines, idx, conversation, user_context, page, start_time,
    )

def _reprompt_address_with_errors(
    resolved_lines, idx, conversation, user_context, page, start_time, errors
):
    """
    Re-show the SAME line's address card after validation rejected it.

    Deliberately does NOT advance bulk_current_line_index and does NOT set
    address_confirmed — the rep stays on this line until the address is valid or
    they skip it.
    """
    return _build_address_card_response(
        resolved_lines, idx, conversation, user_context, page, start_time,
        validation_errors=errors,
    )

def _build_address_card_response(
    resolved_lines, idx, conversation, user_context, page, start_time,
    validation_errors=None,
):
    """
    Build the SHOW_BULK_ADDRESS_CONFIRMATION card for resolved_lines[idx].

    Shared by the normal advance path and the validation re-prompt so the two
    can't drift. When `validation_errors` is supplied the card is rendered in
    its blocked form: the errors ride along in the payload, the bot message
    names what's missing, and "Yes, confirm" is REMOVED from the suggestion
    chips.

    That last part matters: conversation_flow.py maps any reply matching
    yes|yeah|confirm|ok|sure|correct to the bulk_address_confirmed action, so
    leaving the chip on screen would invite the rep straight back into the
    rejection they just hit.
    """
    current_line = resolved_lines[idx]
    user_context["bulk_current_line_index"] = idx
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value

    # The shipping block drives the read-only summary line on the card.
    shipping_block = current_line.get("shipping_address") or {}
    billing_block = current_line.get("billing_address") or {}

    # Address-only mode (company has delivery history but no customer
    # account) leaves customer_id None. int(None) raised here and killed
    # the whole card — which is how a chosen address showed up blank.
    _cid = current_line.get("customer_id")
    if not shipping_block.get("address_1") and _cid:
        try:
            cust_call = endpoints.fetch_customer(
                customer_id=int(_cid),
                description=f"Fetch address for {current_line['customer_display_name']}",
            )
            cust_result = woo_client.execute(cust_call)
            if cust_result.get("success") and isinstance(cust_result.get("data"), dict):
                data = cust_result["data"]
                fetched_billing = data.get("billing", {}) or {}
                fetched_shipping = data.get("shipping", {}) or {}
                # Shipping block drives this confirmation card; fall back to
                # billing if the customer has no shipping address on file.
                shipping_block = fetched_shipping if fetched_shipping.get("address_1") else fetched_billing
                current_line["shipping_address"] = shipping_block
                # Populate the billing block too, so order creation has both.
                if not billing_block.get("address_1"):
                    billing_block = fetched_billing or shipping_block
                    current_line["billing_address"] = billing_block
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
        except Exception as exc:
            logger.warning(
                f"_build_address_card_response | failed to fetch address "
                f"for customer_id={current_line.get('customer_id')} | error={exc}"
            )

    # Historical shipping-email fallback. WooCommerce customer records (the
    # fetch above) have no email on the shipping block — only ORDER HISTORY
    # captures the store's custom Shipping Email field (the plugin's
    # /company-order-addresses returns it per row as shipping_email, read
    # from order meta _shipping_email). The address-choice picker already
    # carries this across for a customer with 2+ historical addresses; this
    # covers the common 0-or-1 case, which skips that picker entirely and
    # would otherwise leave email permanently blank until typed by hand.
    #
    # The recipient's WooCommerce ACCOUNT email is deliberately NOT used as a
    # fallback: Shipping Email can legitimately differ from it (a receiving
    # desk inbox), so a wrong-but-plausible address would be worse than a
    # blank one the rep is told about. Blank + a warning is the agreed
    # behaviour.
    email_miss_reason = ""
    if not shipping_block.get("email"):
        _company = user_context.get("bulk_company_scope", "")
        if not _company:
            email_miss_reason = "no_company"
        else:
            _hist_rows = _company_order_addresses(_company, user_context)
            # Match on the recipient's NAME. A row's customer_id is the person
            # who PLACED that order, which for anything a rep entered is the
            # rep — so it answers a different question than the one being
            # asked here and is not used at all.
            #
            # It used to be a fallback, and could return the WRONG PERSON'S
            # email: it matched rows placed BY the recipient, so if they had
            # ever ordered for a colleague at this company, that colleague's
            # shipping email was stamped on this order unverified. Rare, silent
            # and unchecked afterwards — removed.
            #
            # Must require shipping_email in the match predicate itself, not
            # just identity — otherwise this locks onto the person's single
            # most recent order even when THAT one has a blank email, and
            # gives up instead of walking back to an older order that has
            # one. Rows are already date-DESC from the plugin, so the first
            # row satisfying both conditions is the most recent one with data.
            _fn = _norm_name(shipping_block.get("first_name"))
            _ln = _norm_name(shipping_block.get("last_name"))
            _match = None
            _tier = ""

            if _fn or _ln:
                # Tier 1 — exact on both names, after normalisation.
                _match = next(
                    (
                        r for r in _hist_rows
                        if _norm_name(r.get("shipping_first_name")) == _fn
                        and _norm_name(r.get("shipping_last_name")) == _ln
                        and r.get("shipping_email")
                    ),
                    None,
                )
                _tier = "exact" if _match else ""

            if not _match and _ln and _fn:
                # Tier 2 — surname exact, forename initial. Catches a middle
                # name sitting in first_name, an initial instead of a name,
                # and "Jacquelyn"/"Jacqueline"-style spelling drift.
                #
                # Stops here on purpose: general fuzzy matching on a person's
                # name is not safe at this scale, because two J. Smiths at one
                # firm is entirely plausible and a wrong email is worse than
                # the miss it would save.
                _match = next(
                    (
                        r for r in _hist_rows
                        if _norm_name(r.get("shipping_last_name")) == _ln
                        and _norm_name(r.get("shipping_first_name"))[:1] == _fn[:1]
                        and r.get("shipping_email")
                    ),
                    None,
                )
                _tier = "surname+initial" if _match else ""

            if not _match:
                email_miss_reason = (
                    "no_history" if not _hist_rows else "no_email_in_history"
                )

            logger.info(
                f"bulk_order | shipping-email fallback for "
                f"{current_line.get('customer_display_name', '') or 'unknown'} "
                f"| company={_company!r} rows={len(_hist_rows)} "
                f"| found={_tier or 'no'}"
                + (f" reason={email_miss_reason}" if not _match else "")
            )
            if _match:
                shipping_block["email"] = _match["shipping_email"]
                current_line["shipping_address"] = shipping_block
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")

    # Prefill from the EFFECTIVE address, not the raw base blocks, so a rep who
    # saved a partial edit and got rejected sees their own values back in the
    # panel instead of the original ones.
    effective_billing, effective_shipping = _effective_address_for_line(
        current_line,
        user_context.get("bulk_address_overrides", {}),
        idx,
        user_context.get("rep_email", ""),
        user_context.get("rep_billing_address"),
        user_context=user_context,
    )

    addr_parts = [
        effective_shipping.get("address_1", ""),
        effective_shipping.get("address_2", ""),
        effective_shipping.get("city", ""),
        effective_shipping.get("state", ""),
        effective_shipping.get("postcode", ""),
    ]
    addr_str = ", ".join(p for p in addr_parts if p) or "No address on file"

    items_text = f"{current_line['product_name']} ×{current_line['quantity']}"
    if current_line.get("is_reorder"):
        items_text = f"[Reorder] {items_text}"

    # Full field set so the inline edit panel can prefill every field.
    # Billing carries the CS custom fields; shipping carries order_notes.
    _BILLING_FIELDS = (
        "first_name", "last_name", "company",
        "billing_field_type", "billing_project",
        "address_1", "address_2", "city", "state", "postcode", "country",
        "phone", "email", "project_rep",
    )
    _SHIPPING_FIELDS = (
        "first_name", "last_name", "company",
        "address_1", "address_2", "city", "state", "postcode", "country",
        "email", "order_notes",
    )

    def _pick(block, fields):
        return {f: (block or {}).get(f, "") for f in fields}

    billing_payload = _pick(effective_billing, _BILLING_FIELDS)
    shipping_payload = _pick(effective_shipping, _SHIPPING_FIELDS)

    # project_rep is already defaulted to the logged-in rep inside
    # _effective_address_for_line, so the dropdown arrives pre-selected.

    # ▼ emit a structured action so React can render the address card + panel
    payload = {
        "customer_name": _line_recipient_display(current_line),
        "items_text": items_text,
        # Legacy read-only summary fields (kept for back-compat).
        "address": {
            "address_1": effective_shipping.get("address_1", ""),
            "address_2": effective_shipping.get("address_2", ""),
            "city":      effective_shipping.get("city", ""),
            "state":     effective_shipping.get("state", ""),
            "postcode":  effective_shipping.get("postcode", ""),
        },
        "addr_str": addr_str,
        # Full structured blocks for the editable panel prefill.
        "billing": billing_payload,
        "shipping": shipping_payload,
        "progress": {"current": idx + 1, "total": len(resolved_lines)},
    }
    if has_errors(validation_errors):
        payload["validation_errors"] = validation_errors

    address_action = {
        "type": "SHOW_BULK_ADDRESS_CONFIRMATION",
        "payload": payload,
    }

    header = (
        f"**Order for {_line_recipient_display(current_line)}** "
        if not current_line.get("is_self_order") else "**Your order** "
    ) + f"({idx + 1} of {len(resolved_lines)})\r\n\r\n"

    # A blank Shipping Email otherwise only surfaces AFTER the rep hits
    # confirm, as a generic "missing N required fields" rejection. We already
    # know here both that it is blank and why, so say so up front and name the
    # reason — the rep can act on "nothing on file for this person" but not on
    # "field missing".
    email_notice = ""
    if not effective_shipping.get("email"):
        _who = _line_recipient_display(current_line)
        _co = user_context.get("bulk_company_scope", "")
        if email_miss_reason == "no_company":
            email_notice = (
                "✉️ No Shipping Email — this order has no company scope, so "
                "there is no order history to look one up in. Please add it below."
            )
        elif email_miss_reason == "no_history":
            email_notice = (
                f"✉️ No Shipping Email — no past deliveries on file for "
                f"**{_co}** to take one from. Please add it below."
            )
        elif email_miss_reason == "no_email_in_history":
            email_notice = (
                f"✉️ No Shipping Email — none of **{_who}**'s past orders at "
                f"**{_co}** recorded one. Please add it below."
            )
        else:
            email_notice = "✉️ No Shipping Email on file. Please add it below."

    # Clauses the user typed that could NOT be validated ("rep John Smith"
    # when no such rep exists, an unrecognised order type). Shown on the card
    # BEFORE confirm, next to the missing-field list, so the reason a field is
    # blank is visible at the moment it can be fixed. Shown in BOTH branches:
    # an unusable clause is exactly what tends to leave a required field empty
    # and land the rep in the rejection branch.
    _clause_notices = (user_context or {}).get("bulk_field_clause_notices") or []
    clause_notice = "\r\n".join(_clause_notices)

    if has_errors(validation_errors):
        missing_count = count_missing(validation_errors)
        bot_message = (
            header
            + f"📦 {items_text}\r\n"
            + f"📍 Shipping to: {addr_str}\r\n\r\n"
            + f"⚠️ This order is missing {missing_count} required "
            + ("field" if missing_count == 1 else "fields")
            + f": {format_missing_fields(validation_errors)}.\r\n\r\n"
            + (f"{clause_notice}\r\n\r\n" if clause_notice else "")
            + "Please update the address, or skip this order."
        )
        suggestions = ["Change address", "Skip this order"]
        logger.info(
            f"bulk_order | address validation blocked line {idx} "
            f"({current_line.get('customer_display_name')}) | "
            f"missing={format_missing_fields(validation_errors)}"
        )
    else:
        bot_message = (
            header
            + f"📦 {items_text}\r\n"
            + f"📍 Shipping to: {addr_str}\r\n\r\n"
            + (f"{email_notice}\r\n\r\n" if email_notice else "")
            + (f"{clause_notice}\r\n\r\n" if clause_notice else "")
            + "Confirm this address?"
        )
        suggestions = ["Yes, confirm", "Change address", "Skip this order"]

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": suggestions,
        "actions": [address_action],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200

def _address_group_key(addr) -> str:
    """
    Canonical key for "is this the same destination?".
    
    Only the fields that define a destination, normalised. Hashing the whole
    dict split lines that ship to the identical address purely because the two
    code paths that build them differ in shape — the parser's roster entry
    carries an empty "phone" key and the recipient-reply path does not, so one
    person's two products became two orders.
    """
    a = addr or {}
    fields = (
        "first_name", "last_name", "company",
        "address_1", "address_2", "city", "state", "postcode", "country",
    )
    return "|".join(
        re.sub(r"\s+", " ", str(a.get(f) or "")).strip().lower()
        for f in fields
    )

def _norm_name(value) -> str:
    """
    Normalise a person's name for comparison against order-history rows.

    Case-folded, accents removed, and every non-word character INCLUDING
    whitespace dropped. Removing spaces as well as punctuation is what makes
    both directions work: "Smith-Jones"/"smith jones" and "O'Brien"/"o brien"
    each collapse to one form, where stripping punctuation alone fixes one and
    breaks the other. Also folds "Van Der Berg"/"Vanderberg".

    Used only to compare two spellings of the same name, never to display one.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w]", "", text).lower()