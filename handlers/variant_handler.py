"""
handlers/variant_handler.py — Steps 3.55, 3.7, 5.5: Variant and variation handling.

Step 3.55 — AWAITING_VARIANT_SELECTION: resolve variant from user response.
Step 3.7  — Variation product: format parent + matching variations.
Step 5.5  — Detect when variant/quantity is still needed for an order intent.

Each public function returns a Flask response or None to fall through.
"""

import time

from flask import jsonify

from app_config import WOO_BASE_URL, CLASSIFIER_PROVIDER_TAG, get_currency_symbol
from config.settings import DEFAULT_PER_PAGE
from models import Intent, WooAPICall
from woo_client import woo_client
from formatters import format_product, format_variation, _filter_variations_by_entities
from response_generator import generate_bot_message, generate_suggestions
from conversation_flow import FlowState
from chat_logger import get_logger, sanitize_log_string
from formatters import _entities_to_dict
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

def _get_safe_options(attrs):
    if isinstance(attrs, dict):
        return {k.replace("pa_", "").replace("-", " ").title(): str(v).replace("-", " ").title() for k, v in attrs.items() if v}
    elif isinstance(attrs, list):
        return {a.get("name", ""): a.get("option", "") for a in attrs if isinstance(a, dict) and a.get("name") and a.get("option")}
    return {}

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

    _ABANDON_INTENTS = {
        Intent.PRODUCT_SEARCH, Intent.PRODUCT_LIST, Intent.CATEGORY_BROWSE,
        Intent.CATEGORY_LIST, Intent.FILTER_BY_ATTRIBUTE, Intent.PRODUCT_QUICK_SHIP, 
        Intent.GREETING,
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
        return None

    if not (current_flow_state == FlowState.AWAITING_VARIANT_SELECTION and customer_id):
        return None

    _var_product_id = user_context.get("pending_product_id")
    _var_product_name = user_context.get("pending_product_name", "the product")
    _var_quantity = user_context.get("pending_quantity")
    logger.info(f"Step 3.55: Variant selection response | pending_product_id={_var_product_id} | pending_quantity={_var_quantity}")

    if not _var_product_id:
        return None

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

    prev_resolved = user_context.get("resolved_attributes", {})
    if prev_resolved:
        user_msg_lower = message.lower()
        user_msg_clean = _STRIP_QUOTES_RE.sub('', user_msg_lower)
        refined_resolved = dict(prev_resolved)
        for attr_name, attr_val in prev_resolved.items():
            candidate_options = set()
            for var in all_variations:
                opts = _get_safe_options(var.get("attributes", []))
                for name, opt in opts.items():
                    if name.lower() == attr_name.lower():
                        candidate_options.add(opt)
            current_val_lower = attr_val.lower()
            for opt in candidate_options:
                opt_lower = opt.lower()
                opt_clean = _STRIP_QUOTES_RE.sub('', opt_lower)
                if (current_val_lower in opt_lower and opt_lower != current_val_lower and opt_clean in user_msg_clean):
                    refined_resolved[attr_name] = opt
                    break
        prev_resolved = refined_resolved

        pre_filtered = []
        for var in all_variations:
            var_attrs = {k.lower(): v.lower() for k, v in _get_safe_options(var.get("attributes", [])).items()}
            if all(prev_resolved[attr_name].lower() in var_attrs.get(attr_name.lower(), "") for attr_name in prev_resolved):
                pre_filtered.append(var)
        if pre_filtered:
            all_variations = pre_filtered

    if _resolve_variant:
        import re
        user_text_lower = message.lower()
        user_text_clean = _STRIP_QUOTES_RE.sub('', user_text_lower)
        user_tokens = set(_TOKENIZE_RE.findall(user_text_clean))
        
        # Create a heavily stripped version for exact matching (removes commas, hyphens, etc.)
        text_for_exact = re.sub(r'[^\w\s]', ' ', user_text_clean)
        
        scores = []
        for var in all_variations:
            if not var.get("attributes"):
                continue
                
            base_score = score_variation_against_text(var, user_text_clean, user_tokens)
            
            # 🚀 FIX: Manually strip quotes and punctuation from DB options to catch dimensions
            opts = _get_safe_options(var.get("attributes", []))
            for opt_val in opts.values():
                clean_opt = _STRIP_QUOTES_RE.sub('', opt_val.lower()).strip()
                opt_for_exact = re.sub(r'[^\w\s]', ' ', clean_opt)
                
                # Pad with spaces to ensure word boundaries (so "12x12" doesn't falsely match "12x120")
                if opt_for_exact and f" {opt_for_exact} " in f" {text_for_exact} ":
                    base_score += 100
                    
            scores.append((var, base_score))

        max_score = max((s for _, s in scores), default=0)
        matched = [var for var, s in scores if s == max_score] if max_score > 0 else all_variations
        
        logger.debug(f"Step 3.55 Scoring: user_text_clean='{user_text_clean}'")
        logger.debug(f"Step 3.55 Scoring: max_score={max_score} | variations_tied_for_max={len(matched)}")
        if len(matched) < 5:
            logger.debug(f"Step 3.55 Scoring: Matched variants = {[v['id'] for v in matched]}")

    else:
        matched = _filter_variations_by_entities(all_variations, entities)
        if len(matched) != 1:
            user_text_lower = message.lower()
            candidates = matched if len(matched) > 1 else all_variations
            text_matched = [
                var for var in candidates
                if var.get("attributes") and all(
                    opt.lower() in user_text_lower
                    for opt in _get_safe_options(var.get("attributes", [])).values()
                )
            ]
            if text_matched and len(text_matched) < len(candidates):
                matched = text_matched

    if len(matched) == 1:
        _resolved_variation = matched[0]
        _resolved_variation_id = _resolved_variation["id"]
        logger.info(f"Step 3.55: Resolved to variation_id={_resolved_variation_id}")

        if _resolved_variation.get("stock_status") == "outofstock" or _resolved_variation.get("in_stock") is False:
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": f"I'm sorry, but that specific variant is currently out of stock! 😔\n\nWould you like to choose a different finish or size?",
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["Show me other options", "Cancel Order"],
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                    "pending_product_id": _var_product_id,
                    "pending_product_name": _var_product_name,
                    "pending_quantity": _var_quantity,
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                "pagination": default_pagination(page),
            }), 200

        _variant_label = " / ".join(_get_safe_options(_resolved_variation.get("attributes", [])).values())
        
        _variant_price = (
            _resolved_variation.get("sale_price")
            or _resolved_variation.get("price")
            or _resolved_variation.get("regular_price")
            or ""
        )

        if not _var_quantity:
            _price_line = f"\n**Unit Price:** {get_currency_symbol()}{_variant_price}" if _variant_price else ""
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Great choice! Here's what you selected:\n\n"
                    f"**Product:** {_var_product_name}\n"
                    f"**Variant:** {_variant_label}"
                    f"{_price_line}\n\n"
                    f"How many would you like to order? You can tap an option below or type any exact number in the chat. 🛒"
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["1", "5", "10", "25", "Cancel Order"],
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
                "suggestions": ["Yes, use this address", "Change address", "Cancel Order"],
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
                "suggestions": ["Cancel Order"],
                "session_id": session_id,
                "metadata": {**base_meta, "flow_state": FlowState.AWAITING_NEW_ADDRESS.value},
                "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
                "pagination": default_pagination(page),
            }), 200

    resolved_attributes = {}
    if len(matched) > 1:
        attr_values = {}
        for v in matched:
            for name, opt in _get_safe_options(v.get("attributes", [])).items():
                if name and opt:
                    attr_values.setdefault(name, set()).add(opt)
        for attr_name, options in attr_values.items():
            if len(options) == 1:
                resolved_attributes[attr_name] = list(options)[0]
        
    if prev_resolved:
        for k, v in prev_resolved.items():
            if k not in resolved_attributes:
                resolved_attributes[k] = v

    _session_parent = sessions.get(session_id, {}).get("variation_cache", {}).get(str(_var_product_id), {}).get("parent_raw")
    if _session_parent:
        parent_raw = _session_parent
    else:
        parent_call = WooAPICall(
            method="GET",
            endpoint=f"{WOO_BASE_URL}/products/{_var_product_id}",
            params={},
            description=f"Fetch parent product '{_var_product_name}'",
        )
        parent_resp = woo_client.execute(parent_call)
        parent_raw = parent_resp.get("data", {}) if parent_resp.get("success") else {}

    if len(matched) > 1:
        attr_values_all = {}
        for v in matched:
            for name, opt in _get_safe_options(v.get("attributes", [])).items():
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
                " / ".join(_get_safe_options(v.get("attributes", [])).values())
                for v in matched
            ]
            prompt_msg = (
                f"I found **{len(matched)}** variants of **{_var_product_name}** matching your description:\n\n"
                + "\n".join(f"• {lbl}" for lbl in variation_labels if lbl)
                + "\n\nWhich one would you like?"
            )
    else:
        prompt_msg = build_variant_prompt(parent_raw, _var_product_name, resolved_attributes)
        if len(all_variations) > 0:
            prompt_msg = "Sorry, I couldn't find that exact variant. " + prompt_msg

    elapsed = time.time() - start_time
    return jsonify({
        "success": True,
        "bot_message": prompt_msg,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Cancel Order"],
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
        # ── No specific attributes requested — paginate variations in-memory ──
        # The API fetched all variations (per_page=100) because attribute-matching
        # needs the full set. For display, slice to DEFAULT_PER_PAGE per page.
        total_variations = len(variations_raw)
        start = (page - 1) * DEFAULT_PER_PAGE
        end = start + DEFAULT_PER_PAGE
        page_slice = variations_raw[start:end]
        variation_products = [format_variation(v, parent_product_raw) for v in page_slice]
        products = [parent_formatted] + variation_products
        logger.info(
            f"Step 3.7: No attribute filter — returning {len(variation_products)} of "
            f"{total_variations} variations (page {page})"
        )
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

    # ── Build pagination — override for in-memory paginated variations ──
    if variations_raw and not has_attributes:
        total_variations = len(variations_raw)
        total_pages = max(1, -(-total_variations // DEFAULT_PER_PAGE))  # ceil division
        pagination = {
            "page": page,
            "per_page": DEFAULT_PER_PAGE,
            "total_items": total_variations,
            "total_pages": total_pages,
            "has_more": page < total_pages,
        }
    else:
        pagination = build_pagination(page, api_responses, api_calls_to_execute)

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": intent.value,
        "products": products,
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": metadata,
        "pagination": pagination,
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
    customer_id=None,
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
    
    logger.info(
        f"🛑 DEBUG STEP 5.5 | Product: {product.get('name')} | "
        f"Type: {product.get('type')} | "
        f"Variations Count: {len(product.get('variations', []))}"
    )
    
    # ── OUT OF STOCK INTERCEPT ──
    if product.get("stock_status") == "outofstock":
        elapsed = time.time() - start_time
        from conversation_flow import FlowState
        return jsonify({
            "success": True,
            "bot_message": f"I'm so sorry, but **{product['name']}** is currently out of stock!",
            "intent": intent.value,
            "products": products_formatted[:1],
            "suggestions": ["Show similar products", "Browse categories"],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.IDLE.value,
                "response_time_ms": round(elapsed * 1000),
            },
            "flow_state": FlowState.IDLE.value,
            "pagination": default_pagination(page),
        }), 200

    # No quantity yet
    if not entities.quantity:
        if product.get("type") == "variable" or product.get("variations"):
            _raw_for_prompt = next((p for p in all_products_raw if not p.get("parent_id")), {})
            _variations_for_cache = _raw_for_prompt.get("variations", [])
            
            # --- FAST TRACK: EXACT VARIANT RESOLVED BY API ---
            if len(_variations_for_cache) == 1:
                _resolved_var = _variations_for_cache[0]
                if _resolved_var.get("stock_status") == "outofstock" or _resolved_var.get("in_stock") is False:
                    elapsed = time.time() - start_time
                    from conversation_flow import FlowState
                    return jsonify({
                        "success": True,
                        "bot_message": f"I'm sorry, but that specific variant is currently out of stock! 😔",
                        "intent": intent.value,
                        "products": products_formatted[:1],
                        "suggestions": ["Show similar products", "Browse categories"],
                        "session_id": session_id,
                        "metadata": {
                            "flow_state": FlowState.IDLE.value,
                            "response_time_ms": round(elapsed * 1000),
                        },
                        "flow_state": FlowState.IDLE.value,
                        "pagination": default_pagination(page),
                    }), 200
                    
                _var_price = _resolved_var.get("sale_price") or _resolved_var.get("price") or _resolved_var.get("regular_price") or ""
                _price_line = f"\n**Unit Price:** {get_currency_symbol()}{_var_price}" if _var_price else ""
                _var_label = " / ".join(_get_safe_options(_resolved_var.get("attributes", [])).values())
                
                elapsed = time.time() - start_time
                from conversation_flow import FlowState
                return jsonify({
                    "success": True,
                    "bot_message": (
                        f"Great choice! Here's what you selected:\n\n"
                        f"**Product:** {product['name']}\n"
                        f"**Variant:** {_var_label}"
                        f"{_price_line}\n\n"
                        f"How many would you like to order? You can tap an option below or type any exact number in the chat. 🛒"
                    ),
                    "intent": intent.value,
                    "products": products_formatted[:1],
                    "suggestions": ["1", "5", "10", "25", "Cancel Order"],
                    "session_id": session_id,
                    "metadata": {
                        "flow_state": FlowState.AWAITING_QUANTITY.value,
                        "pending_product_id": product.get("id"),
                        "pending_product_name": product["name"],
                        "pending_variation_id": _resolved_var["id"],
                        "response_time_ms": round(elapsed * 1000),
                    },
                    "flow_state": FlowState.AWAITING_QUANTITY.value,
                    "pagination": default_pagination(page),
                }), 200

            # --- NORMAL: NEEDS VARIANT SELECTION ---
            from handlers.chat_utils import build_variant_prompt
            prompt_msg = build_variant_prompt(_raw_for_prompt, product["name"], getattr(entities, 'attributes', {}))
            if session_id and session_id in sessions:
                _pid = str(product.get("id"))
                sessions[session_id].setdefault("variation_cache", {})[_pid] = {
                    "variations": _variations_for_cache,
                    "parent_raw": _raw_for_prompt,
                }
                logger.info(f"Step 5.5: Cached {len(_variations_for_cache)} variations for product_id={_pid} in session")
            elapsed = time.time() - start_time
            from conversation_flow import FlowState
            return jsonify({
                "success": True,
                "bot_message": prompt_msg,
                "intent": intent.value,
                "products": products_formatted[:1],
                "suggestions": ["Cancel Order"],
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
        from conversation_flow import FlowState
        return jsonify({
            "success": True,
            "bot_message": f"Sure, I can order **{product['name']}** for you! How many do you need? You can tap an option below or type any exact number in the chat. 🛒",
            "intent": intent.value,
            "products": products_formatted[:1],
            "suggestions": ["1", "5", "10", "25", "Cancel Order"],
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
    if entities.quantity and not order_data and (product.get("type") == "variable" or product.get("variations")):
        _raw_for_prompt = next((p for p in all_products_raw if not p.get("parent_id")), {})
        _variations_for_cache = _raw_for_prompt.get("variations", [])
        
        # --- FAST TRACK: QUANTITY AND VARIANT BOTH RESOLVED! ---
        if len(_variations_for_cache) == 1:
            _resolved_var = _variations_for_cache[0]
            if _resolved_var.get("stock_status") == "outofstock" or _resolved_var.get("in_stock") is False:
                elapsed = time.time() - start_time
                from conversation_flow import FlowState
                return jsonify({
                    "success": True,
                    "bot_message": f"I'm sorry, but that specific variant is currently out of stock! 😔",
                    "intent": intent.value,
                    "products": products_formatted[:1],
                    "suggestions": ["Show similar products", "Browse categories"],
                    "session_id": session_id,
                    "metadata": {
                        "flow_state": FlowState.IDLE.value,
                        "response_time_ms": round(elapsed * 1000),
                    },
                    "flow_state": FlowState.IDLE.value,
                    "pagination": default_pagination(page),
                }), 200

            shipping_address = fetch_shipping_address(customer_id, "Step 5.5 FastTrack")
            has_address = bool(shipping_address and (shipping_address.get("address_1") or shipping_address.get("city")))

            base_meta = {
                "pending_product_id": product.get("id"),
                "pending_product_name": product["name"],
                "pending_quantity": entities.quantity,
                "pending_variation_id": _resolved_var["id"],
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
                from conversation_flow import FlowState
                return jsonify({
                    "success": True,
                    "bot_message": (
                        f"Great! I have everything I need to order **{entities.quantity}** of **{product['name']}**.\n\n"
                        f"Your shipping address on file:\n\n📦 **{addr_display}**\n\n"
                        "Would you like to ship to this address, or use a different one?"
                    ),
                    "intent": intent.value,
                    "products": products_formatted[:1],
                    "suggestions": ["Yes, use this address", "Change address", "Cancel Order"],
                    "session_id": session_id,
                    "metadata": {**base_meta, "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value},
                    "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value,
                    "pagination": default_pagination(page),
                }), 200
            else:
                elapsed = time.time() - start_time
                from conversation_flow import FlowState
                return jsonify({
                    "success": True,
                    "bot_message": (
                        f"Great! I have everything I need to order **{entities.quantity}** of **{product['name']}**.\n\n"
                        "No shipping address is on file. Please type your shipping address (street, city, state, zip code):"
                    ),
                    "intent": intent.value,
                    "products": products_formatted[:1],
                    "suggestions": ["Cancel Order"],
                    "session_id": session_id,
                    "metadata": {**base_meta, "flow_state": FlowState.AWAITING_NEW_ADDRESS.value},
                    "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
                    "pagination": default_pagination(page),
                }), 200

        # --- NORMAL: NEEDS VARIANT SELECTION ---
        from handlers.chat_utils import build_variant_prompt
        prompt_msg = build_variant_prompt(_raw_for_prompt, product["name"], getattr(entities, 'attributes', {}))
        if session_id and session_id in sessions:
            _pid = str(product.get("id"))
            sessions[session_id].setdefault("variation_cache", {})[_pid] = {
                "variations": _variations_for_cache,
                "parent_raw": _raw_for_prompt,
            }
            logger.info(f"Step 5.5: Cached {len(_variations_for_cache)} variations for product_id={_pid} in session")
        elapsed = time.time() - start_time
        from conversation_flow import FlowState
        return jsonify({
            "success": True,
            "bot_message": prompt_msg,
            "intent": intent.value,
            "products": products_formatted[:1],
            "suggestions": ["Cancel Order"],
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
