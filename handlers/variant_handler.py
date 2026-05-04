"""
handlers/variant_handler.py — Steps 3.55, 3.7, 5.5: Variant and variation handling.

Step 3.55 — AWAITING_VARIANT_SELECTION: resolve variant from user response.
Step 3.7  — Variation product: format parent + matching variations.
Step 5.5  — Detect when variant/quantity is still needed for an order intent.

Each public function returns a Flask response or None to fall through.
"""

import time
import re
from flask import jsonify

from app_config import DEFAULT_PER_PAGE, WOO_BASE_URL, CLASSIFIER_PROVIDER_TAG, get_currency_symbol
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
    score_variation_against_text,
    _STRIP_QUOTES_RE,
    _TOKENIZE_RE,
)
from api_builder import match_variation_to_entities
from datetime import datetime, timezone
from sqlalchemy.orm.attributes import flag_modified

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
    user_context,
    _resolve_variant,
):
    """
    Step 3.55: Resolve variant selection from user response.
    Returns Flask response or None.
    """

    _ABANDON_INTENTS = {
        Intent.PRODUCT_LIST, Intent.CATEGORY_BROWSE,
        Intent.CATEGORY_LIST, Intent.PRODUCT_QUICK_SHIP, 
        Intent.GREETING,
    }
    
    # ── SMART CHECK: Are they searching for a completely different product? ──
    is_searching_new_product = False
    
    if intent in (Intent.PRODUCT_SEARCH, Intent.FILTER_BY_ATTRIBUTE):
        new_id = getattr(entities, 'product_id', None)
        pending_id = user_context.get("pending_product_id")
        
        # If they found a new product, and it's NOT the one we are currently ordering -> Abandon
        if new_id and pending_id and new_id != pending_id:
            is_searching_new_product = True

    # Now we only abandon if it's an explicit abandon intent OR they searched for a different product
    if current_flow_state == FlowState.AWAITING_VARIANT_SELECTION and (intent in _ABANDON_INTENTS or is_searching_new_product):
        logger.info(
            f"Step 3.55: Abandoning variant flow — new intent={intent.value} detected | "
            f"message=\"{sanitize_log_string(message)}\""
        )
        user_context.pop("pending_product_id", None)
        user_context.pop("pending_product_name", None)
        user_context.pop("pending_quantity", None)
        user_context.pop("resolved_attributes", None)
        return None

    if not (current_flow_state == FlowState.AWAITING_VARIANT_SELECTION and customer_id):
        return None

    _var_product_id = user_context.get("pending_product_id") or getattr(entities, 'product_id', None)
    _var_product_name = user_context.get("pending_product_name") or getattr(entities, 'product_name', None) or "the product"
    _var_quantity = user_context.get("pending_quantity")
    logger.info(f"Step 3.55: Variant selection response | pending_product_id={_var_product_id} | pending_quantity={_var_quantity}")

    if not _var_product_id:
        return None
    
    # Paginate to get all variations
    all_variations = []
    page_num = 1
    while True:
        var_resp = woo_client.execute(WooAPICall(
            method="GET",
            endpoint=f"{WOO_BASE_URL}/products/{_var_product_id}/variations",
            params={"per_page": 100, "page": page_num, "status": "publish"},
            description=f"Fetch variations for variant selection of '{_var_product_name}'",
        ))
        batch = var_resp.get("data", []) if var_resp.get("success") else []
        all_variations.extend(batch)
        if len(batch) < 100:
            break
        page_num += 1
        
    # Fetch parent_raw early so we know the EXACT variation axes required
    parent_raw = {}
    parent_call = WooAPICall(
        method="GET",
        endpoint=f"{WOO_BASE_URL}/products/{_var_product_id}",
        params={},
        description=f"Fetch parent product '{_var_product_name}'",
    )
    parent_resp = woo_client.execute(parent_call)
    parent_raw = parent_resp.get("data", {}) if parent_resp.get("success") else {}

    prev_resolved = user_context.get("resolved_attributes", {})
    # Extract ALL variation-capable attributes from the parent product
    candidate_options = {}
    for attr in parent_raw.get("attributes", []):
        if attr.get("variation"):
            name = attr.get("name", "")
            nice_name = name.replace("pa_", "").replace("-", " ").title() if name.startswith("pa_") else name.title()
            candidate_options[nice_name] = set(str(o) for o in attr.get("options", []) if str(o).strip())

    user_msg_lower = message.lower()
    user_msg_clean = _STRIP_QUOTES_RE.sub('', user_msg_lower)
    
    # Extract regex outside the f-string for Python 3.10 compatibility
    stripped_text = re.sub(r'[^\w\s]', ' ', user_msg_clean)
    msg_for_extraction = f" {stripped_text} "
    msg_for_extraction = re.sub(r'\s+', ' ', msg_for_extraction) # Collapse multiple spaces
    
    # Catch newly spoken attributes from the user's message
    for nice_name, opts in candidate_options.items():
        for opt in opts:
            opt_clean = _STRIP_QUOTES_RE.sub('', opt.lower()).strip()
            
            # Strip punctuation from the option as well so they match perfectly
            opt_for_exact = re.sub(r'[^\w\s]', ' ', opt_clean)
            opt_for_exact = re.sub(r'\s+', ' ', opt_for_exact).strip()
            
            
            # ── Normalised check: handles curly/smart quotes (e.g. 8"X8" where " isn't stripped) ──
            opt_normalised = re.sub(r'\s+', '', opt_for_exact)
            msg_normalised  = re.sub(r'\s+', '', stripped_text)
            if opt_normalised and re.search(
                r'(?<![a-z0-9])' + re.escape(opt_normalised) + r'(?![a-z0-9])',
                msg_normalised
            ):
                prev_resolved[nice_name] = opt

            # ── Padded check: standard word-boundary match for space-delimited options ──
            if opt_for_exact and f" {opt_for_exact} " in msg_for_extraction:
                prev_resolved[nice_name] = opt
            elif opt_clean == user_msg_clean.strip():
                prev_resolved[nice_name] = opt

    if _resolve_variant:
        user_text_lower = message.lower()
        user_text_clean = _STRIP_QUOTES_RE.sub('', user_text_lower)
        user_tokens = set(_TOKENIZE_RE.findall(user_text_clean))
        
        # Create a heavily stripped version for exact matching (removes commas, hyphens, etc.)
        text_for_exact = re.sub(r'[^\w\s]', ' ', user_text_clean)
        
        scores = []
        for var in all_variations:
            if not var.get("attributes"): continue  
            base_score = score_variation_against_text(var, user_text_clean, user_tokens)
            
            # Manually strip quotes and punctuation from DB options to catch dimensions
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

        # ── Tiebreak / fallback: score matched set against prev_resolved ──────────
        # If max_score==0 (all tied), or multiple tied, the current message alone
        # can't distinguish variations. Use the accumulated resolved attributes to
        # find the best match, so a wildcard axis as the final message (e.g. "Matte",
        # "12x12") doesn't cause the wrong variation to be picked.
        if len(matched) != 1 and prev_resolved:
            resolved_scores = []
            for var in matched:
                var_opts = _get_safe_options(var.get("attributes", []))
                rscore = 0
                for res_key, res_val in prev_resolved.items():
                    for var_attr_name, var_attr_val in var_opts.items():
                        if (res_key.lower() == var_attr_name.lower()
                                and res_val.lower() == var_attr_val.lower()):
                            rscore += 50
                resolved_scores.append((var, rscore))

            best_rscore = max((s for _, s in resolved_scores), default=0)
            if best_rscore > 0:
                matched = [var for var, s in resolved_scores if s == best_rscore]
                logger.info(
                    f"Step 3.55: Tiebreak via prev_resolved | "
                    f"best_rscore={best_rscore} | "
                    f"matched={[v['id'] for v in matched]}"
                )
        
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

    # Check if all REQUIRED wildcard/variation axes have been resolved!
    missing_required = []
    resolved_keys_lower = {k.lower() for k in prev_resolved.keys()}
    for nice_name in candidate_options.keys():
        if nice_name.lower() not in resolved_keys_lower:
            missing_required.append(nice_name)

    is_fully_resolved = len(missing_required) == 0

    if len(matched) >= 1 and is_fully_resolved:
        _resolved_variation = matched[0]
        _resolved_variation_id = _resolved_variation["id"]
        logger.info(f"Step 3.55: Resolved to variation_id={_resolved_variation_id}")
        user_context["pending_variation_id"] = _resolved_variation_id

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

        _variant_label = " / ".join(prev_resolved.values())

        _variant_price = (
            _resolved_variation.get("sale_price")
            or _resolved_variation.get("price")
            or _resolved_variation.get("regular_price")
            or ""
        )
        
        user_context["resolved_attributes"] = prev_resolved

        if not _var_quantity:
            _price_line = f"\n**Unit Price:** {get_currency_symbol()}{_variant_price}" if _variant_price else ""
            elapsed = time.time() - start_time

            # ── If this flow originated from a cart/browse action (not an order
            # intent), go back to cart confirmation so the user can choose.
            _pending_action = user_context.get("pending_action", "order")

            if _pending_action == "cart":
                user_context.pop("pending_action", None)
                _pending_name = f"{_var_product_name} — {_variant_label}"
                user_context["pending_product_name"] = _pending_name
                return jsonify({
                    "success": True,
                    "bot_message": (
                        f"Here's **{_pending_name}**. ✅ In stock"
                        f"{_price_line}\n\n"
                        f"Would you like to add it to your cart?"
                    ),
                    "intent": "guided_flow",
                    "products": [],
                    "suggestions": ["Yes, add it", "No thanks"],
                    "session_id": session_id,
                    "metadata": {
                        "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
                        "pending_product_id": _var_product_id,
                        "pending_product_name": _pending_name,
                        "pending_variation_id": _resolved_variation_id,
                        "resolved_attributes": prev_resolved,
                        "response_time_ms": round(elapsed * 1000),
                    },
                    "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
                    "pagination": default_pagination(page),
                }), 200
                
            # Order flow — ask for quantity
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
                    "resolved_attributes": prev_resolved,
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_QUANTITY.value,
                "pagination": default_pagination(page),
            }), 200
        
        quantity = _var_quantity or 1
        variant_suffix = f" ({_variant_label})" if _variant_label else ""
        cart_msg = f"Got it — add **{_var_product_name}**{variant_suffix} ×{quantity} to your cart?"
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": cart_msg,
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Yes, add it", "No thanks"],
            "session_id": session_id,
            "metadata": {
                "pending_product_id": _var_product_id,
                "pending_product_name": _var_product_name,
                "pending_quantity": quantity,
                "pending_variation_id": _resolved_variation_id,
                "resolved_attributes": prev_resolved,
                "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
                "response_time_ms": round(elapsed * 1000),
            },
            "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
            "pagination": default_pagination(page),
        }), 200
            
    # ── Fallback: Need to ask for missing info ──
    resolved_attributes = dict(prev_resolved) if prev_resolved else {}
    
    if not is_fully_resolved:
        # 🚀 UPDATE: Pass all_variations so wildcard axes are merged perfectly
        prompt_msg = build_variant_prompt(parent_raw, _var_product_name, resolved_attributes, all_variations)
        # If we have a generic variation matched, we shouldn't say "Sorry I couldn't find that exact variant"
        prompt_msg = prompt_msg.replace("Sorry, I couldn't find that exact variant. ", "")
    elif len(matched) > 1:
        # Traditional Ambiguity Handling
        attr_values_all = {}
        for v in matched:
            if v.get("stock_status") == "outofstock" or v.get("in_stock") is False:
                continue
            for name, opt in _get_safe_options(v.get("attributes", [])).items():
                if name and opt:
                    attr_values_all.setdefault(name, set()).add(opt)
        
        _already_resolved = {k.lower() for k in resolved_attributes}
        
        # Supplement with parent-level wildcard options for ambiguity resolution
        for attr in parent_raw.get("attributes", []):
            if isinstance(attr, dict) and attr.get("variation") is True:
                name = attr.get("name", "")
                nice_name = name.replace("pa_", "").replace("-", " ").title() if name.startswith("pa_") else name.title()
                if nice_name and nice_name.lower() not in _already_resolved:
                    opts = attr.get("options", [])
                    if opts:
                        if nice_name not in attr_values_all:
                            attr_values_all[nice_name] = set()
                        for o in opts:
                            if str(o).strip():
                                attr_values_all[nice_name].add(str(o).strip())

        ambiguous = {
            k: sorted(list(v))
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
        # Pass all_variations
        prompt_msg = build_variant_prompt(parent_raw, _var_product_name, resolved_attributes, all_variations)
        if len(all_variations) > 0:
            prompt_msg = "Sorry, I couldn't find that exact variant. " + prompt_msg
            
    user_context["resolved_attributes"] = resolved_attributes

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
    # ── New params for cart confirmation flow ──
    user_context=None,
    conversation=None,
):
    VARIATION_INTENTS = {
        Intent.PRODUCT_SEARCH,
        Intent.PRODUCT_DETAIL,
        Intent.PRODUCT_VARIATIONS,
        Intent.PRODUCT_ATTRIBUTE_INFO,
    }
    if not (intent in VARIATION_INTENTS and entities.product_id):
        return None

    parent_product_raw = None
    variations_raw = []

    for resp in api_responses:
        if not resp.get("success"):
            continue
        data = resp.get("data")

        if isinstance(data, dict) and "products" in data:
            items = data["products"]
        elif isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
        else:
            items = []

        for item in items:
            if isinstance(item, dict) and item.get("id") == entities.product_id:
                parent_product_raw = item
                break

        if not parent_product_raw and items and isinstance(items[0], dict) and items[0].get("parent_id") is not None:
            variations_raw = items

        if parent_product_raw and parent_product_raw.get("variations"):
            variations_raw = parent_product_raw.get("variations")

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
            entities.category_name = actual_cats

    has_attributes = bool(entities.attributes)

    # ── Resolved variation tracking (new) ──
    resolved_variation = None   # set when we narrow to exactly one variant
    matched_variation = None    # kept for metadata compat

    # ── Product list assembly ──
    if intent == Intent.PRODUCT_ATTRIBUTE_INFO:
        products = [parent_formatted]
        logger.info("Step 3.7: Attribute Info requested. Returning ONLY parent product card.")

    elif variations_raw and has_attributes:
        if intent == Intent.PRODUCT_DETAIL:
            matched_variation = match_variation_to_entities(variations_raw, entities)
            if matched_variation:
                logger.info(
                    f"Step 3.7: Matched variation id={matched_variation.get('id')} | "
                    f"attrs={[a.get('option') for a in matched_variation.get('attributes', [])]}"
                )
                resolved_variation = matched_variation
                variation_products = [format_variation(matched_variation, parent_product_raw)]
            else:
                logger.info("Step 3.7: match_variation_to_entities found no match — falling back to filtered list")
                filtered_vars = _filter_variations_by_entities(variations_raw, entities)
                variation_products = [format_variation(v, parent_product_raw) for v in filtered_vars]

        else:
            # PRODUCT_VARIATIONS / PRODUCT_SEARCH with attributes
            # ── Try exact match first before falling back to filtered list ──
            matched_variation = match_variation_to_entities(variations_raw, entities)
            if matched_variation:
                logger.info(
                    f"Step 3.7: Exact match found id={matched_variation.get('id')} "
                    f"for intent={intent.value}"
                )
                resolved_variation = matched_variation
                variation_products = [format_variation(matched_variation, parent_product_raw)]
            else:
                filtered_vars = _filter_variations_by_entities(variations_raw, entities)
                if not filtered_vars and variations_raw:
                    start = (page - 1) * DEFAULT_PER_PAGE
                    filtered_vars = variations_raw[start: start + DEFAULT_PER_PAGE]
                # If filtering still yields exactly one, treat as resolved
                if len(filtered_vars) == 1:
                    resolved_variation = filtered_vars[0]
                    logger.info(
                        f"Step 3.7: Filter narrowed to single variation "
                        f"id={resolved_variation.get('id')}"
                    )
                variation_products = [format_variation(v, parent_product_raw) for v in filtered_vars]

        products = [parent_formatted] + variation_products

    elif variations_raw:
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

    # ── Bot message ──
    should_show_summary = (
        intent == Intent.PRODUCT_ATTRIBUTE_INFO
        or (intent == Intent.PRODUCT_VARIATIONS and not has_attributes)
    )

    if should_show_summary and (variations_raw or parent_product_raw):
        has_full_options = False
        for pa in parent_product_raw.get("attributes", []):
            if isinstance(pa, dict) and pa.get("options"):
                has_full_options = True
                break

        if not has_full_options:
            logger.info(f"Step 3.7: Parent attributes missing options. Fetching full product {entities.product_id}.")
            parent_call = WooAPICall(
                method="GET",
                endpoint=f"{WOO_BASE_URL}/products/{entities.product_id}",
                params={},
                description=f"Fetch full parent product attributes for '{parent_formatted['name']}'"
            )
            parent_resp = woo_client.execute(parent_call)
            if parent_resp.get("success"):
                full_parent_data = parent_resp.get("data", {})
                parent_product_raw["attributes"] = full_parent_data.get("attributes", [])

        attr_values = {}
        for pa in parent_product_raw.get("attributes", []):
            if isinstance(pa, dict) and pa.get("name"):
                pa_name = pa.get("name").replace("pa_", "").replace("-", " ").title()
                if pa_name not in attr_values:
                    attr_values[pa_name] = set()
                for opt in pa.get("options", []):
                    if str(opt).strip():
                        attr_values[pa_name].add(str(opt).strip())

        for var in variations_raw:
            opts = _get_safe_options(var.get("attributes", []))
            for k, v in opts.items():
                if k and str(v).strip():
                    if k not in attr_values:
                        attr_values[k] = set()
                    attr_values[k].add(str(v).strip())

        if attr_values:
            target_attrs = getattr(entities, 'target_attributes', [])

            if target_attrs and intent == Intent.PRODUCT_ATTRIBUTE_INFO:
                target_attrs_lower = [t.lower() for t in target_attrs]
                filtered_attr_values = {
                    k: v for k, v in attr_values.items()
                    if any(t in k.lower() or k.lower() in t for t in target_attrs_lower)
                }
                if filtered_attr_values:
                    attr_values = filtered_attr_values
                    intro_text = f"Here are the requested options for **{parent_formatted['name']}**:\n\n"
                else:
                    intro_text = f"I couldn't find those specific options, but here are all available options for **{parent_formatted['name']}**:\n\n"
            else:
                intro_text = f"Here are all the available options for **{parent_formatted['name']}**:\n\n"

            attr_summary = [
                f"• **{k}**: {', '.join(sorted(v_set))}"
                for k, v_set in attr_values.items() if v_set
            ]
            summary_text = intro_text + "\n".join(attr_summary)

            if intent == Intent.PRODUCT_ATTRIBUTE_INFO:
                bot_message = summary_text
            else:
                bot_message = f"{summary_text}\n\n{generate_bot_message(intent, entities, products, confidence, order_data)}"
        else:
            bot_message = generate_bot_message(intent, entities, products, confidence, order_data)

    else:
        bot_message = generate_bot_message(intent, entities, products, confidence, order_data)

    if category_mismatch_msg:
        bot_message = f"⚠️ {category_mismatch_msg}\n\n{bot_message}"

    if intent == Intent.PRODUCT_VARIATIONS and not variations_raw:
        if parent_product_raw.get("type", "simple") != "variable":
            bot_message = (
                f"**{parent_formatted['name']}** is a single standard product "
                f"and doesn't come in multiple variations. Here's the item:"
            )

    # ── Cart confirmation override when single variation resolved ──
    next_flow_state = FlowState.IDLE.value
    suggestions = generate_suggestions(intent, entities, products)

    if resolved_variation and user_context is not None and conversation is not None:

        # ── parent_product_raw from advanced search has stripped attributes.
        # Check if variation flags are present; if not, fetch the full product
        # so all_variation_axes is correctly populated before the wildcard check.
        has_variation_flags = any(
            isinstance(attr, dict) and "variation" in attr
            for attr in parent_product_raw.get("attributes", [])
        )
        if not has_variation_flags:
            logger.info(
                f"Step 3.7: parent attributes missing variation flags — "
                f"fetching full product {parent_product_raw.get('id')} for wildcard validation."
            )
            _full_parent_call = WooAPICall(
                method="GET",
                endpoint=f"{WOO_BASE_URL}/products/{parent_product_raw.get('id')}",
                params={},
                description="Fetch full parent product attributes for wildcard check",
            )
            _full_parent_resp = woo_client.execute(_full_parent_call)
            if _full_parent_resp.get("success"):
                parent_product_raw["attributes"] = (
                    _full_parent_resp.get("data", {}).get("attributes", [])
                )

        # ── Wildcard detection ─────────────────────────────────────────────────
        # WooCommerce requires ALL variation axes in the cart payload, even when a
        # variation only defines a subset (leaving others as wildcards).
        # Detect missing axes and ask the user before going to cart confirmation.
        var_attrs = _get_safe_options(resolved_variation.get("attributes", []))
        all_variation_axes = {
            attr.get("name", "").replace("pa_", "").replace("-", " ").title()
            for attr in parent_product_raw.get("attributes", [])
            if isinstance(attr, dict) and attr.get("variation")
        }
        missing_axes = all_variation_axes - set(var_attrs.keys())

        if missing_axes:
            # ── Partial match: collect remaining axes first ────────────────────
            logger.info(
                f"Step 3.7: Variation {resolved_variation.get('id')} is wildcard — "
                f"missing axes: {missing_axes} — → AWAITING_VARIANT_SELECTION"
            )
            user_context["pending_product_id"]   = parent_product_raw.get("id")
            user_context["pending_variation_id"] = resolved_variation.get("id")
            user_context["pending_product_name"] = parent_formatted["name"]
            user_context["resolved_attributes"]  = var_attrs  # e.g. {"Colors": "VIRGINIA Angora"}
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

            bot_message = build_variant_prompt(
                parent_product_raw, parent_formatted["name"], var_attrs, variations_raw
            )
            suggestions = ["Cancel"]
            next_flow_state = FlowState.AWAITING_VARIANT_SELECTION.value

        else:
            # ── Fully specified: go to cart confirmation ───────────────────────
            variation_name = (
                resolved_variation.get("variation_label")
                or " / ".join(var_attrs.values())
                or "this variant"
            )
            pending_name = f"{parent_formatted['name']} — {variation_name}"

            user_context["pending_product_id"]   = parent_product_raw.get("id")
            user_context["pending_variation_id"] = resolved_variation.get("id")
            user_context["pending_product_name"] = pending_name
            user_context["resolved_attributes"]  = var_attrs
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

            bot_message = (
                f"Here's **{pending_name}**. ✅ In stock\n\n"
                f"Would you like to add it to your cart?"
            )
            suggestions = ["Yes, add it", "No thanks"]
            next_flow_state = FlowState.AWAITING_CART_CONFIRMATION.value
            logger.info(
                f"Step 3.7: Single variation fully resolved — "
                f"product_id={parent_product_raw.get('id')} "
                f"variation_id={resolved_variation.get('id')} "
                f"→ AWAITING_CART_CONFIRMATION"
            )

    # ── Pagination ──
    if variations_raw and not has_attributes and intent != Intent.PRODUCT_ATTRIBUTE_INFO:
        total_variations = len(variations_raw)
        total_pages = max(1, -(-total_variations // DEFAULT_PER_PAGE))
        pagination = {
            "page": page,
            "per_page": DEFAULT_PER_PAGE,
            "total_items": total_variations,
            "total_pages": total_pages,
            "has_more": page < total_pages,
        }
    else:
        pagination = build_pagination(page, api_responses, api_calls_to_execute)

    # ── Metadata ──
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
        **(
            {
                "matched_variation_id": matched_variation.get("id"),
                "matched_variation_price": (
                    matched_variation.get("sale_price")
                    or matched_variation.get("price")
                    or matched_variation.get("regular_price")
                ),
            }
            if matched_variation else {}
        ),
    }

    return jsonify({
        "success":     True,
        "bot_message": bot_message,
        "intent":      intent.value,
        "products":    products,
        "suggestions": suggestions,
        "session_id":  session_id,
        "metadata":    metadata,
        "pagination":  pagination,
        "flow_state":  next_flow_state,
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

    product = products_formatted[0]  # initial candidate

    # Guard: verify the result actually matches what the user asked for
    _requested_name = getattr(entities, 'order_item_name', None) or getattr(entities, 'product_name', None)
    if _requested_name and products_formatted:
        _req_lower = _requested_name.lower()
        _name_match = next(
            (p for p in products_formatted
            if _req_lower in p.get('name', '').lower()
            or p.get('name', '').lower() in _req_lower),
            None
        )
        if _name_match:
            product = _name_match
        else:
            logger.warning(
                f"Step 5.5: Name mismatch — user requested '{_requested_name}' but "
                f"API returned {[p.get('name') for p in products_formatted]}. Aborting order flow."
            )
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": (
                    f"I couldn't find **{_requested_name}** in our catalog. "
                    "Could you double-check the product name, or would you like to browse the catalog?"
                ),
                "intent": intent.value,
                "products": [],
                "suggestions": ["Browse catalog", "Show all products"],
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.IDLE.value,
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.IDLE.value,
                "pagination": default_pagination(page),
            }), 200
    
    logger.info(
        f"🛑 DEBUG STEP 5.5 | Product: {product.get('name')} | "
        f"Type: {product.get('type')} | "
        f"Variations Count: {len(product.get('variations', []))}"
    )
    
    # ── OUT OF STOCK INTERCEPT ──
    if product.get("stock_status") == "outofstock":
        elapsed = time.time() - start_time
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
        
    # Pre-fetch full attributes for the prompt if the custom API stripped them!
    _raw_for_prompt = {}
    _variations_for_cache = []
    if product.get("type") == "variable" or product.get("variations"):
        _raw_for_prompt = next((p for p in all_products_raw if not p.get("parent_id")), {})
        
        has_full_options = False
        for pa in _raw_for_prompt.get("attributes", []):
            if isinstance(pa, dict) and pa.get("options"):
                has_full_options = True
                break
                
        if not has_full_options:
            logger.info(f"Step 5.5: Parent attributes missing options. Fetching full product {product.get('id')} from Woo API.")
            parent_call = WooAPICall(
                method="GET",
                endpoint=f"{WOO_BASE_URL}/products/{product.get('id')}",
                params={},
                description=f"Fetch full parent product attributes for '{product['name']}'"
            )
            parent_resp = woo_client.execute(parent_call)
            if parent_resp.get("success"):
                _raw_for_prompt["attributes"] = parent_resp.get("data", {}).get("attributes", [])
                
        _variations_for_cache = _raw_for_prompt.get("variations", [])

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
            # Pass _variations_for_cache
            prompt_msg = build_variant_prompt(_raw_for_prompt, product["name"], getattr(entities, 'attributes', {}), _variations_for_cache)
            elapsed = time.time() - start_time
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

        # --- FAST TRACK: QUANTITY AND VARIANT BOTH RESOLVED → cart confirmation ---
        if len(_variations_for_cache) == 1:
            _resolved_var = _variations_for_cache[0]
            if _resolved_var.get("stock_status") == "outofstock" or _resolved_var.get("in_stock") is False:
                elapsed = time.time() - start_time
                
                return jsonify({
                    "success": True,
                    "bot_message": "I'm sorry, but that specific variant is currently out of stock! 😔",
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

            _var_label = " / ".join(_get_safe_options(_resolved_var.get("attributes", [])).values())
            _variant_suffix = f" ({_var_label})" if _var_label else ""
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Got it — add **{product['name']}**{_variant_suffix} ×{entities.quantity} to your cart?"
                ),
                "intent": intent.value,
                "products": products_formatted[:1],
                "suggestions": ["Yes, add it", "No thanks"],
                "session_id": session_id,
                "metadata": {
                    "flow_state":           FlowState.AWAITING_CART_CONFIRMATION.value,
                    "pending_product_id":   product.get("id"),
                    "pending_product_name": product["name"],
                    "pending_quantity":     entities.quantity,
                    "pending_variation_id": _resolved_var["id"],
                    "response_time_ms":     round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
                "pagination": default_pagination(page),
            }), 200

        # --- NORMAL: NEEDS VARIANT SELECTION ---
        from handlers.chat_utils import build_variant_prompt
        prompt_msg = build_variant_prompt(_raw_for_prompt, product["name"], getattr(entities, 'attributes', {}), _variations_for_cache)
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": prompt_msg,
            "intent": intent.value,
            "products": products_formatted[:1],
            "suggestions": ["Cancel Order"],
            "session_id": session_id,
            "metadata": {
                "flow_state":           FlowState.AWAITING_VARIANT_SELECTION.value,
                "pending_product_id":   product.get("id"),
                "pending_product_name": product["name"],
                "pending_quantity":     entities.quantity,
                "response_time_ms":     round(elapsed * 1000),
            },
            "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
            "pagination": default_pagination(page),
        }), 200

    return None