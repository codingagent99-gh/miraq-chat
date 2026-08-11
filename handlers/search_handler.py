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
from handlers.suggestion_builder import build_suggestions
from handlers.search_refinement import describe_active_filters_labeled

logger = get_logger("miraq_chat")

_SEARCH_FILTER_INTENTS = {
    Intent.PRODUCT_SEARCH,
    Intent.PRODUCT_LIST,
    Intent.CATEGORY_BROWSE,
    Intent.FILTER_BY_ATTRIBUTE,
    Intent.PRODUCT_BY_TAG,
    Intent.PRODUCT_QUICK_SHIP,
    Intent.MOST_POPULAR,
}


def log_matched_products(all_products_raw: list, api_calls_to_execute: list, intent=None):
    """
    Step 3.1: Log per-product match context on success, or exact filters on zero results.
    Visibility: matched products log at DEBUG, zero results log at WARNING.
    """
    from models import Intent
    _ORDER_INTENTS = {Intent.REORDER, Intent.LAST_ORDER, Intent.ORDER_HISTORY,
                      Intent.ORDER_TRACKING, Intent.ORDER_STATUS, Intent.HISTORICAL_SEARCH}
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
    elif intent not in _ORDER_INTENTS:
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
    store_loader,
):
    # ── Guard: load-more on a known product is never a "not found" ──
    # page > 1 with a resolved product_id means the frontend is paginating
    # variations of a product we already found. An empty page 2+ is "end of
    # results", NOT a missing product — skip the LLM retry entirely.
    if page > 1 and getattr(entities, "product_id", None):
        logger.info(
            f"Step 3.8: Skipping LLM retry — load-more for known "
            f"product_id={entities.product_id} page={page} (not a missing product)"
        )
        return all_products_raw, None

    if not (intent in _SEARCH_FILTER_INTENTS and len(all_products_raw) == 0):
        return all_products_raw, None

    # ── Pre-check: unrecognized search_term — return local message immediately ──
    _has_other_signals = bool(
        entities.product_name
        or entities.category_name
        or getattr(entities, "target_category_slugs", None)
        or entities.attributes
        or entities.tag_slugs
        or getattr(entities, "attr_tag_or_pairs", None)
    )
    if entities.search_term and _has_other_signals:
        # search_term alongside other real signals means the classifier
        # genuinely narrowed things down and is just missing this one term —
        # a confident "we don't carry that" is appropriate. If search_term
        # is the ONLY thing populated, nothing was actually understood (the
        # classifier's catch-all fallback dumped the raw text in) — that
        # case falls through to the LLM retry below instead, regardless of
        # which specific words made the message unrecognizable.
        _term = entities.search_term
        _cat = entities.category_name or "products"
        _no_results_msg = (
            f"I couldn't find any **{_term} {_cat.lower()}** in our catalog. "
            f"We may not carry that specific type. "
        )
        logger.info(
            f"Step 3.8: Unrecognized search_term='{_term}' returned 0 results — "
            f"returning local no-results response (skipping LLM retry)"
        )
        elapsed = time.time() - start_time
        _filter_suggestions = build_suggestions(entities, store_loader)
        return all_products_raw, (jsonify({
            "success": True,
            "bot_message": _no_results_msg,
            "intent": intent.value,
            "products": [],
            "suggestions": [
                f"Show all {_cat}",
                "New Search",
            ],
            "filter_suggestions": _filter_suggestions,
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

    if len(all_products_raw) == 0:
        suggestion_msg = llm_retry_result.get("suggestion_message")
        _cat = entities.category_name or "products"

        if not suggestion_msg:
            # LLM retry yielded nothing useful — build a fallback that NAMES the
            # combination. "those filters" told the shopper nothing: with several
            # dimensions in play (category + colour + tag + size) they cannot tell
            # which one is too narrow, or even what MiraQ thought they asked for.
            _labeled = describe_active_filters_labeled(entities)
            _dimensions = _labeled.count("**") // 2 if _labeled else 0

            if _labeled and _dimensions > 1:
                suggestion_msg = (
                    f"No products match all of these together:\n\n{_labeled}\n\n"
                    f"Each filter on its own may have results — it's the combination "
                    f"that's empty. Try removing one, or tap a filter below to drop it."
                )
            elif _labeled:
                suggestion_msg = (
                    f"No products match {_labeled} in our catalog.\n\n"
                    f"Try a different value, or browse all **{_cat}** instead."
                )
            else:
                suggestion_msg = (
                    f"I couldn't find any **{_cat}** matching your search. "
                )
            logger.info("Step 3.8: LLM retry produced no suggestion — using fallback no-results message")

        elapsed = time.time() - start_time
        llm_metadata = llm_retry_result.get("metadata", {})
        llm_metadata["response_time_ms"] = round(elapsed * 1000)
        llm_metadata["original_intent"] = intent.value
        llm_metadata["confidence"] = round(confidence, 2)

        _filter_suggestions = build_suggestions(entities, store_loader)
        return all_products_raw, (jsonify({
            "success": True,
            "bot_message": suggestion_msg,
            "intent": intent.value,
            "products": [],
            "suggestions": [
                f"Show all {_cat}",
                "New Search",
            ],
            "filter_suggestions": _filter_suggestions,
            "session_id": session_id,
            "metadata": llm_metadata,
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }), 200)

    return all_products_raw, None