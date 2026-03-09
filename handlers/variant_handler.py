"""
handlers/variant_handler.py — Steps 3.55, 3.7, 5.5: Variant and variation handling.

Step 3.55 — AWAITING_VARIANT_SELECTION: resolve variant from user response.
Step 3.7  — Variation product: format parent + matching variations.
Step 5.5  — Detect when variant/quantity is still needed for an order intent.

Each public function returns a Flask response or None to fall through.
"""

import time

from flask import jsonify

from app_config import WOO_BASE_URL
from models import Intent, WooAPICall
from woo_client import woo_client
from formatters import format_product, format_variation, _filter_variations_by_entities
from response_generator import generate_bot_message, generate_suggestions, INTENT_LABELS
from conversation_flow import FlowState
from chat_logger import get_logger, sanitize_log_string
from formatters import _entities_to_dict
from app_config import CLASSIFIER_PROVIDER_TAG
from handlers.chat_utils import (
    default_pagination,
    build_pagination,
    build_variant_prompt,
    fetch_shipping_address,
    score_variation_against_text,
    _STRIP_QUOTES_RE,
    _TOKENIZE_RE,
)
from api_builder import match_variation_to_entities

from datetime import datetime, timezone

logger = get_logger("miraq_chat")


def handle_variant_selection(
    current_flow_state,
    intent,
    entities,
    message,
    customer_id,
    session_id,
    page,
    start_time,
    sessions,
    user_context,
    _resolve_variant,
):
    """
    Step 3.55: Resolve variant selection from user response.
    Returns Flask response or None.
    """
    # Abandon variant flow if user sent a new search/browse intent
    _ABANDON_INTENTS = {
        Intent.PRODUCT_SEARCH, Intent.PRODUCT_LIST, Intent.CATEGORY_BROWSE,
        Intent.CATEGORY_LIST, Intent.FILTER_BY_FINISH, Intent.FILTER_BY_SIZE,
        Intent.FILTER_BY_COLOR, Intent.FILTER_BY_APPLICATION, Intent.PRODUCT_BY_VISUAL,
        Intent.PRODUCT_BY_ORIGIN, Intent.PRODUCT_QUICK_SHIP, Intent.GREETING,
    }
    if current_flow_state == FlowState.AWAITING_VARIANT_SELECTION and intent in _ABANDON_INTENTS:
        logger.info(
            f"Step 3.55: Abandoning variant flow — new intent={intent.value} detected | "
            f"message=\"{sanitize_log_string(message)}\""
        )
        if session_id and session_id in sessions:
            sessions[session_id]["flow_state"] = FlowState.IDLE.value
            sessions[session_id]["user_context"].pop("pending_product_id", None)
            sessions[session_id]["user_context"].pop("pending_product_name", None)
            sessions[session_id]["user_context"].pop("pending_quantity", None)
            sessions[session_id]["user_context"].pop("resolved_attributes", None)
        return None  # fall through with IDLE flow state

    if not (current_flow_state == FlowState.AWAITING_VARIANT_SELECTION and customer_id):
        return None

    _var_product_id = user_context.get("pending_product_id")
    _var_product_name = user_context.get("pending_product_name", "the product")
    _var_quantity = user_context.get("pending_quantity")
    logger.info(f"Step 3.55: Variant selection response | pending_product_id={_var_product_id} | pending_quantity={_var_quantity}")

    if not _var_product_id:
        return None

    # ── Load variations (session cache or API) ──
    _session_data = sessions.get(session_id, {})
    _var_cache = _session_data.get("variation_cache", {}).get(str(_var_product_id))
    if _var_cache:
        all_variations = _var_cache["variations"]
        logger.info(f"Step 3.55: Using session-cached variations ({len(all_variations)}) — skipping API call")
        _variations_loaded = True
    else:
        var_call = WooAPICall(
            method="GET",
            endpoint=f"{WOO_BASE_URL}/products/{_var_product_id}/variations",
            params={"per_page": 100, "status": "publish"},
            description=f"Fetch variations for variant selection of '{_var_product_name}'",
        )
        var_resp = woo_client.execute(var_call)
        _variations_loaded = var_resp.get("success") and isinstance(var_resp.get("data"), list)
        if _variations_loaded:
            all_variations = var_resp["data"]

    if not _variations_loaded:
        return None

    # ── Pre-filter using resolved attributes from prior turns ──
    prev_resolved = user_context.get("resolved_attributes", {})
    if prev_resolved:
        # Refine prev_resolved if user specified a more specific value this turn
        user_msg_lower = message.lower()
        user_msg_clean = _STRIP_QUOTES_RE.sub('', user_msg_lower)
        refined_resolved = dict(prev_resolved)
        for attr_name, attr_val in prev_resolved.items():
            candidate_options = set()
            for var in all_variations:
                for a in var.get("attributes", []):
                    if a.get("name", "").lower() == attr_name.lower():
                        opt = a.get("option", "")
                        if opt:
                            candidate_options.add(opt)
            current_val_lower = attr_val.lower()
            for opt in candidate_options:
                opt_lower = opt.lower()
                opt_clean = _STRIP_QUOTES_RE.sub('', opt_lower)
                if (
                    current_val_lower in opt_lower
                    and opt_lower != current_val_lower
                    and opt_clean in user_msg_clean
                ):
                    refined_resolved[attr_name] = opt
                    logger.info(
                        f"Step 3.55: Refined resolved attribute "
                        f"{attr_name}: '{attr_val}' → '{opt}' based on current message"
                    )
                    break
        prev_resolved = refined_resolved

        # Apply pre-filter
        pre_filtered = []
        for var in all_variations:
            var_attrs = {
                a.get("name", "").lower(): a.get("option", "").lower()
                for a in var.get("attributes", [])
            }
            if all(
                prev_resolved[attr_name].lower() in var_attrs.get(attr_name.lower(), "")
                for attr_name in prev_resolved
            ):
                pre_filtered.append(var)
        if pre_filtered:
            logger.info(f"Step 3.55: Pre-filtered {len(all_variations)} → {len(pre_filtered)} using resolved_attributes={prev_resolved}")
            all_variations = pre_filtered

    # ── Score/match variations against user message ──
    if _resolve_variant:
        user_text_lower = message.lower()
        user_text_clean = _STRIP_QUOTES_RE.sub('', user_text_lower)
        user_tokens = set(_TOKENIZE_RE.findall(user_text_clean))
        scores = [
            (var, score_variation_against_text(var, user_text_clean, user_tokens))
            for var in all_variations
            if var.get("attributes")
        ]
        max_score = max((s for _, s in scores), default=0)
        matched = [var for var, s in scores if s == max_score] if max_score > 0 else all_variations
    else:
        matched = _filter_variations_by_entities(all_variations, entities)
        if len(matched) != 1:
            user_text_lower = message.lower()
            candidates = matched if len(matched) > 1 else all_variations
            text_matched = [
                var for var in candidates
                if var.get("attributes") and all(
                    a.get("option", "").lower() in user_text_lower
                    for a in var.get("attributes", [])
                    if a.get("option")
                )
            ]
            if text_matched and len(text_matched) < len(candidates):
                matched = text_matched

    # ── Single match — enter confirmation flow ──
    if len(matched) == 1:
        _resolved_variation = matched[0]
        _resolved_variation_id = _resolved_variation["id"]
        logger.info(f"Step 3.55: Resolved to variation_id={_resolved_variation_id}")

        _variant_label = " / ".join(
            a.get("option", "") for a in _resolved_variation.get("attributes", []) if a.get("option")
        )
        _variant_price = (
            _resolved_variation.get("sale_price")
            or _resolved_variation.get("price")
            or _resolved_variation.get("regular_price")
            or ""
        )

        if not _var_quantity:
            logger.info(f"Step 3.55: Variant resolved, asking for quantity | price={_variant_price}")
            _price_line = f"\n**Unit Price:** ${_variant_price}" if _variant_price else ""
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Great choice! Here's what you selected:\n\n"
                    f"**Product:** {_var_product_name}\n"
                    f"**Variant:** {_variant_label}"
                    f"{_price_line}\n\n"
                    f"How many would you like to order? 🛒"
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["1", "5", "10", "25"],
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.AWAITING_QUANTITY.value,
                    "pending_product_id": _var_product_id,
                    "pending_product_name": _var_product_name,
                    "pending_variation_id": _resolved_variation_id,
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_QUANTITY.value,
                "pagination": default_pagination(page),
            }), 200

        # Quantity known — proceed to shipping
        logger.info(f"Step 3.55: Variant resolved with quantity={_var_quantity}, proceeding to shipping")
        shipping_address = fetch_shipping_address(customer_id, "Step 3.55")
        has_address = bool(shipping_address and (shipping_address.get("address_1") or shipping_address.get("city")))

        base_meta = {
            "pending_product_id": _var_product_id,
            "pending_product_name": _var_product_name,
            "pending_quantity": _var_quantity,
            "pending_variation_id": _resolved_variation_id,
            "response_time_ms": round((time.time() - start_time) * 1000),
        }

        if has_address:
            addr_parts = [p for p in [
                shipping_address.get("address_1", ""), shipping_address.get("address_2", ""),
                shipping_address.get("city", ""), shipping_address.get("state", ""),
                shipping_address.get("postcode", ""), shipping_address.get("country", ""),
            ] if p]
            addr_display = ", ".join(addr_parts)
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Your shipping address on file:\n\n📦 **{addr_display}**\n\n"
                    "Would you like to ship to this address, or use a different one?"
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["Yes, use this address", "Change address", "Cancel"],
                "session_id": session_id,
                "metadata": {**base_meta, "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value},
                "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value,
                "pagination": default_pagination(page),
            }), 200
        else:
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": "No shipping address is on file. Please type your shipping address (street, city, state, zip code):",
                "intent": "guided_flow",
                "products": [],
                "suggestions": [],
                "session_id": session_id,
                "metadata": {**base_meta, "flow_state": FlowState.AWAITING_NEW_ADDRESS.value},
                "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
                "pagination": default_pagination(page),
            }), 200

    # ── Multiple or no match — ask user to narrow down ──
    logger.info(f"Step 3.55: Could not resolve to single variation | matched={len(matched)} of {len(all_variations)}")
    resolved_attributes = {}
        # Calculate resolved attributes when matched > 1 (regardless of whether it equals all_variations)
    if len(matched) > 1:
        attr_values = {}
        for v in matched:
            for a in v.get("attributes", []):
                name = a.get("name", "")
                opt = a.get("option", "")
                if name and opt:
                    attr_values.setdefault(name, set()).add(opt)
        for attr_name, options in attr_values.items():
            if len(options) == 1:
                resolved_attributes[attr_name] = list(options)[0]
        logger.info(f"Step 3.55: Resolved attributes so far: {resolved_attributes}")
        
    if prev_resolved:
        for k, v in prev_resolved.items():
            if k not in resolved_attributes:
                resolved_attributes[k] = v
        logger.info(f"Step 3.55: Merged with previous resolved_attributes: {resolved_attributes}")

    # Load parent product for re-prompt (from cache or API)
    _session_parent = sessions.get(session_id, {}).get("variation_cache", {}).get(str(_var_product_id), {}).get("parent_raw")
    if _session_parent:
        parent_raw = _session_parent
        logger.info("Step 3.55: Using session-cached parent_raw — skipping parent product API call")
    else:
        parent_call = WooAPICall(
            method="GET",
            endpoint=f"{WOO_BASE_URL}/products/{_var_product_id}",
            params={},
            description=f"Fetch parent product '{_var_product_name}' for variant re-prompt",
        )
        parent_resp = woo_client.execute(parent_call)
        parent_raw = parent_resp.get("data", {}) if parent_resp.get("success") else {}

    if len(matched) > 1:
        attr_values_all = {}
        for v in matched:
            for a in v.get("attributes", []):
                name = a.get("name", "")
                opt = a.get("option", "")
                if name and opt:
                    attr_values_all.setdefault(name, set()).add(opt)
        _already_resolved = {k.lower() for k in resolved_attributes}
        ambiguous = {
            k: sorted(v)
            for k, v in attr_values_all.items()
            if len(v) > 1 and k.lower() not in _already_resolved
        }
        if ambiguous:
            lines = [f"Great, I found **{_var_product_name}** in your selected options! I just need a bit more info:\n"]
            for attr_name, options in ambiguous.items():
                lines.append(f"• **{attr_name}:** {', '.join(options)}")
            lines.append("\nWhich combination would you like?")
            prompt_msg = "\n".join(lines)
        else:
            variation_labels = [
                " / ".join(a.get("option", "") for a in v.get("attributes", []) if a.get("option"))
                for v in matched
            ]
            prompt_msg = (
                f"I found **{len(matched)}** variants of **{_var_product_name}** matching your description:\n\n"
                + "\n".join(f"• {lbl}" for lbl in variation_labels if lbl)
                + "\n\nWhich one would you like?"
            )
    else:
        prompt_msg = build_variant_prompt(parent_raw, _var_product_name)
        if len(all_variations) > 0:
            prompt_msg = "Sorry, I couldn't find that exact variant. " + prompt_msg

    elapsed = time.time() - start_time
    return jsonify({
        "success": True,
        "bot_message": prompt_msg,
        "intent": "guided_flow",
        "products": [],
        "suggestions": [],
        "session_id": session_id,
        "metadata": {
            "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
            "pending_product_id": _var_product_id,
            "pending_product_name": _var_product_name,
            "pending_quantity": _var_quantity,
            "resolved_attributes": resolved_attributes,
            "response_time_ms": round(elapsed * 1000),
        },
        "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
        "pagination": default_pagination(page),
    }), 200


def handle_variation_product(
    intent,
    entities,
    api_responses,
    api_calls_to_execute,
    confidence,
    order_data,
    session_id,
    page,
    start_time,
    sessions,
):
    """
    Step 3.7: Format parent product + matched variations for PRODUCT_SEARCH /
    PRODUCT_DETAIL / PRODUCT_VARIATIONS when a specific product_id is present.
    Returns Flask response or None.
    """
    VARIATION_INTENTS = {Intent.PRODUCT_SEARCH, Intent.PRODUCT_DETAIL, Intent.PRODUCT_VARIATIONS}
    if not (intent in VARIATION_INTENTS and entities.product_id):
        return None

    parent_product_raw = None
    variations_raw = []

    for resp in api_responses:
        if not resp.get("success"):
            continue
        data = resp.get("data")
        if isinstance(data, dict) and data.get("id") == entities.product_id:
            parent_product_raw = data
        elif isinstance(data, list) and data and data[0].get("parent_id") is not None:
            variations_raw = data

    if not parent_product_raw:
        return None

    from formatters import format_product as _format_product
    parent_formatted = _format_product(parent_product_raw)

    category_mismatch_msg = ""
    if entities.category_name:
        product_cats = [c.lower() for c in parent_formatted.get("categories", [])]
        requested_cat = entities.category_name.lower()
        if product_cats and requested_cat not in product_cats:
            actual_cats = ", ".join(parent_formatted.get("categories", []))
            category_mismatch_msg = (
                f"**{parent_formatted['name']}** is not available in the "
                f"**{entities.category_name}** category — it's part of "
                f"**{actual_cats}**."
            )
            logger.info(
                f"Step 3.7: Category mismatch detected | "
                f"product={parent_formatted['name']} | "
                f"requested_category={entities.category_name} | "
                f"actual_categories={actual_cats}"
            )
            entities.category_name = actual_cats

    has_attributes = bool(entities.attributes)
    matched_variation = None  # best single-variation match for PRODUCT_DETAIL price lookups

    if variations_raw and has_attributes:
        # For PRODUCT_DETAIL: use match_variation_to_entities to find the single best
        # variation matching the user's requested attributes (e.g. Finish=Silky, Size=3"x3").
        # This surfaces the correct price instead of returning all 50+ variations.
        if intent == Intent.PRODUCT_DETAIL:
            matched_variation = match_variation_to_entities(variations_raw, entities)
            if matched_variation:
                logger.info(
                    f"Step 3.7: Matched variation id={matched_variation.get('id')} | "
                    f"price={matched_variation.get('price')} | "
                    f"attrs={[a.get('option') for a in matched_variation.get('attributes', [])]}"
                )
                variation_products = [format_variation(matched_variation, parent_product_raw)]
            else:
                logger.info("Step 3.7: match_variation_to_entities found no match — falling back to filtered list")
                filtered_vars = _filter_variations_by_entities(variations_raw, entities)
                variation_products = [format_variation(v, parent_product_raw) for v in filtered_vars]
        else:
            filtered_vars = _filter_variations_by_entities(variations_raw, entities)
            variation_products = [format_variation(v, parent_product_raw) for v in filtered_vars]
        products = [parent_formatted] + variation_products
    elif variations_raw:
        variation_products = [format_variation(v, parent_product_raw) for v in variations_raw]
        products = [parent_formatted] + variation_products
    else:
        products = [parent_formatted]

    bot_message = generate_bot_message(intent, entities, products, confidence, order_data)
    if category_mismatch_msg:
        bot_message = f"⚠️ {category_mismatch_msg}\n\n{bot_message}"

    suggestions = generate_suggestions(intent, entities, products)
    elapsed = time.time() - start_time
    metadata = {
        "confidence": round(confidence, 2),
        "products_count": len(products),
        "provider": CLASSIFIER_PROVIDER_TAG,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_time_ms": round(elapsed * 1000),
        "intent_raw": intent.value,
        "entities": _entities_to_dict(entities),
        "variations_found": len(variations_raw),
        "variations_matched": len(products) - 1 if variations_raw else 0,
        "category_mismatch": bool(category_mismatch_msg),
        # For PRODUCT_DETAIL: surface matched variation price so response_generator
        # and frontend can display it directly without scanning the products list.
        **({"matched_variation_price": (
            matched_variation.get("sale_price")
            or matched_variation.get("price")
            or matched_variation.get("regular_price")
        ), "matched_variation_id": matched_variation.get("id")} if matched_variation else {}),
    }

    if session_id and session_id in sessions:
        sessions[session_id]["history"].append({
            "role": "bot", "message": bot_message, "intent": intent.value,
            "products_count": len(products),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    from response_generator import INTENT_LABELS as _IL
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": _IL.get(intent, "unknown"),
        "products": products,
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": metadata,
        "pagination": build_pagination(page, api_responses, api_calls_to_execute),
    }), 200


def handle_quantity_and_variant_check(
    intent,
    entities,
    all_products_raw,
    order_data,
    order_create_intents,
    session_id,
    page,
    start_time,
    sessions,
):
    """
    Step 5.5: Detect when quantity or variant selection is still needed
    for an order intent. Returns Flask response or None.
    """
    if intent not in order_create_intents:
        return None

    products_formatted = []
    for p in all_products_raw:
        if not p.get("parent_id"):
            from formatters import format_product, format_custom_product
            if "featured_image" in p:
                products_formatted.append(format_custom_product(p))
            else:
                products_formatted.append(format_product(p))
    products_formatted = [p for p in products_formatted if p.get("name")]

    if not products_formatted:
        return None

    product = products_formatted[0]

    # No quantity yet
    if not entities.quantity:
        if product.get("type") == "variable":
            _raw_for_prompt = next((p for p in all_products_raw if not p.get("parent_id")), {})
            _variations_for_cache = [p for p in all_products_raw if p.get("parent_id") == product.get("id")]
            prompt_msg = build_variant_prompt(_raw_for_prompt, product["name"])
            if session_id and session_id in sessions:
                _pid = str(product.get("id"))
                sessions[session_id].setdefault("variation_cache", {})[_pid] = {
                    "variations": _variations_for_cache,
                    "parent_raw": _raw_for_prompt,
                }
                logger.info(f"Step 5.5: Cached {len(_variations_for_cache)} variations for product_id={_pid} in session")
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": prompt_msg,
                "intent": INTENT_LABELS.get(intent, "order"),
                "products": products_formatted[:1],
                "suggestions": [],
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                    "pending_product_id": product.get("id"),
                    "pending_product_name": product["name"],
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                "pagination": default_pagination(page),
            }), 200

        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": f"Sure, I can order **{product['name']}** for you! How many do you need? 🛒",
            "intent": INTENT_LABELS.get(intent, "order"),
            "products": products_formatted[:1],
            "suggestions": ["1", "5", "10", "25"],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.AWAITING_QUANTITY.value,
                "pending_product_name": product["name"],
                "pending_product_id": product.get("id"),
                "response_time_ms": round(elapsed * 1000),
            },
            "flow_state": FlowState.AWAITING_QUANTITY.value,
            "pagination": default_pagination(page),
        }), 200

    # Quantity known but variable product not yet resolved
    if entities.quantity and not order_data and product.get("type") == "variable":
        _raw_for_prompt = next((p for p in all_products_raw if not p.get("parent_id")), {})
        _variations_for_cache = [p for p in all_products_raw if p.get("parent_id") == product.get("id")]
        prompt_msg = build_variant_prompt(_raw_for_prompt, product["name"])
        if session_id and session_id in sessions:
            _pid = str(product.get("id"))
            sessions[session_id].setdefault("variation_cache", {})[_pid] = {
                "variations": _variations_for_cache,
                "parent_raw": _raw_for_prompt,
            }
            logger.info(f"Step 5.5: Cached {len(_variations_for_cache)} variations for product_id={_pid} in session")
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": prompt_msg,
            "intent": INTENT_LABELS.get(intent, "order"),
            "products": products_formatted[:1],
            "suggestions": [],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                "pending_product_id": product.get("id"),
                "pending_product_name": product["name"],
                "pending_quantity": entities.quantity,
                "response_time_ms": round(elapsed * 1000),
            },
            "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
            "pagination": default_pagination(page),
        }), 200

    return None