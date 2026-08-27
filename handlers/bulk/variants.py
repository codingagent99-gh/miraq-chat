"""Variant/axis selection for the bulk-order flow.

Split verbatim out of handlers/bulk_order_handler.py — pure move, no logic
changes. Function bodies are byte-identical to what they replaced; the only
additions are this module's imports and three deferred imports (marked
below) that exist solely to break the import cycle back into the handler.
"""

import time
import re
import unicodedata

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from app_config import ECOMMERCE_BACKEND
from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination, _get_safe_options

logger = get_logger("miraq_chat")

# ══════════════════════════════════════════════════════════════
# ── Helper: detect variable products ──
# ══════════════════════════════════════════════════════════════

def _is_variable_product(product_id: int, store_loader) -> bool:
    """Return True if the catalog entry for product_id has variations."""
    if not store_loader or not product_id:
        return False
    for p in (store_loader.products or []):
        if p.get("id") == product_id:
            return bool(p.get("variations")) or p.get("type") == "variable"
    return False

# ══════════════════════════════════════════════════════════════
# ── Private: _ask_for_bulk_variant ──
# ══════════════════════════════════════════════════════════════

def _parent_axis_meta(product_id, user_context):
    """
    Every axis the PARENT product marks as used-for-variations, with the
    taxonomy slug and attribute id alongside the option names:

        {"Sample Size": {"taxonomy": "pa_sample-size", "attribute_id": 11,
                         "options": ["Chip Card", '12"x12"']}}

    This is the only reliable source for WooCommerce "Any" axes. wc/v3's
    variations endpoint OMITS an attribute a variation leaves as Any rather
    than returning it blank, so a variation cannot be inspected to discover
    what it failed to specify — Adams 13544 comes back carrying only Colors,
    with no trace of Finish or Sample Size. Comparing against the parent is
    what surfaces them.

    The taxonomy slug and attribute id are kept because line-item meta has to
    be written under the TAXONOMY key ("pa_sample-size"), not the display name
    ("Sample Size") — see _variant_meta_entry.
    """
    cache = user_context.setdefault("bulk_parent_axis_cache", {})
    key = str(product_id)
    cached = cache.get(key)
    # Sessions persisted before this cache carried taxonomy/id hold the old
    # {name: [options]} shape. Treat those as a miss and refetch rather than
    # projecting garbage out of a list.
    if isinstance(cached, dict) and all(
        isinstance(v, dict) and "options" in v for v in cached.values()
    ):
        return cached

    axes = {}
    fetched = False

    # Shopify variants are fully-specified option combinations — there is no
    # "Any", so there are no blank axes for this function to discover and
    # nothing to compare a variation against. endpoints.product_variation_
    # taxonomies already returns None on Shopify for the same reason, so
    # every caller downstream is built to cope with an empty result.
    #
    # Returning early rather than letting the call through: woo_client
    # refuses every request on a Shopify deployment by design, so this would
    # otherwise be a guaranteed-failed fetch per product per prompt, logged
    # as a warning each time. The empty dict IS the correct answer here, not
    # a degraded one, so it is cached like any other success.
    if ECOMMERCE_BACKEND == "shopify":
        cache[key] = axes
        user_context["bulk_parent_axis_cache"] = cache
        return axes

    try:
        call = endpoints.fetch_product(
            product_id=product_id,
            description=f"Fetch parent attributes for product_id={product_id}",
        )
        res = woo_client.execute(call)
        data = res.get("data") if res.get("success") else None
        if isinstance(data, dict):
            for a in data.get("attributes", []) or []:
                if not isinstance(a, dict) or not a.get("variation"):
                    continue
                name = str(a.get("name") or "").strip()
                opts = [str(o) for o in (a.get("options") or []) if str(o).strip()]
                if name and opts:
                    axes[name] = {
                        "taxonomy": str(a.get("slug") or "").strip(),
                        "attribute_id": a.get("id"),
                        "options": opts,
                    }
            fetched = True
    except Exception as exc:
        logger.warning(
            f"bulk_order | parent attribute fetch failed for {product_id} | error={exc}"
        )

    # Success only — a cached failure would disable the prompt for the session.
    if fetched:
        cache[key] = axes
        user_context["bulk_parent_axis_cache"] = cache
    return axes

def _parent_variation_axes(product_id, user_context):
    """
    {axis name: [option names]} — the shape every prompt-building caller wants.
    Thin projection of _parent_axis_meta so both share one fetch and one cache.
    """
    return {
        name: meta["options"]
        for name, meta in _parent_axis_meta(product_id, user_context).items()
    }

def _missing_variant_axes(line, user_context):
    """
    Parent variation axes the line's matched variation does NOT pin down.

    Returns [] for a line with no variation yet (the normal prompt covers it)
    and for a fully specified one.
    """
    product_id = line.get("product_id")
    if not product_id or not line.get("variation_id"):
        return []
    parent = _parent_variation_axes(product_id, user_context)
    if not parent:
        return []
    specified = {
        str(a).strip().lower()
        for a in (line.get("specified_variant_axes") or [])
    }
    return [name for name in parent if name.strip().lower() not in specified]

def _parent_any_axis_options(product_id, axis_names, user_context):
    """Options for a named subset of the parent's variation axes."""
    if not axis_names:
        return {}
    parent = _parent_variation_axes(product_id, user_context)
    wanted = {a.strip().lower() for a in axis_names if a}
    return {
        name: opts for name, opts in parent.items()
        if name.strip().lower() in wanted
    }

def _ensure_missing_axes(line, user_context):
    """
    True when the line has a variation that leaves parent axes unset, and
    stamps them onto the line so the prompt knows what to ask.
    """
    # A self-contained sample form (Chip Card) leaves the other axes as
    # WooCommerce "Any" by design — they are not applicable, not missing.
    if line.get("self_contained_variant"):
        return False
    if line.get("blank_variant_axes"):
        return True
    missing = _missing_variant_axes(line, user_context)
    if missing:
        line["blank_variant_axes"] = missing
        logger.info(
            f"bulk_order | product {line.get('product_id')} variation "
            f"{line.get('variation_id')} leaves {missing} unset — will ask"
        )
        return True
    return False

def _ask_for_bulk_variant(
    lines_as_dicts, needs_variant_indices, pos,
    conversation, user_context, page, start_time,
):
    line_idx = needs_variant_indices[pos]
    line = lines_as_dicts[line_idx]
    product_id = line["product_id"]

    cache = user_context.setdefault("bulk_variant_cache", {})
    cache_key = str(product_id)

    if cache_key not in cache:
        from store_registry import get_store_loader

        # Backend-neutral (see the Protocol): Woo fetches over REST, Shopify
        # reads what the store loader already holds. The direct
        # woo_client.execute() this replaces was refused on a Shopify
        # deployment, so the prompt rendered with zero options.
        raw = endpoints.list_variants_resolved(
            product_id=product_id,
            store_loader=get_store_loader(),
            per_page=100,
        )
        if raw is None:
            # Undetermined — do NOT write [] into the cache. This cache lives
            # in conversation.context_data, so caching a failed lookup as
            # "no variations" would poison it for the rest of the
            # conversation and the shopper would be re-prompted with an empty
            # option list every time. Falling through with an empty local
            # list leaves the next turn free to retry.
            logger.warning(
                f"bulk_order | variation lookup undetermined for "
                f"product_id={product_id} — prompting without cached options"
            )
        else:
            cache[cache_key] = raw
            user_context["bulk_variant_cache"] = cache
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

    variations = cache.get(cache_key, [])

    # When the hint narrowed to several variations ("Harmony Moon" in five
    # sizes), offer only those. Using every variation would re-ask the
    # colour the rep already gave and list sizes that do not exist in it.
    _candidates = line.get("candidate_variation_ids") or []
    if _candidates:
        _narrowed = [v for v in variations if v.get("id") in _candidates]
        if _narrowed:
            variations = _narrowed

    attr_axes: dict = {}
    for var in variations:
        # _get_safe_options normalises both the custom flat-dict shape and
        # the standard WC list-of-dicts shape, and drops blank options.
        for name, option in _get_safe_options(var.get("attributes", [])).items():
            if name and option:
                attr_axes.setdefault(name, set()).add(option)

    # Axes the matched variation leaves as "Any" carry no value on ANY
    # variation, so attr_axes above can't see them — their options live on the
    # parent product. The storefront makes the shopper pick these, so the rep
    # must be asked too.
    #
    # blank_variant_axes is only populated once a variation has been resolved
    # and inspected. On the FIRST prompt for a line that has no variation yet
    # it is empty, so this used to add nothing and the rep was asked for
    # Colors alone — then asked again for Finish and Sample Size the moment a
    # colour pinned a variation. Two prompts for one product, the first one
    # visibly incomplete.
    #
    # Derive them here instead: any parent variation axis that NO variation
    # sets is an "Any" axis by definition, which is exactly what
    # _missing_variant_axes would conclude later. Products whose variations
    # encode every axis produce an empty list and are unaffected.
    _blank_axes = list(line.get("blank_variant_axes") or [])
    if not _blank_axes:
        _seen = {k.strip().lower() for k in attr_axes}
        _blank_axes = [
            n for n in _parent_variation_axes(product_id, user_context)
            if n.strip().lower() not in _seen
        ]
    _any_axes = _parent_any_axis_options(product_id, _blank_axes, user_context)
    for _name, _opts in _any_axes.items():
        attr_axes.setdefault(_name, set()).update(_opts)

    # EVERY axis stays on the prompt, including ones the matched variation
    # already pins — the rep may still want to change the colour. What their
    # message settled rides along in `preselected` so the UI can pre-tick it
    # rather than making them find and re-pick a value they already gave.
    attributes = [
        {"name": name, "options": sorted(opts)}
        for name, opts in attr_axes.items()
    ]

    _resolved_axes = {}
    if line.get("variation_id"):
        _matched_var = next(
            (v for v in variations if v.get("id") == line.get("variation_id")), None
        )
        if _matched_var:
            _resolved_axes = {
                n: o
                for n, o in _get_safe_options(_matched_var.get("attributes", [])).items()
                if n and o
            }
    else:
        # No single variation pinned, but the rep's terms may still have
        # settled some axes: if EVERY surviving candidate carries the same
        # value on an axis, that axis is decided no matter which candidate
        # they end up on.
        #
        # This is how Adams differs from Allspice and Tara. Adams's variations
        # DO enumerate 12"x12", so the term matched and narrowed 32 → 5 — which
        # meant it never became an "unmatched" term and the pre-selection above
        # had nothing to work with, even though all 5 survivors share that size
        # AND the Matte finish. Allspice and Tara took the other route because
        # their Sample Size axis is "Any" on the variations.
        #
        # Both routes end in the same place: the rep is not asked again for
        # something they already said.
        _cands = [v for v in variations
                  if v.get("id") in set(line.get("candidate_variation_ids") or [])]
        if len(_cands) > 1:
            _per_axis = {}
            for _v in _cands:
                for _n, _o in _get_safe_options(_v.get("attributes", [])).items():
                    if _n and _o:
                        _per_axis.setdefault(_n, set()).add(_o)
            _agreed = {n: next(iter(vals)) for n, vals in _per_axis.items()
                       if len(vals) == 1}
            if _agreed:
                _resolved_axes = dict(_agreed)
                logger.info(
                    f"bulk_order | product {product_id}: {len(_cands)} candidate "
                    f"variations agree on {_agreed} — treating as settled"
                )

    # Terms the rep typed that matched no VARIATION, because that axis is
    # "Any" on the variations and its options live on the parent — the
    # _any_axes just merged in above. The parser could not use them to narrow,
    # so without this they were logged and dropped, and the prompt asked for a
    # size the rep had already given (and a genuinely wrong term vanished
    # silently). Resolve each against the merged axis options and pre-select
    # the ones that check out.
    # Compare on a key that drops EVERY non-alphanumeric, not _slugify:
    # _slugify keeps hyphens, so '5"x10"' and '5" x 10"' produce "5x10" vs
    # "5-x-10" and would not match. This mirrors the parser's own
    # _normalize_term_key, so both sides agree on what "the same option"
    # means — the catalog writes these sizes both ways.
    def _optkey(s):
        return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

    _leftover = [t for t in (line.get("unmatched_variant_terms") or []) if str(t).strip()]
    _still_bad = []
    for _term in _leftover:
        _tk = _optkey(_term)
        _hit = None
        for _axis_name, _opts in attr_axes.items():
            for _opt in _opts:
                if _tk and _tk == _optkey(_opt):
                    _hit = (_axis_name, _opt)
                    break
            if _hit:
                break
        if _hit and _hit[0] not in _resolved_axes:
            _resolved_axes[_hit[0]] = _hit[1]
            logger.info(
                f"bulk_order | pre-selected {_hit[0]}='{_hit[1]}' for product "
                f"{product_id} from the rep's own wording ('{_term}')"
            )
        elif not _hit:
            _still_bad.append(_term)

    # Anything that matched no option anywhere is a real mistake. Naming it
    # keeps it from being silently ignored — the same rule the
    # unmatched_variant_hint message below follows.
    if _still_bad:
        logger.warning(
            f"bulk_order | product {product_id}: terms {_still_bad} match no "
            f"option on any axis — surfacing to the rep"
        )

    # Use the same derived list the attribute options came from, so the
    # wording ("just need the Finish, Sample Size") matches what's rendered
    # even on the first prompt, where the line carries no blank_variant_axes.
    _open_axes = {a.strip().lower() for a in _blank_axes if a}

    variation_list = [
        {
            "id": var["id"],
            "attributes": _get_safe_options(var.get("attributes", [])),
        }
        for var in variations
    ]

    conversation.flow_state = FlowState.AWAITING_BULK_VARIANT_SELECTION.value

    # If the user named a variant we couldn't match (e.g. "Aurora Taupe" when
    # Aurora has no Taupe), say so explicitly and show what IS available —
    # otherwise the generic "select the missing details" prompt looks like we
    # ignored what they typed.
    _bad_hint = (line.get("unmatched_variant_hint") or "").strip()
    _hint_not_in_catalog = bool(_bad_hint) and not any(
        _bad_hint.lower() in opt.lower()
        for opts in attr_axes.values() for opt in opts
    )

    # Deferred import: _line_recipient_display stays in bulk_order_handler,
    # which imports this module — a top-level import here would be circular.
    from handlers.bulk_order_handler import _line_recipient_display

    # ▼ CHANGED: wrap data inside "payload" key
    action = {
        "type": "SHOW_BULK_VARIANT_PROMPT",
        "payload": {
            "line_index": line_idx,
            "company": _line_recipient_display(line),
            "is_self_order": line.get("is_self_order", False),
            "product_name": line.get("product_name", ""),
            "quantity": line.get("quantity", 0),
            "progress": {"current": pos + 1, "total": len(needs_variant_indices)},
            "attributes": attributes,
            # Axes already settled from the rep's message. The UI should show
            # these as chosen (read-only), not ask for them again.
            "preselected": _resolved_axes,
            "variations": variation_list,
            "unmatched_variant_hint": _bad_hint if _hint_not_in_catalog else "",
        },
    }

    _line_label = (
        f"{_line_recipient_display(line)} × {line.get('quantity', 0) or '?'}"
    )
    if _hint_not_in_catalog:
        _bot_message = (
            f"I couldn't find **{_bad_hint}** for **{line['product_name']}** — "
            f"that option isn't in the catalog. Please pick from the available "
            f"options instead ({_line_label}):"
        )
    elif _still_bad:
        # A term that matched no option on any axis. Said out loud rather
        # than dropped, so the rep is not left assuming a value they typed
        # was applied.
        _bad_list = ", ".join(f"**{t}**" for t in _still_bad)
        _bot_message = (
            f"I couldn't find {_bad_list} for **{line['product_name']}** — "
            f"please pick from the available options ({_line_label}):"
        )
    else:
        if _resolved_axes and _open_axes:
            _settled = ", ".join(f"{v}" for v in _resolved_axes.values())
            _bot_message = (
                f"**{line['product_name']}** — {_settled} is set. "
                f"Just need the {', '.join(line.get('blank_variant_axes') or [])} "
                f"({_line_label}):"
            )
        else:
            _bot_message = (
                f"Please select the missing product details for "
                f"**{line['product_name']}** ({_line_label}):"
            )

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": _bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Cancel"],
        "actions": [action],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_VARIANT_SELECTION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_VARIANT_SELECTION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_variant_selection_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_variant_selection_reply(
    message, store_loader, conversation, user_context, page, start_time
):
    """
    Called when the user replies during AWAITING_BULK_VARIANT_SELECTION.
    Scores each variation by how many of its attribute options appear in the
    message, stamps the best match as variation_id, then advances.
    Re-prompts via _ask_for_bulk_variant if no match is found.
    """
    # Deferred imports: both live in bulk_order_handler, which imports this
    # module — top-level imports here would be circular.
    from handlers.bulk_order_handler import (
        _advance_to_next_address_confirmation,
        _build_bulk_confirmation_response,
    )

    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    needs_variant_indices = user_context.get("bulk_variant_line_indices", [])
    pos = user_context.get("bulk_variant_current_pos", 0)

    # Guard — shouldn't be here
    if not needs_variant_indices or pos >= len(needs_variant_indices):
        return _build_bulk_confirmation_response(
            lines_as_dicts, conversation, user_context, page, start_time
        )

    line_idx = needs_variant_indices[pos]
    line = lines_as_dicts[line_idx]
    product_id = line["product_id"]
    cache = user_context.get("bulk_variant_cache", {})
    variations = cache.get(str(product_id), [])

    # Score: count how many attribute options from a variation appear in the message
    msg_lower = message.lower()
    best_match = None
    best_score = -1

    for var in variations:
        attrs = var.get("attributes", [])
        if not attrs:
            continue
        score = sum(
            1 for a in attrs if a.get("option", "").lower() in msg_lower
        )
        if score > best_score:
            best_score = score
            best_match = var

    if not best_match or best_score == 0:
        # Re-show the same prompt
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, pos,
            conversation, user_context, page, start_time,
        )

    # Stamp the resolved variation
    import re as _re

    line["variation_id"] = best_match["id"]

    # Picking a variation resolves the PRODUCT axis only — it says nothing
    # about whether a recipient was ever identified. Clearing `unresolved`
    # here unconditionally made a line's readiness depend on whether it
    # happened to need a variant prompt, which is how order #1066565 shipped
    # its two prompted lines and dropped its two chip-card lines.
    #
    # Every blocker is now cleared by the step that owns it, so by the time a
    # line reaches this prompt it should already be resolved: Step 5 skips
    # lines with no product_id, and the company / recipient / email / address
    # steps all run before it. If one still arrives unresolved, say so loudly
    # rather than promoting it into a live order.
    if line.get("unresolved"):
        logger.warning(
            f"bulk_order | line {line_idx} ({line.get('product_name')}) reached "
            f"the variant prompt still unresolved "
            f"(reason={line.get('unresolved_reason')}) — leaving it unresolved; "
            f"a variation choice does not identify a recipient"
        )

    # The variation just chosen may ITSELF leave parent axes unset — "Any"
    # axes that no single variation encodes. Detect that HERE, after stamping.
    # A line whose variation came from this prompt (rather than from a hint in
    # the original message) had no variation to inspect until now, so nothing
    # had ever computed its blank axes and Sample Size / Finish were dropped
    # from the order silently. Lines with a hint were checked earlier, which
    # is why only those ever asked.
    _ensure_missing_axes(line, user_context)

    # Record the rep's choice for any axis the variation itself can't encode.
    # These ride along as order line-item meta — exactly where WooCommerce
    # puts an "Any" attribute chosen on the product page. The reply often
    # already names them ("TOKYO Aegean Blue, Chip Card"), so harvest from
    # this same message before deciding anything is still missing.
    _pending_axes = line.get("blank_variant_axes") or []
    if _pending_axes:
        _opts = _parent_any_axis_options(product_id, _pending_axes, user_context)
        _meta = dict(line.get("variant_meta") or {})
        for _axis, _choices in _opts.items():
            for _choice in sorted(_choices, key=len, reverse=True):
                if _choice.lower() in msg_lower:
                    _meta[_axis] = _choice
                    break
        line["variant_meta"] = _meta
        line["blank_variant_axes"] = [
            a for a in _pending_axes
            if not any(a.strip().lower() == k.strip().lower() for k in _meta)
        ]
        logger.info(
            f"bulk_order | variant meta for line {line_idx} → {_meta} | "
            f"still pending={line['blank_variant_axes']}"
        )

    # The user has now chosen a real option — don't keep warning about the
    # hint that failed, or a re-prompt later would repeat a stale complaint.
    line["unmatched_variant_hint"] = ""

    # If quantity was not specified earlier, extract the last standalone
    # integer from the user's reply. Attribute options like "12x24" don't
    # produce bare word-boundary integers, so the last match is the qty.
    if not line.get("quantity") or not line.get("quantity_explicitly_set"):
        qty_matches = _re.findall(r'\b(\d+)\b', message)
        if qty_matches:
            line["quantity"] = int(qty_matches[-1])
            line["quantity_explicitly_set"] = True   # ← ADD
    
    lines_as_dicts[line_idx] = line
    user_context["pending_bulk_lines"] = lines_as_dicts

    # An axis the reply didn't answer → ask THIS line again instead of moving
    # on, which is what silently produced order items with no Sample Size or
    # Finish. Capped: after two further tries the order proceeds without them
    # rather than trapping the rep in a prompt they cannot satisfy (a parent
    # axis with no usable options would otherwise loop forever).
    _still_pending = line.get("blank_variant_axes") or []
    _axis_tries = int(line.get("variant_axis_attempts") or 0)
    if _still_pending and _axis_tries < 2:
        line["variant_axis_attempts"] = _axis_tries + 1
        lines_as_dicts[line_idx] = line
        user_context["pending_bulk_lines"] = lines_as_dicts
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        logger.info(
            f"bulk_order | line {line_idx} still missing {_still_pending} "
            f"after variant pick — re-asking (attempt {_axis_tries + 1}/2)"
        )
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, pos,
            conversation, user_context, page, start_time,
        )
    if _still_pending:
        logger.warning(
            f"bulk_order | line {line_idx} proceeding with {_still_pending} "
            f"unset after {_axis_tries} attempt(s)"
        )

    next_pos = pos + 1
    user_context["bulk_variant_current_pos"] = next_pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    if next_pos < len(needs_variant_indices):
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, next_pos,
            conversation, user_context, page, start_time,
        )

    # All variants resolved — collect ADDRESSES next, then show the summary.
    #
    # This site returns directly rather than going through
    # _continue_after_addresses_chosen, so reordering that function alone left
    # this path unchanged: the rep still saw a "ready to place" table before
    # any address existed.
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

def _slugify(value: str) -> str:
    """
    Local stand-in for WordPress sanitize_title(). Used only when the real
    term list is unreachable — see _term_slug.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)   # '12"x12"' → '12x12'
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")

def _attribute_terms(attribute_id, user_context):
    """
    {lowercased term name: slug} for one global product attribute.

    Cached per attribute for the session — there are only a handful (Colors,
    Finish, Sample Size) and they change about never, so this is one call per
    attribute per conversation, not per line.
    """
    cache = user_context.setdefault("bulk_attr_term_cache", {})
    key = str(attribute_id)
    if key in cache:
        return cache[key]

    terms = {}
    fetched = False
    try:
        res = woo_client.execute(
            endpoints.list_attribute_terms(
                attribute_id=attribute_id,
                description=f"Fetch terms for attribute_id={attribute_id}",
            )
        )
        data = res.get("data") if res.get("success") else None
        if isinstance(data, list):
            for t in data:
                if not isinstance(t, dict):
                    continue
                name = re.sub(r"\s+", " ", str(t.get("name") or "")).strip().lower()
                slug = str(t.get("slug") or "").strip()
                if name and slug:
                    terms[name] = slug
            fetched = True
    except Exception as exc:
        logger.warning(
            f"bulk_order | attribute term fetch failed for {attribute_id} | error={exc}"
        )

    # Cache successes only, for the same reason as the parent axis cache.
    if fetched:
        cache[key] = terms
        user_context["bulk_attr_term_cache"] = cache
    return terms

def _term_slug(attribute_id, option_name, user_context):
    """
    Display name → term slug ("Chip Card" → "chip-card").

    Prefers the real term list, because a slug edited by hand in wp-admin will
    not match what sanitize_title() would have produced. Falls back to
    slugifying locally when the lookup is unavailable: a best-guess slug that
    usually resolves beats writing the display name, which never does.
    """
    lookup = _attribute_terms(attribute_id, user_context) if attribute_id else {}
    normalised = re.sub(r"\s+", " ", str(option_name or "")).strip().lower()
    slug = lookup.get(normalised)
    if slug:
        return slug
    guess = _slugify(option_name)
    if lookup:
        logger.warning(
            f"bulk_order | no term matching {option_name!r} on attribute "
            f"{attribute_id} — falling back to slugified {guess!r}"
        )
    return guess

def _variant_meta_entry(product_id, axis, value, user_context):
    """
    One line-item meta entry for a rep-chosen "Any" axis.

    Written under the TAXONOMY key with the TERM SLUG — {"key":
    "pa_sample-size", "value": "chip-card"} — which is what the storefront
    produces and what everything downstream reads.

    This used to write the display name as the key ({"key": "Sample Size",
    "value": "Chip Card"}). WooCommerce treated that as ordinary custom meta,
    so the order carried TWO rows: the free-text one, plus the empty
    "pa_sample-size" that WC itself writes because
    WC_Product_Variation::get_variation_attributes() returns every parent
    attribute including the ones the variation leaves as "Any". The blank
    value made WC's term lookup fail, so display_key fell back to the raw
    taxonomy and invoices printed a bare "pa_sample-size:" row. Writing the
    taxonomy key means update_meta_data() replaces that empty row instead of
    sitting beside it, and WC derives the label and display value itself.

    Falls back to the old display-name shape if the taxonomy can't be
    resolved, which is no worse than what it replaces.
    """
    meta = (_parent_axis_meta(product_id, user_context) or {}).get(axis) or {}
    taxonomy = meta.get("taxonomy")
    if not taxonomy:
        return {"key": str(axis), "value": str(value)}
    return {
        "key": taxonomy,
        "value": _term_slug(meta.get("attribute_id"), value, user_context),
    }