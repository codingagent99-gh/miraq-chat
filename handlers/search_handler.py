"""
handlers/search_handler.py — Steps 3.1 and 3.8: Search debug logging and empty result retry.

Step 3.1 — Debug: log matched product context (tags/cats/attrs) or zero-result filters.
Step 3.8 — LLM retry on empty search results, or local no-results message.

Returns a Flask response if no products found after retry, else None to fall through.
"""

import time

from flask import jsonify

from models import Intent
from classifier import classify
from api_builder import build_api_calls
from woo_client import woo_client
from app_config import (
    CLASSIFIER_PROVIDER_TAG,
    LLM_FALLBACK_ENABLED,
    LLM_RETRY_ON_EMPTY_RESULTS,
)
from llm_fallback import llm_retry_search
from conversation_flow import FlowState
from chat_logger import get_logger
from formatters import _entities_to_dict
from handlers.chat_utils import default_pagination
from response_generator import INTENT_LABELS

logger = get_logger("miraq_chat")

_SEARCH_FILTER_INTENTS = {
    Intent.PRODUCT_SEARCH,
    Intent.PRODUCT_LIST,
    Intent.CATEGORY_BROWSE,
    Intent.FILTER_BY_FINISH,
    Intent.FILTER_BY_SIZE,
    Intent.FILTER_BY_COLOR,
    Intent.FILTER_BY_APPLICATION,
    Intent.PRODUCT_BY_VISUAL,
    Intent.PRODUCT_BY_ORIGIN,
}


def log_matched_products(all_products_raw: list, api_calls_to_execute: list):
    """
    Step 3.1: Log per-product match context on success, or exact filters on zero results.
    Visibility: matched products log at DEBUG, zero results log at WARNING.
    """
    if all_products_raw:
        for _p in all_products_raw[:8]:
            _p_cats = [c["slug"] for c in _p.get("categories", []) if isinstance(c, dict)]
            _p_tags = [t["slug"] for t in _p.get("tags", []) if isinstance(t, dict)]
            _p_attrs = {
                a["slug"]: a.get("options", [])
                for a in _p.get("attributes", [])
                if isinstance(a, dict) and a.get("visible") and a.get("options")
            }
            logger.debug(
                f"Step 3.1: Matched | id={_p.get('id')} name={_p.get('name')!r} | "
                f"cats={_p_cats} | tags={_p_tags} | attrs={_p_attrs}"
            )
    else:
        for _call in api_calls_to_execute:
            _filters = _call.params.get("filters", "n/a")
            _search = _call.params.get("search", "")
            logger.warning(
                f"Step 3.1: Zero results | endpoint={_call.endpoint.split('/')[-1]} | "
                f"filters={_filters}" + (f" | search={_search!r}" if _search else "")
            )


def handle_empty_results(
    intent,
    entities,
    all_products_raw,
    message,
    session_id,
    page,
    start_time,
    confidence,
    sessions,
    store_loader,
):
    """
    Step 3.8: Handle empty search results — either local no-results message or LLM retry.

    Returns (updated_all_products_raw, flask_response_or_none).
    Flask response is non-None only when results are still empty after retry
    and we have a suggestion message to show.
    """
    if not (intent in _SEARCH_FILTER_INTENTS and len(all_products_raw) == 0):
        return all_products_raw, None

    # ── Pre-check: unrecognized search_term — return local message immediately ──
    if entities.search_term:
        _term = entities.search_term
        _cat = entities.category_name or "products"
        _no_results_msg = (
            f"I couldn't find any **{_term} {_cat.lower()}** in our catalog. "
            f"We may not carry that specific type. "
            f"Would you like to browse all **{_cat}** instead, or try a different search?"
        )
        logger.info(
            f"Step 3.8: Unrecognized search_term='{_term}' returned 0 results — "
            f"returning local no-results response (skipping LLM retry)"
        )
        elapsed = time.time() - start_time
        if session_id and session_id in sessions:
            sessions[session_id]["history"].append({
                "role": "bot",
                "message": _no_results_msg,
                "intent": intent.value,
            })
        return all_products_raw, (jsonify({
            "success": True,
            "bot_message": _no_results_msg,
            "intent": INTENT_LABELS.get(intent, "unknown"),
            "products": [],
            "suggestions": [
                f"Show all {_cat}",
                "What categories do you have?",
                "Show me what's on sale",
            ],
            "session_id": session_id,
            "metadata": {
                "confidence": round(confidence, 2),
                "products_count": 0,
                "provider": CLASSIFIER_PROVIDER_TAG,
                "response_time_ms": round(elapsed * 1000),
                "intent_raw": intent.value,
                "entities": _entities_to_dict(entities),
                "no_results_reason": "unrecognized_search_term",
            },
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }), 200)

    # ── LLM retry ──
    if not (LLM_RETRY_ON_EMPTY_RESULTS and LLM_FALLBACK_ENABLED):
        return all_products_raw, None

    logger.info(f"Step 3.8: Empty search results, trying LLM retry | intent={intent.value}")

    entities_dict = {
        "product_name": entities.product_name,
        "category_name": entities.category_name,
        **entities.attributes,
    }

    llm_retry_result = llm_retry_search(
        user_message=message,
        original_intent=intent.value,
        entities=entities_dict,
        session_id=session_id,
        store_loader=store_loader,
    )

    if not llm_retry_result.get("success"):
        return all_products_raw, None

    retry_type = llm_retry_result.get("retry_type")

    if retry_type == "corrected_search" and llm_retry_result.get("corrected_term"):
        corrected_term = llm_retry_result["corrected_term"]
        logger.info(f"Step 3.8: LLM suggested correction | corrected_term={corrected_term}")

        corrected_result = classify(corrected_term)
        corrected_api_calls = build_api_calls(corrected_result)
        corrected_responses = woo_client.execute_all(corrected_api_calls)

        corrected_products_raw = []
        for resp in corrected_responses:
            if resp.get("success"):
                data = resp.get("data")
                if isinstance(data, dict) and "products" in data:
                    corrected_products_raw.extend(data["products"])
                elif isinstance(data, list):
                    corrected_products_raw.extend(data)
                elif isinstance(data, dict):
                    corrected_products_raw.append(data)

        if corrected_products_raw:
            all_products_raw = corrected_products_raw
            logger.info(f"Step 3.8: LLM retry successful | found {len(all_products_raw)} products")
        else:
            logger.info("Step 3.8: LLM retry still returned 0 products")

    if len(all_products_raw) == 0 and llm_retry_result.get("suggestion_message"):
        suggestion_msg = llm_retry_result["suggestion_message"]
        elapsed = time.time() - start_time
        llm_metadata = llm_retry_result.get("metadata", {})
        llm_metadata["response_time_ms"] = round(elapsed * 1000)
        llm_metadata["original_intent"] = intent.value
        llm_metadata["confidence"] = round(confidence, 2)

        if session_id and session_id in sessions:
            sessions[session_id]["history"].append({
                "role": "bot",
                "message": suggestion_msg,
                "intent": intent.value,
            })

        return all_products_raw, (jsonify({
            "success": True,
            "bot_message": suggestion_msg,
            "intent": INTENT_LABELS.get(intent, "unknown"),
            "products": [],
            "suggestions": [],
            "session_id": session_id,
            "metadata": llm_metadata,
            "pagination": default_pagination(page),
        }), 200)

    return all_products_raw, None