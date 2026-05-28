"""
handlers/llm_handler.py — Step 1.5: LLM fallback and entity merging.

Handles unknown intent, low confidence, and missing entity cases by calling
the LLM fallback. Returns (intent, entities, confidence, result) on success,
or a Flask response directly for conversational/disambiguation cases.
"""

from flask import jsonify

from models import Intent, ExtractedEntities, ClassifiedResult
from conversation_flow import FlowState
from app_config import (
    LLM_FALLBACK_ENABLED,
    CLASSIFIER_PROVIDER_TAG,
)
from llm_fallback import llm_fallback
from conversation_flow import should_disambiguate, get_disambiguation_message, LOW_CONFIDENCE_THRESHOLD
from chat_logger import get_logger
from handlers.chat_utils import default_pagination

logger = get_logger("miraq_chat")

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

    # Build a compact entities summary from the classifier's ExtractedEntities
    _entity_keys = [
        "product_name", "category_name", "attributes", "tag_slugs",
        "order_id", "order_item_name", "search_term",
    ]
    entities_summary = {
        k: getattr(entities, k) for k in _entity_keys
        if getattr(entities, k)
    }

    llm_result = llm_fallback(
        user_message=message,
        original_intent=intent.value,
        original_confidence=confidence,
        trigger_reason=trigger_reason,
        session_id=session_id,
        store_loader=store_loader,
        session_history=session_history,
        entities_summary=entities_summary,
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
        merge_result = _merge_llm_entities(
            llm_result, entities, fallback_type, store_loader, logger
        )

        if merge_result is None:
            # LLM returned a hallucinated intent string — treat as failure
            disambig = get_disambiguation_message()
            elapsed = time.time() - start_time
            logger.warning(
                f"Step 1.5: LLM hallucinated invalid intent — returning disambiguation | "
                f"original_intent={intent.value} | fallback_type={fallback_type}"
            )
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
                    "llm_error": "LLM returned an unrecognised intent value",
                },
                "flow_state": disambig["flow_state"],
                "pagination": default_pagination(page),
            }), 200

        new_intent, new_entities, new_confidence = merge_result

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
    if intent == Intent.PRODUCT_SEARCH and entities.product_name is None and not getattr(entities, 'target_category_slugs', None) and not entities.attr_tag_or_pairs:
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
    """
    Resolve intent and confidence from the LLM result.

    The LLM performs intent-only classification — it does not return entities.
    Original entities from the local classifier are preserved unchanged.

    Returns:
        (Intent, ExtractedEntities, float) on success
        None if the LLM returned an unrecognised intent string (caller should
        treat this as a failure and return disambiguation to the user)
    """
    new_entities = original_entities

    # Safely extract and normalize the intent string from the LLM
    llm_intent_str = llm_result.get("intent", "unknown").lower().strip()

    try:
        # Single Source of Truth: cast string directly to the Intent enum
        new_intent = Intent(llm_intent_str)
    except ValueError:
        # LLM hallucinated a non-existent intent — signal failure to caller
        # so the user gets a clean disambiguation instead of a silent misroute
        log.warning(
            f"Step 1.5: LLM returned unrecognised intent '{llm_intent_str}' — "
            f"returning None to trigger disambiguation."
        )
        return None

    raw_confidence = llm_result.get("confidence", 0.70)

    # Clamp: a resolved intent whose confidence sits below the disambiguation
    # threshold would retrigger the LLM on the very next turn, creating a loop.
    # We accept any value the LLM reported, but floor it just above the threshold
    # so the pipeline treats this as a confident resolution.
    _MIN_RESOLVED_CONFIDENCE = LOW_CONFIDENCE_THRESHOLD + 0.05  # 0.65
    new_confidence = max(raw_confidence, _MIN_RESOLVED_CONFIDENCE)

    if new_confidence != raw_confidence:
        log.debug(
            f"Step 1.5: LLM confidence clamped | "
            f"raw={raw_confidence:.2f} -> clamped={new_confidence:.2f}"
        )

    return new_intent, new_entities, new_confidence