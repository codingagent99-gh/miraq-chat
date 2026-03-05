"""
handlers/llm_handler.py — Step 1.5: LLM fallback and entity merging.

Handles unknown intent, low confidence, and missing entity cases by calling
the LLM fallback. Returns (intent, entities, confidence, result) on success,
or a Flask response directly for conversational/disambiguation cases.
"""

import dataclasses as _dc

from flask import jsonify

from models import Intent, ExtractedEntities, ClassifiedResult
from conversation_flow import FlowState
from app_config import (
    LLM_FALLBACK_ENABLED,
    CLASSIFIER_PROVIDER_TAG,
)
from llm_fallback import llm_fallback
from conversation_flow import should_disambiguate, get_disambiguation_message
from chat_logger import get_logger
from handlers.chat_utils import default_pagination

logger = get_logger("miraq_chat")

# Maps LLM-returned intent strings to Intent enum values when the string
# doesn't directly match an Intent value.
_INTENT_MAPPING = {
    "search":            Intent.PRODUCT_SEARCH,
    "product_search":    Intent.PRODUCT_SEARCH,
    "browse":            Intent.CATEGORY_BROWSE,
    "category_browse":   Intent.CATEGORY_BROWSE,
    "filter":            Intent.PRODUCT_LIST,
    "filter_by_finish":  Intent.FILTER_BY_FINISH,
    "filter_by_color":   Intent.FILTER_BY_COLOR,
    "filter_by_size":    Intent.FILTER_BY_SIZE,
    "filter_by_application": Intent.FILTER_BY_APPLICATION,
    "filter_by_material": Intent.FILTER_BY_MATERIAL,
    "general_question":  Intent.PRODUCT_LIST,
    "order_inquiry":     Intent.ORDER_HISTORY,
    "order_history":     Intent.ORDER_HISTORY,
    "check_orders":      Intent.ORDER_HISTORY,
    "my_orders":         Intent.ORDER_HISTORY,
    "order_status":      Intent.ORDER_STATUS,
    "order_tracking":    Intent.ORDER_TRACKING,
    "last_order":        Intent.LAST_ORDER,
    "reorder":           Intent.REORDER,
    "order":             Intent.QUICK_ORDER,
    "place_order":       Intent.PLACE_ORDER,
    "quick_order":       Intent.QUICK_ORDER,
    "order_item":        Intent.ORDER_ITEM,
    "discount_inquiry":  Intent.DISCOUNT_INQUIRY,
    "promotions":        Intent.PROMOTIONS,
    "clearance":         Intent.CLEARANCE_PRODUCTS,
    "greeting":          Intent.GREETING,
    "product_attribute_info": Intent.PRODUCT_ATTRIBUTE_INFO,
    # Origin intents — LLM may return these for "products from Italy / made in X" queries
    "product_by_origin": Intent.PRODUCT_BY_ORIGIN,
    "filter_by_origin":  Intent.PRODUCT_BY_ORIGIN,
    "origin":            Intent.PRODUCT_BY_ORIGIN,
    # Attribute / tag filter intents added to match updated system prompt
    "filter_by_attribute": Intent.FILTER_BY_ATTRIBUTE,
    "product_by_tag":    Intent.PRODUCT_BY_TAG,
    "product_by_collection": Intent.PRODUCT_BY_COLLECTION,
    "category_list":     Intent.CATEGORY_LIST,
    "general_question":  Intent.PRODUCT_LIST,
}


def run_llm_fallback(
    message: str,
    intent: Intent,
    entities: ExtractedEntities,
    confidence: float,
    session_id: str,
    session_history,
    store_loader,
    page: int,
    start_time: float,
    order_create_intents: set,
    user_context: dict,
    sessions: dict,
):
    """
    Run LLM fallback if needed.

    Returns one of:
      - (intent, entities, confidence, result)  — classifier result updated, continue pipeline
      - Flask response tuple                     — respond directly (conversational/disambiguation)
      - None                                     — LLM not needed, continue with original values
    """
    import time

    should_try_llm = _should_trigger_llm(intent, confidence, entities, order_create_intents, user_context)
    if not should_try_llm:
        return None

    trigger_reason = _get_trigger_reason(intent, confidence, entities, order_create_intents, user_context)

    if not LLM_FALLBACK_ENABLED:
        disambig = get_disambiguation_message()
        elapsed = time.time() - start_time
        logger.info(f"Step 1.5: Low confidence, returning disambiguation (LLM disabled) | confidence={confidence:.2f}")
        return jsonify({
            "success": True,
            "bot_message": disambig["bot_message"],
            "intent": "disambiguation",
            "products": [],
            "suggestions": disambig["suggestions"],
            "session_id": session_id,
            "metadata": {
                "flow_state": disambig["flow_state"],
                "confidence": round(confidence, 2),
                "original_intent": intent.value,
                "response_time_ms": round(elapsed * 1000),
                "provider": "conversation_flow",
            },
            "flow_state": disambig["flow_state"],
            "pagination": default_pagination(page),
        }), 200

    logger.info(
        f"Step 1.5: LLM fallback triggered | session={session_id} | reason={trigger_reason} | "
        f"original_intent={intent.value} | confidence={confidence:.2f} | message={message!r}"
    )

    llm_result = llm_fallback(
        user_message=message,
        original_intent=intent.value,
        original_confidence=confidence,
        trigger_reason=trigger_reason,
        session_id=session_id,
        store_loader=store_loader,
        session_history=session_history,
    )

    if not llm_result.get("success"):
        disambig = get_disambiguation_message()
        elapsed = time.time() - start_time
        logger.info(f"Step 1.5: LLM failed, returning disambiguation | confidence={confidence:.2f}")
        return jsonify({
            "success": True,
            "bot_message": disambig["bot_message"],
            "intent": "disambiguation",
            "products": [],
            "suggestions": disambig["suggestions"],
            "session_id": session_id,
            "metadata": {
                "flow_state": disambig["flow_state"],
                "confidence": round(confidence, 2),
                "original_intent": intent.value,
                "response_time_ms": round(elapsed * 1000),
                "provider": "conversation_flow",
                "llm_error": llm_result.get("error", "LLM fallback failed"),
            },
            "flow_state": disambig["flow_state"],
            "pagination": default_pagination(page),
        }), 200

    fallback_type = llm_result.get("fallback_type")

    # ── Conversational response — return directly ──
    if fallback_type == "conversational":
        elapsed = time.time() - start_time
        llm_metadata = llm_result.get("metadata", {})
        llm_metadata["response_time_ms"] = round(elapsed * 1000)

        if session_id and session_id in sessions:
            sessions[session_id]["history"].append({
                "role": "bot",
                "message": llm_result["bot_message"],
                "intent": "conversational",
            })

        return jsonify({
            "success": True,
            "bot_message": llm_result["bot_message"],
            "intent": "conversational",
            "products": [],
            "suggestions": [],
            "session_id": session_id,
            "metadata": llm_metadata,
            "pagination": default_pagination(page),
        }), 200

    # ── Intent/entity resolved — merge into new entities ──
    if fallback_type in ["intent_resolved", "entity_extracted"]:
        new_intent, new_entities, new_confidence = _merge_llm_entities(
            llm_result, entities, fallback_type, store_loader, logger
        )

        result = ClassifiedResult(
            intent=new_intent,
            entities=new_entities,
            confidence=new_confidence,
        )

        logger.info(
            f"Step 1.5: LLM fallback applied | new_intent={new_intent.value} | "
            f"new_confidence={new_confidence:.2f} | fallback_type={fallback_type}"
        )
        return new_intent, new_entities, new_confidence, result

    # Unknown fallback_type
    logger.warning(f"Step 1.5: Unknown fallback_type={fallback_type!r} — treating as failure")
    disambig = get_disambiguation_message()
    elapsed = time.time() - start_time
    return jsonify({
        "success": True,
        "bot_message": disambig["bot_message"],
        "intent": "disambiguation",
        "products": [],
        "suggestions": disambig["suggestions"],
        "session_id": session_id,
        "metadata": {
            "flow_state": disambig["flow_state"],
            "confidence": round(confidence, 2),
            "original_intent": intent.value,
            "response_time_ms": round(elapsed * 1000),
            "provider": "conversation_flow",
        },
        "flow_state": disambig["flow_state"],
        "pagination": default_pagination(page),
    }), 200


def _should_trigger_llm(intent, confidence, entities, order_create_intents, user_context) -> bool:
    if intent.value == "unknown":
        return True
    if should_disambiguate(intent.value, confidence):
        return True
    if intent == Intent.PRODUCT_SEARCH and entities.product_name is None and entities.category_id is None:
        return True
    if intent in order_create_intents and entities.order_item_name is None and entities.product_name is None:
        last_product_ctx_check = user_context.get("last_product")
        if not (last_product_ctx_check and last_product_ctx_check.get("id")):
            return True
    return False


def _get_trigger_reason(intent, confidence, entities, order_create_intents, user_context) -> str:
    if intent.value == "unknown":
        return "unknown_intent"
    if should_disambiguate(intent.value, confidence):
        return "low_confidence"
    return "missing_entities"


def _merge_llm_entities(llm_result, original_entities, fallback_type, store_loader, log):
    """Merge LLM-returned entities onto a fresh ExtractedEntities instance."""
    llm_entities_dict = llm_result.get("entities", {})
    new_entities = ExtractedEntities()

    # Reflect actual fields on ExtractedEntities at runtime —
    # no hardcoded list that can go stale with models.py changes.
    _entity_fields = {f.name for f in _dc.fields(ExtractedEntities)}

    for llm_field, llm_value in llm_entities_dict.items():
        if not llm_value:
            continue
        if llm_field == "origin":
            # Origin needs tag resolution — store as attribute AND resolve to
            # made-in-X tag slug so api_builder can filter correctly.
            new_entities.attributes["origin"] = llm_value
            if store_loader:
                _tag_ids = store_loader.get_tag_ids_for_keyword(llm_value)
                if not _tag_ids:
                    _tag_ids = store_loader.get_tag_ids_for_keyword(f"made in {llm_value.lower()}")
                if _tag_ids:
                    new_entities.tag_ids.extend(_tag_ids)
                    for _tid in _tag_ids:
                        _tag = store_loader.tag_by_id.get(_tid)
                        if _tag:
                            new_entities.tag_slugs.append(_tag["slug"])
                    log.info(f"Step 1.5: Origin resolved | origin={llm_value!r} | tag_slugs={new_entities.tag_slugs}")
                else:
                    log.warning(f"Step 1.5: Origin not resolved to tag | origin={llm_value!r}")
        elif llm_field in _entity_fields:
            setattr(new_entities, llm_field, llm_value)
        else:
            # Dynamic attribute (finish, visual, color, size, etc.)
            new_entities.attributes[llm_field] = llm_value

    # For entity_extracted, preserve original entities not overridden by LLM
    if fallback_type == "entity_extracted":
        for _f in _entity_fields:
            if getattr(new_entities, _f, None) is None:
                orig_val = getattr(original_entities, _f, None)
                if orig_val is not None:
                    setattr(new_entities, _f, orig_val)
        for _k, _v in original_entities.attributes.items():
            if _k not in new_entities.attributes:
                new_entities.attributes[_k] = _v
        existing_tag_ids = set(new_entities.tag_ids)
        for _tid in original_entities.tag_ids:
            if _tid not in existing_tag_ids:
                new_entities.tag_ids.append(_tid)
        existing_tag_slugs = set(new_entities.tag_slugs)
        for _slug in original_entities.tag_slugs:
            if _slug not in existing_tag_slugs:
                new_entities.tag_slugs.append(_slug)

    # Resolve intent string
    llm_intent_str = llm_result.get("intent", "unknown")
    try:
        new_intent = Intent(llm_intent_str)
    except ValueError:
        new_intent = _INTENT_MAPPING.get(llm_intent_str, Intent.PRODUCT_LIST)
        if llm_intent_str not in _INTENT_MAPPING:
            log.warning(
                f"Step 1.5: Unmapped LLM intent '{llm_intent_str}' — "
                f"falling back to PRODUCT_LIST. Consider adding it to _INTENT_MAPPING."
            )

    new_confidence = llm_result.get("confidence", 0.70)
    return new_intent, new_entities, new_confidence