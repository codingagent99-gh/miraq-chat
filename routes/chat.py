"""
Chat endpoint as a Flask Blueprint.
"""

import time
import uuid
from datetime import datetime, timezone
import os
from flask import Blueprint, request, jsonify
from models import ExtractedEntities, ClassifiedResult

from app_config import (
    ORDER_INTENTS,
    ORDER_CREATE_INTENTS,
    CLASSIFIER_PROVIDER_TAG,
    get_currency_symbol,
)
from woo_client import woo_client
from formatters import format_product, format_custom_product, format_category, _entities_to_dict
from response_generator import generate_bot_message, generate_suggestions, _resolve_user_placeholders
from session_store import sessions, touch_session
from models import Intent, WooAPICall
from classifier import classify
from api_builder import build_api_calls
from conversation_flow import FlowState, handle_flow_state
from chat_logger import get_logger, sanitize_log_string
from store_registry import get_store_loader

from handlers.chat_utils import default_pagination, build_pagination, format_order_for_frontend
from handlers.flow_handler import handle_flow
from handlers.llm_handler import run_llm_fallback
from handlers.order_handler import handle_reorder, handle_order_detail, handle_quick_order, handle_historical_search
from handlers.variant_handler import handle_variant_selection, handle_variation_product, handle_quantity_and_variant_check
from handlers.search_handler import log_matched_products, handle_empty_results

logger = get_logger("miraq_chat")
chat_bp = Blueprint("chat", __name__)

def parse_csv_message(msg: str, loader) -> ClassifiedResult | None:
    """Hybrid Parser: Secures exact matches first, uses NLP on leftovers, then Vector AI for synonyms."""
    
    terms = [t.strip().lower() for t in msg.split(",") if t.strip()]
    if not terms:
        return None
        
    entities = ExtractedEntities()
    if not hasattr(entities, 'target_category_slugs'):
        entities.target_category_slugs = set()
        
    unmatched_terms = []
    
    # ─── PHASE 1: EXACT MATCH SECURE ───
    for term in terms:
        term_matched = False
        
        # 1. Product Check
        if loader and hasattr(loader, 'product_by_name_lower') and term in loader.product_by_name_lower:
            prod = loader.product_by_name_lower[term]
            entities.product_name = prod.get("name")
            entities.product_slug = prod.get("slug", "")
            entities.product_id = prod.get("id")
            term_matched = True

        # 2. Category Check
        elif loader and term in getattr(loader, 'category_by_name_lower', {}):
            cat = loader.category_by_name_lower[term]
            entities.target_category_slugs.add(cat.get("slug"))
            if not getattr(entities, 'category_name', None):
                entities.category_name = cat.get("name")
            term_matched = True
            
        # 3. Tag Check
        elif loader and term in getattr(loader, 'tag_by_name_lower', {}):
            tag = loader.tag_by_name_lower[term]
            entities.tag_slugs.append(tag.get("slug"))
            entities.tag_ids.append(tag.get("id"))
            term_matched = True
            
        # 4. Attribute Check
        elif loader and hasattr(loader, 'all_attributes_raw'):
            for attr in loader.all_attributes_raw:
                label = attr.get("attribute_label", "").lower().strip()
                for attr_val in attr.get("terms", []):
                    if term == attr_val.get("name", "").lower():
                        term_slug = attr_val.get("slug")
                        if label not in entities.attributes:
                            entities.attributes[label] = term_slug
                        else:
                            entities.attributes[label] += f",{term_slug}"
                        term_matched = True
                        break
                if term_matched:
                    break
        
        # If the term failed, save it for the NLP AI!
        if not term_matched:
            unmatched_terms.append(term)

    # SAFETY LOCK 1: If EVERYTHING matched perfectly, exit early
    if not unmatched_terms:
        resolved_intent = Intent.PRODUCT_SEARCH if entities.product_id else Intent.FILTER_BY_ATTRIBUTE
        return ClassifiedResult(intent=resolved_intent, entities=entities, confidence=1.0)
        
    # ─── PHASE 2: REGEX NLP (Joined String) ───
    from classifier import classify 
    
    fallback_text = ", ".join(unmatched_terms)
    nlp_result = classify(fallback_text)
    nlp_entities = nlp_result.entities
    
    # MERGE PRODUCT
    if nlp_entities.product_id and not entities.product_id:
        entities.product_name = nlp_entities.product_name
        entities.product_slug = nlp_entities.product_slug
        entities.product_id = nlp_entities.product_id
        
    # MERGE CATEGORIES
    if hasattr(nlp_entities, 'target_category_slugs') and nlp_entities.target_category_slugs:
        entities.target_category_slugs.update(nlp_entities.target_category_slugs)
        if not getattr(entities, 'category_name', None):
            entities.category_name = nlp_entities.category_name
            
    # MERGE TAGS
    for tid, tslug in zip(nlp_entities.tag_ids, nlp_entities.tag_slugs):
        if tid not in entities.tag_ids:
            entities.tag_ids.append(tid)
            entities.tag_slugs.append(tslug)
            
    # MERGE ATTRIBUTES
    if nlp_entities.attributes:
        for k, v in nlp_entities.attributes.items():
            if k not in entities.attributes:
                entities.attributes[k] = v
            else:
                if v not in entities.attributes[k]:
                    entities.attributes[k] += f",{v}"
                    
    # MERGE EXCLUSIONS
    if getattr(nlp_entities, 'excluded_tags', None):
        if not hasattr(entities, 'excluded_tags'): entities.excluded_tags = []
        entities.excluded_tags.extend(nlp_entities.excluded_tags)
        
    if getattr(nlp_entities, 'excluded_categories', None):
        if not hasattr(entities, 'excluded_categories'): entities.excluded_categories = []
        entities.excluded_categories.extend(nlp_entities.excluded_categories)
        
    if getattr(nlp_entities, 'excluded_attributes', None):
        if not hasattr(entities, 'excluded_attributes'): entities.excluded_attributes = {}
        for k, v in nlp_entities.excluded_attributes.items():
            if k not in entities.excluded_attributes: entities.excluded_attributes[k] = v
            else: entities.excluded_attributes[k].extend(v)
            
    if getattr(nlp_entities, 'excluded_search_term', None):
        entities.excluded_search_term = nlp_entities.excluded_search_term
                
    # MERGE SEMANTIC MATCHES
    if nlp_entities.semantic_matches:
        entities.semantic_matches.extend(nlp_entities.semantic_matches)
                                   
    # ─── PHASE 2.5: SEMANTIC VECTOR FALLBACK (True AI) ───
    still_unmatched_pos = []
    still_unmatched_neg = []
    
    has_leftovers = getattr(nlp_entities, 'search_term', None) or getattr(nlp_entities, 'excluded_search_term', None)
    
    if has_leftovers and loader:
        import torch
        from sentence_transformers import util
        SEMANTIC_THRESHOLD = 0.55 
        
        if not hasattr(loader, 'semantic_tensors') or loader.semantic_tensors is None:
            logger.warning("Phase 2.5: Vector AI skipped because semantic_tensors is None!")
            if getattr(nlp_entities, 'search_term', None): still_unmatched_pos.extend(nlp_entities.search_term.split(","))
            if getattr(nlp_entities, 'excluded_search_term', None): still_unmatched_neg.extend(nlp_entities.excluded_search_term.split(","))
        else:
            def _process_vectors(term_string, is_negative=False):
                unmatched = []
                chunks = [t.strip() for t in term_string.split(",") if t.strip()]
                for term in chunks:
                    user_vector = loader.vector_model.encode(term, convert_to_tensor=True)
                    cosine_scores = util.cos_sim(user_vector, loader.semantic_tensors)[0]
                    top_results = torch.topk(cosine_scores, k=3)
                    
                    candidates = []
                    for score, idx in zip(top_results[0], top_results[1]):
                        if score.item() >= SEMANTIC_THRESHOLD:
                            matched_slug = loader.semantic_keys[idx]
                            candidate_data = loader.semantic_dictionary[matched_slug].copy()
                            candidate_data["user_text"] = term
                            candidate_data["score"] = score.item()
                            candidate_data["is_negative"] = is_negative
                            candidates.append(candidate_data)
                    
                    if candidates:
                        entities.semantic_matches.append(candidates)
                        logger.info(f"Vector Match: '{term}' mapped to '{candidates[0]['suggested_name']}' (Score: {candidates[0]['score']:.2f} | Negative: {is_negative})")
                    else:
                        unmatched.append(term)
                return unmatched

            if getattr(nlp_entities, 'search_term', None):
                still_unmatched_pos = _process_vectors(nlp_entities.search_term, is_negative=False)
            if getattr(nlp_entities, 'excluded_search_term', None):
                still_unmatched_neg = _process_vectors(nlp_entities.excluded_search_term, is_negative=True)
            
    elif not has_leftovers and fallback_text and not nlp_entities.product_id and not getattr(nlp_entities, 'target_category_slugs', None) and not nlp_entities.tag_slugs and not nlp_entities.attributes and not nlp_entities.semantic_matches:
        still_unmatched_pos = [fallback_text]

    entities.search_term = ", ".join(still_unmatched_pos) if still_unmatched_pos else None
    entities.excluded_search_term = ", ".join(still_unmatched_neg) if still_unmatched_neg else None

    # SAFETY LOCK 2: Preserve conversational AI intents
    CATALOG_INTENTS = {Intent.FILTER_BY_ATTRIBUTE, Intent.PRODUCT_SEARCH, Intent.PRODUCT_VARIATIONS, Intent.UNKNOWN}
    
    if nlp_result.intent in CATALOG_INTENTS:
        resolved_intent = Intent.PRODUCT_SEARCH if entities.product_id else Intent.FILTER_BY_ATTRIBUTE
    else:
        resolved_intent = nlp_result.intent
    
    final_confidence = max(nlp_result.confidence, 0.95)
    
    return ClassifiedResult(intent=resolved_intent, entities=entities, confidence=final_confidence)

@chat_bp.route("/chat", methods=["POST"])
def chat():
    start_time = time.time()

    # ─── Parse request ───
    body = request.get_json(silent=True)
    if not body:
        logger.warning("POST /chat | Invalid JSON body")
        return jsonify({
            "success": False,
            "bot_message": "Invalid request. Send JSON with 'message' field.",
            "intent": "error",
            "products": [],
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": "",
            "metadata": {"error": "Invalid JSON body"},
            "pagination": default_pagination(),
        }), 400

    message = body.get("message", "").strip()
    session_id = body.get("session_id", "").strip()
    if not session_id:
        session_id = f"auto-{uuid.uuid4().hex}"
        logger.info(f"POST /chat | No session_id received — generated: {session_id}")

    user_context = body.get("user_context", {})
    page = int(body.get("page", 1))

    truncated_msg = message[:100] + "..." if len(message) > 100 else message
    sanitized_msg = sanitize_log_string(truncated_msg)
    customer_id = user_context.get("customer_id")
    flow_state = user_context.get("flow_state", "idle")
    logger.info(f'POST /chat | session={session_id} | message="{sanitized_msg}" | customer_id={customer_id} | flow_state={flow_state}')

    if not message:
        logger.warning(f"POST /chat | session={session_id} | Empty message")
        return jsonify({
            "success": False,
            "bot_message": "Please type a message! Try asking about our products, categories, or your orders.",
            "intent": "error",
            "products": [],
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": session_id,
            "metadata": {"error": "Empty message"},
            "pagination": default_pagination(page),
        }), 400

    # ─── Update session ───
    if session_id:
        if session_id not in sessions:
            sessions[session_id] = {
                "history": [],
                "user_context": user_context,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_active": datetime.now(timezone.utc).timestamp(),
            }
        sessions[session_id]["history"].append({
            "role": "user",
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        touch_session(session_id)

    # ─── Step 0.5: Suggestion retry intercept ───
    _suggestion_retry = body.get("suggestion_retry")
    if _suggestion_retry:
        from store_registry import get_store_loader as _get_loader

        _sr_label = _suggestion_retry.get("label", "suggestion retry")
        logger.info(
            f"Step 0.5: Suggestion retry | session={session_id} | label={_sr_label!r}"
        )

        _loader = _get_loader()
        _sr_entities = ExtractedEntities()

        _cat_slug = _suggestion_retry.get("category_slug", "")
        if _cat_slug and _loader:
            _cat_entry = _loader.category_by_slug.get(_cat_slug)
            if _cat_entry:
                _sr_entities.category_name = _cat_entry["name"]
                _sr_entities.category_id = _cat_entry["id"]

        for _extra_slug in (_suggestion_retry.get("extra_category_slugs") or []):
            if _loader:
                _extra_entry = _loader.category_by_slug.get(_extra_slug)
                if _extra_entry:
                    _sr_entities.extra_category_ids.append(_extra_entry["id"])

        for _tslug in (_suggestion_retry.get("tag_slugs") or []):
            _sr_entities.tag_slugs.append(_tslug)
            if _loader:
                _tid = _loader.get_tag_id_by_slug(_tslug)
                if _tid:
                    _sr_entities.tag_ids.append(_tid)

        _sr_entities.attributes = dict(_suggestion_retry.get("attributes") or {})

        _sr_intent = Intent.FILTER_BY_ATTRIBUTE
        _sr_confidence = 1.0
        _sr_result = ClassifiedResult(
            intent=_sr_intent,
            entities=_sr_entities,
            confidence=_sr_confidence,
        )

        _sr_customer_id = user_context.get("customer_id")
        _sr_api_calls = build_api_calls(_sr_result, page, user_message=message, session_id=session_id, customer_id=_sr_customer_id)

        if _sr_customer_id:
            _resolve_user_placeholders(_sr_api_calls, _sr_customer_id)

        _sr_responses = woo_client.execute_all(_sr_api_calls)
        _sr_products_raw = []
        for _r in _sr_responses:
            if _r.get("success"):
                _d = _r.get("data")
                if isinstance(_d, dict) and "products" in _d:
                    _sr_products_raw.extend(_d["products"])
                elif isinstance(_d, list):
                    _sr_products_raw.extend(_d)
                elif isinstance(_d, dict):
                    _sr_products_raw.append(_d)

        _sr_formatted = []
        for _p in _sr_products_raw:
            if _p.get("parent_id"):
                continue
            if "featured_image" in _p:
                _sr_formatted.append(format_custom_product(_p))
            else:
                _sr_formatted.append(format_product(_p))
        _sr_formatted = [p for p in _sr_formatted if p.get("name")]

        _sr_bot_message = (
            f"Here are results for **{_sr_label}**:"
            if _sr_formatted
            else f"No products found for **{_sr_label}** either. Try a different filter."
        )
        _sr_pagination = build_pagination(page, _sr_responses, _sr_api_calls)
        elapsed = time.time() - start_time

        if session_id and session_id in sessions:
            sessions[session_id]["history"].append({
                "role": "bot",
                "message": _sr_bot_message,
                "intent": "suggestion_retry",
                "products_count": len(_sr_formatted),
            })

        return jsonify({
            "success": True,
            "bot_message": _sr_bot_message,
            "intent": "filter",
            "products": _sr_formatted,
            "suggestions": [],
            "filter_suggestions": [],
            "session_id": session_id,
            "metadata": {
                "confidence": 1.0,
                "products_count": len(_sr_formatted),
                "provider": CLASSIFIER_PROVIDER_TAG,
                "response_time_ms": round(elapsed * 1000),
                "intent_raw": "suggestion_retry",
            },
            "pagination": _sr_pagination,
            "flow_state": FlowState.IDLE.value,
        }), 200

    # ─── Step 0: Check conversation flow state ───
    flow_state_str = user_context.get("flow_state", "idle")
    try:
        current_flow_state = FlowState(flow_state_str)
    except ValueError:
        current_flow_state = FlowState.IDLE
    logger.info(f"Step 0: Flow state={current_flow_state.value}")

    # ══════════════════════════════════════════════════════════════
    # ─── STEP 0.8: VECTOR MATCH RESOLUTION BYPASS ───
    # ══════════════════════════════════════════════════════════════
    _skip_classification = False
    bypass_result = None

    if current_flow_state == FlowState.AWAITING_FILTER_CLARIFICATION:
        pending_semantic = user_context.get("pending_semantic_match")
        if pending_semantic:
            msg_lower = message.lower().strip()
            
            is_accept = (
                msg_lower == "yes - use these filters" or
                msg_lower.startswith("yes - use ") or
                msg_lower.startswith("yes - exclude ") or
                msg_lower in ["yes", "y", "yep", "sure", "ok"]
            )
            
            is_reject = (
                msg_lower.startswith("no - ") or
                msg_lower in ["no", "n", "nope"]
            )
            
            is_cancel = msg_lower in ["cancel", "exit", "stop", "nevermind", "never mind", "abort", "start over"]

            if is_cancel:
                logger.info("Step 0.8: User cancelled semantic match. Purging backpack and triggering escape hatch.")
                user_context.pop("pending_semantic_match", None)
                
            if is_accept or is_reject:
                logger.info(f"Step 0.8: User responded to semantic match (Accept={is_accept}). Bypassing NLP.")
                entities = ExtractedEntities()
                entities.target_category_slugs = set()
                
                if is_accept:
                    options_to_apply = []
                    if "options" in pending_semantic:
                        if len(pending_semantic["options"]) > 1 and ("use " in msg_lower or "exclude " in msg_lower):
                            selected_name = message.replace("Yes - use ", "").replace("Yes - exclude ", "").replace("Use ", "").replace("Exclude ", "").strip()
                            selected_match = next((opt for opt in pending_semantic["options"] if opt["suggested_name"].lower() == selected_name.lower()), pending_semantic["options"][0])
                            options_to_apply.append(selected_match)
                        else:
                            options_to_apply.extend(pending_semantic["options"])
                            
                    options_to_apply.extend(pending_semantic.get("extra_semantics", []))
                    
                    for opt in options_to_apply:
                        is_neg = opt.get("is_negative", False)
                        if opt["type"] == "tag":
                            if is_neg:
                                if not hasattr(entities, 'excluded_tags'): entities.excluded_tags = []
                                entities.excluded_tags.append(opt["slug"])
                            else:
                                entities.tag_slugs.append(opt["slug"])
                        elif opt["type"] == "category":
                            if is_neg:
                                if not hasattr(entities, 'excluded_categories'): entities.excluded_categories = []
                                entities.excluded_categories.append(opt["slug"])
                            else:
                                entities.target_category_slugs.add(opt["slug"])
                                entities.category_name = opt["suggested_name"]
                        elif opt["type"] == "attribute":
                            taxonomy = opt["taxonomy"]
                            if is_neg:
                                if not hasattr(entities, 'excluded_attributes'): entities.excluded_attributes = {}
                                if taxonomy not in entities.excluded_attributes: entities.excluded_attributes[taxonomy] = []
                                entities.excluded_attributes[taxonomy].append(opt["slug"])
                            else:
                                if taxonomy not in entities.attributes:
                                    entities.attributes[taxonomy] = opt["slug"]
                                else:
                                    if opt["slug"] not in entities.attributes[taxonomy].split(","):
                                        entities.attributes[taxonomy] += f",{opt['slug']}"

                elif is_reject:
                    entities.search_term = pending_semantic.get("options", [{}])[0].get("user_text", "")
                    if "rejected_semantic_terms" not in user_context:
                        user_context["rejected_semantic_terms"] = []
                    for opt in pending_semantic.get("options", []):
                        user_context["rejected_semantic_terms"].append(opt["suggested_name"])


                # Backpack Retrieval
                leftovers = pending_semantic.get("pending_other_semantics", [])
                if leftovers: entities.semantic_matches.extend(leftovers)

                if pending_semantic.get("carryover_search_term"): entities.search_term = pending_semantic["carryover_search_term"]
                if pending_semantic.get("carryover_tags"): entities.tag_slugs.extend(pending_semantic["carryover_tags"])
                if pending_semantic.get("carryover_categories"):
                    entities.target_category_slugs.update(pending_semantic["carryover_categories"])
                if pending_semantic.get("carryover_category_name"): entities.category_name = pending_semantic["carryover_category_name"]
                if pending_semantic.get("carryover_attributes"):
                    for k, v in pending_semantic["carryover_attributes"].items():
                        if k not in entities.attributes: entities.attributes[k] = v
                        else:
                            if v not in entities.attributes[k].split(","): entities.attributes[k] += f",{v}"
                            
                # Unpack the Exclusions
                if pending_semantic.get("carryover_excluded_tags"):
                    if not hasattr(entities, 'excluded_tags'): entities.excluded_tags = []
                    entities.excluded_tags.extend(pending_semantic["carryover_excluded_tags"])
                if pending_semantic.get("carryover_excluded_categories"):
                    if not hasattr(entities, 'excluded_categories'): entities.excluded_categories = []
                    entities.excluded_categories.extend(pending_semantic["carryover_excluded_categories"])
                if pending_semantic.get("carryover_excluded_attributes"):
                    if not hasattr(entities, 'excluded_attributes'): entities.excluded_attributes = {}
                    for k, v in pending_semantic["carryover_excluded_attributes"].items():
                        if k not in entities.excluded_attributes: entities.excluded_attributes[k] = v
                        else: entities.excluded_attributes[k].extend(v)

                # Reset the state so handle_flow_state ignores it, and flag the bypass
                current_flow_state = FlowState.IDLE
                user_context["flow_state"] = FlowState.IDLE.value
                user_context.pop("pending_semantic_match", None)
                
                # Determine intent
                bypass_intent = Intent.PRODUCT_SEARCH if getattr(entities, 'product_id', None) else Intent.FILTER_BY_ATTRIBUTE
                bypass_result = ClassifiedResult(intent=bypass_intent, entities=entities, confidence=0.98)
                _skip_classification = True


    flow_context = {
        "pending_product_name": user_context.get("pending_product_name"),
        "pending_product_id": user_context.get("pending_product_id"),
        "pending_quantity": user_context.get("pending_quantity"),
        "pending_variation_id": user_context.get("pending_variation_id"),
        "resolved_attributes": user_context.get("resolved_attributes"),
    }

    flow_result = None
    if current_flow_state != FlowState.IDLE:
        flow_result = handle_flow_state(
            state=current_flow_state,
            message=message,
            entities=flow_context,
            confidence=0.0,
        )
        if flow_result and flow_result.get("override_message"):
            message = flow_result["override_message"]

    if flow_result and not flow_result.get("pass_through") and not flow_result.get("override_message"):
        resp = handle_flow(flow_result, user_context, session_id, customer_id, page, start_time, sessions)
        if resp: return resp

    if flow_result:
        resp = handle_flow(flow_result, user_context, session_id, customer_id, page, start_time, sessions)
        if resp: return resp

    _resolve_variant = bool(flow_result and flow_result.get("resolve_variant"))

    # ─── Steps 1–3: Pipeline Routing ───
    if _resolve_variant:
        intent = Intent.QUICK_ORDER
        entities = ExtractedEntities()
        confidence = 1.0
        result = None
        api_calls = []
        api_calls_to_execute = []
        api_responses = []
        all_products_raw = []
        order_data = []
        customer_id = user_context.get("customer_id")
        last_product_ctx = None
        logger.info("Steps 1-3: Skipped (variant resolution mode)")
        
    elif _skip_classification:
        # THE BYPASS
        result = bypass_result
        intent = result.intent
        entities = result.entities
        confidence = result.confidence
        logger.info(f"Step 1: NLP Bypassed. Using semantic clarification intent={intent.value}")
        
    else:
        # ─── NORMAL ROUTE ───
        store_loader = get_store_loader()
        result = parse_csv_message(message, store_loader)
        
        if result:
            logger.info("Step 1: Message parsed as exact-match CSV.")
        else:
            result = classify(message)

        intent = result.intent
        entities = result.entities
        confidence = result.confidence

        entity_summary = {
            k: v for k, v in {
                "product_name": entities.product_name,
                "category_name": entities.category_name,
                "product_id": entities.product_id,
                "order_item_name": entities.order_item_name,
                "quantity": entities.quantity,
                "attributes": entities.attributes or None,
                "tag_slugs": entities.tag_slugs or None,
            }.items() if v is not None
        }
        logger.info(f"Step 1: Classified intent={intent.value} | confidence={confidence:.2f} | entities={entity_summary}")

        # LLM Fallback
        session_history = sessions.get(session_id, {}).get("history") if session_id else None
        llm_outcome = run_llm_fallback(
            message=message,
            intent=intent,
            entities=entities,
            confidence=confidence,
            session_id=session_id,
            session_history=session_history,
            store_loader=store_loader,
            page=page,
            start_time=start_time,
            order_create_intents=ORDER_CREATE_INTENTS,
            user_context=user_context,
            sessions=sessions,
        )

        if llm_outcome is not None:
            if not isinstance(llm_outcome, tuple) or not isinstance(llm_outcome[0], tuple):
                if hasattr(llm_outcome, 'get_data'):  
                    return llm_outcome
                if isinstance(llm_outcome, tuple) and len(llm_outcome) == 2 and isinstance(llm_outcome[1], int):
                    return llm_outcome
            if isinstance(llm_outcome, tuple) and len(llm_outcome) == 4:
                intent, entities, confidence, result = llm_outcome

    # ─── PHASE 3: INTERCEPT NEW VECTOR/SEMANTIC MATCHES ───
    if entities.semantic_matches and current_flow_state != FlowState.AWAITING_FILTER_CLARIFICATION:
        rejected_list = user_context.get("rejected_semantic_terms", [])
        valid_term_groups = []
        
        for candidate_list in entities.semantic_matches:
            if isinstance(candidate_list, dict): candidate_list = [candidate_list]
            clean_list = [c for c in candidate_list if c["suggested_name"] not in rejected_list]
            if clean_list: valid_term_groups.append(clean_list)
                
        # Support both new and old variable name from conversation_flow.py to avoid breaking
        reject_flow = flow_result and flow_result.get("reject_semantic_match")
        
        if valid_term_groups and not reject_flow:
            has_ties = any(len(group) > 1 for group in valid_term_groups)
            
            stashed_semantic_data = {
                "carryover_search_term": entities.search_term,
                "carryover_tags": list(entities.tag_slugs),
                "carryover_attributes": entities.attributes,
                "carryover_excluded_tags": getattr(entities, 'excluded_tags', []),
                "carryover_excluded_categories": getattr(entities, 'excluded_categories', []),
                "carryover_excluded_attributes": getattr(entities, 'excluded_attributes', {})
            }
            
            if hasattr(entities, 'target_category_slugs'):
                stashed_semantic_data["carryover_categories"] = list(entities.target_category_slugs)
                stashed_semantic_data["carryover_category_name"] = getattr(entities, 'category_name', None)
            
            suggestion_buttons = []

            if has_ties:
                active_group = valid_term_groups[0]
                user_original_term = active_group[0]["user_text"]
                is_negative = active_group[0].get("is_negative", False)
                
                stashed_semantic_data["options"] = active_group
                stashed_semantic_data["pending_other_semantics"] = valid_term_groups[1:] 
                
                action_word = "EXCLUDE" if is_negative else "USE"
                bot_message = f"I found multiple matches for '{user_original_term}'. Which one did you mean to {action_word}?"
                for candidate in active_group:
                    verb = "Exclude" if candidate.get("is_negative") else "Use"
                    suggestion_buttons.append(f"{verb} {candidate['suggested_name']}")
                suggestion_buttons.append(f"No - search for '{user_original_term}'")
                
            else:
                primary_semantics = [group[0] for group in valid_term_groups]
                stashed_semantic_data["options"] = [primary_semantics[0]] 
                stashed_semantic_data["extra_semantics"] = primary_semantics[1:] 
                
                suggested_names = [f["suggested_name"] for f in primary_semantics]
                is_negative = primary_semantics[0].get("is_negative", False)
                
                if len(suggested_names) == 1:
                    if is_negative:
                        bot_message = f"I don't have an exact match for '{primary_semantics[0]['user_text']}'. Did you mean to **EXCLUDE** {suggested_names[0]}?"
                        suggestion_buttons.append(f"Yes - exclude {suggested_names[0]}")
                    else:
                        bot_message = f"I don't have an exact match for '{primary_semantics[0]['user_text']}', but I do have **{suggested_names[0]}**. Would you like to use that filter?"
                        suggestion_buttons.append(f"Yes - use {suggested_names[0]}")
                    suggestion_buttons.append(f"No - search for '{primary_semantics[0]['user_text']}'")
                else:
                    joined_names = " and ".join(suggested_names)
                    bot_message = f"I don't have exact matches, but I found **{joined_names}**. Would you like to apply these filters?"
                    suggestion_buttons.append("Yes - use these filters")
                    suggestion_buttons.append("No - use my original text")

            suggestion_buttons.append("Cancel")
            
            elapsed = time.time() - start_time
            logger.info(f"Step 1.9: Intercepting API Pipeline! Pausing to resolve semantic match.")
            
            return jsonify({
                "success": True,
                "bot_message": bot_message,
                "intent": "guided_flow",
                "products": [],
                "suggestions": suggestion_buttons,
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.AWAITING_FILTER_CLARIFICATION.value,
                    "pending_semantic_match": stashed_semantic_data,
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_FILTER_CLARIFICATION.value,
                "pagination": default_pagination(page),
            }), 200

    # ─── Step 2: Build API calls ───
    customer_id = user_context.get("customer_id")
    api_calls = build_api_calls(result, page, user_message=message, session_id=session_id, customer_id=customer_id)
    endpoint_summary = [f"{c.method} {c.endpoint.split('/')[-1]}" for c in api_calls]
    logger.info(f"Step 2: Built {len(api_calls)} API call(s) | endpoints={endpoint_summary}")

    if customer_id:
        logger.info(f"Step 2.5: Resolved customer_id={customer_id}")
        _resolve_user_placeholders(api_calls, customer_id)

    last_product_ctx = user_context.get("last_product")
    if last_product_ctx and last_product_ctx.get("id"):
        logger.info(f"Step 2.6: last_product_ctx found: id={last_product_ctx.get('id')}, name=\"{sanitize_log_string(last_product_ctx.get('name', ''))}\"")
    else:
        logger.info("Step 2.6: No last_product_ctx")
        
    # ─── Step 3: Execute API calls ───
    if not _resolve_variant:
        all_products_raw = []
        order_data = []

        filtered_api_calls = []
        if intent in ORDER_CREATE_INTENTS:
            for call in api_calls:
                if call.method == "POST" and "/orders" in call.endpoint:
                    logger.info(f"Step 3: Skipping POST /orders call from api_builder (intent={intent.value}) - Step 3.6 will handle order creation")
                    continue
                filtered_api_calls.append(call)
            api_calls_to_execute = filtered_api_calls
        else:
            api_calls_to_execute = api_calls

        api_responses = woo_client.execute_all(api_calls_to_execute)

        def _enrich_raw_products(prod_list):
            for p in prod_list:
                if "type" not in p:
                    p["type"] = "variable" if p.get("variations") else "simple"

        for resp in api_responses:
            if resp.get("success"):
                data = resp.get("data")
                if isinstance(data, dict) and "products" in data:
                    _enrich_raw_products(data["products"])
                    (order_data if intent in ORDER_INTENTS else all_products_raw).extend(data["products"])
                elif isinstance(data, list):
                    _enrich_raw_products(data)
                    (order_data if intent in ORDER_INTENTS else all_products_raw).extend(data)
                elif isinstance(data, dict):
                    _enrich_raw_products([data])
                    (order_data if intent in ORDER_INTENTS else all_products_raw).append(data)
            else:
                error_msg = sanitize_log_string(str(resp.get('error', 'Unknown')))
                logger.warning(f"Step 3: API call failed | error={error_msg}")

        logger.info(f"Step 3: API execution complete | all_products_raw count={len(all_products_raw)} | order_data count={len(order_data)}")

        log_matched_products(all_products_raw, api_calls_to_execute)
    
    if intent == Intent.FETCH_CUSTOMER:
        elapsed = int((time.time() - start_time) * 1000)
        customer_raw = order_data[0] if order_data else {}
        
        display = {}
        for field_key in entities.customer_fields_requested:
            if "." in field_key:
                section, key = field_key.split(".", 1)
                display[field_key] = customer_raw.get(section, {}).get(key)
            elif field_key == "full_name":
                display["name"] = f"{customer_raw.get('first_name', '')} {customer_raw.get('last_name', '')}".strip()
            else:
                display[field_key] = customer_raw.get(field_key)
        
        lines = [f"**{k}**: {v or 'not set'}" for k, v in display.items()]
        bot_message = "Here's what I have on file:\n" + "\n".join(lines)
        
        return jsonify({
            "success": True,
            "bot_message": bot_message,
            "intent": "fetch_customer",
            "products": [],
            "suggestions": [],
            "session_id": session_id,
            "metadata": {"confidence": round(confidence, 2), "response_time_ms": elapsed},
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }), 200

    if intent == Intent.UPDATE_CUSTOMER:
        elapsed = int((time.time() - start_time) * 1000)
        update_success = False
        for _api_call, _api_resp in zip(api_calls_to_execute, api_responses):
            if _api_call.method == "PUT" and "/customers/" in _api_call.endpoint:
                update_success = _api_resp.get("success", False)
                break
        _update_signal = [{"success": update_success}]
        bot_message = generate_bot_message(intent, entities, [], confidence, _update_signal)
        suggestions = generate_suggestions(intent, entities, [])
        metadata = {
            "confidence":       round(confidence, 2),
            "products_count":   0,
            "provider":         CLASSIFIER_PROVIDER_TAG,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "response_time_ms": elapsed,
            "intent_raw":       intent.value,
            "entities":         _entities_to_dict(entities),
        }
        logger.info(f"Step 10: Response sent | intent=update_customer | success={update_success} | response_time_ms={elapsed} | flow_state=idle")
        return jsonify({
            "success":     update_success,
            "bot_message": bot_message,
            "intent":      intent.value,
            "products":    [],
            "suggestions": suggestions,
            "session_id":  session_id,
            "metadata":    metadata,
            "pagination":  default_pagination(page),
            "flow_state":  FlowState.IDLE.value,
        }), 200

    resp = handle_reorder(intent, entities, order_data, customer_id, session_id, page, start_time, sessions)
    if resp: return resp

    resp = handle_order_detail(current_flow_state, customer_id, user_context, session_id, page, start_time)
    if resp: return resp
    
    resp = handle_historical_search(
        intent, entities, order_data, customer_id, session_id, page, start_time, sessions
    )
    if resp: return resp

    resp = handle_variant_selection(
        current_flow_state, intent, entities, message, customer_id,
        session_id, page, start_time, sessions, user_context, _resolve_variant,
    )
    if resp: return resp
    
    resp = handle_quantity_and_variant_check(
        intent, entities, all_products_raw, order_data,
        ORDER_CREATE_INTENTS, session_id, page, start_time, sessions, customer_id=customer_id
    )
    if resp: return resp

    resp = handle_quick_order(
        intent, entities, all_products_raw, last_product_ctx,
        customer_id, session_id, page, start_time, sessions, ORDER_CREATE_INTENTS,
    )
    if resp: return resp

    resp = handle_variation_product(
        intent, entities, api_responses, api_calls_to_execute,
        confidence, order_data, session_id, page, start_time, sessions,
    )
    if resp: return resp

    store_loader = get_store_loader()
    all_products_raw, resp = handle_empty_results(
        intent, entities, all_products_raw, message,
        session_id, page, start_time, confidence, sessions, store_loader,
    )
    if resp: return resp

    products = []
    if intent == Intent.CATEGORY_LIST:
        seen_names = set()
        for cat in all_products_raw:
            name = cat.get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                products.append(format_category(cat))
    else:
        for p in all_products_raw:
            if p.get("parent_id"):
                continue
            if isinstance(p.get("attributes"), dict):
                products.append(format_custom_product(p))
            else:
                products.append(format_product(p))
    
    products = [p for p in products if p.get("name")]
    logger.info(f"Step 4: Formatted {len(products)} products")
    
    _pagination_data = build_pagination(page, api_responses, api_calls_to_execute)
    _total_items_for_msg = _pagination_data.get("total_items")
    bot_message = generate_bot_message(
        intent, 
        entities, 
        products, 
        confidence, 
        order_data, 
        total_items=_total_items_for_msg,
        page=page 
    )
    
    if intent in ORDER_CREATE_INTENTS and order_data:
        placed_order = order_data[-1]
        used_product_name = (
            products[0]["name"] if products
            else (placed_order["line_items"][0].get("name") or "your item") if placed_order.get("line_items")
            else "your item"
        )
        total_str = placed_order.get("total", "0.00")
        try:
            total = float(total_str) if total_str else 0.0
        except (ValueError, TypeError):
            total = 0.0

        if total == 0.0 and placed_order.get("line_items"):
            try:
                line_total = sum(float(item.get("total") or 0) for item in placed_order["line_items"])
                if line_total > 0:
                    total = line_total
            except (ValueError, TypeError) as e:
                pass

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
    }

    if session_id and session_id in sessions:
        sessions[session_id]["history"].append({
            "role": "bot",
            "message": bot_message,
            "intent": intent.value,
            "products_count": len(products),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    response = {
        "success": True,
        "bot_message": bot_message,
        "intent": intent.value,
        "products": products,
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": metadata,
        "pagination": _pagination_data,
    }

    if intent in (Intent.ORDER_HISTORY, Intent.LAST_ORDER) and order_data:
        response["orders"] = [format_order_for_frontend(o) for o in order_data]
        response["order_pagination"] = build_pagination(page, api_responses, api_calls_to_execute)

    response["flow_state"] = (
        FlowState.AWAITING_ANYTHING_ELSE.value
        if intent in ORDER_CREATE_INTENTS and order_data
        else FlowState.IDLE.value
    )
    
    logger.info(
        f"Step 10: Response sent | intent={intent.value} | "
        f"products_count={len(products)} | response_time_ms={metadata['response_time_ms']} | "
        f"flow_state={response['flow_state']}"
    )

    return jsonify(response), 200