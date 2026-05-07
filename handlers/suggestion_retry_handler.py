"""
handlers/suggestion_retry_handler.py — Handles the suggestion_retry payload
when the frontend fires a retry with pre-built filter slugs.
"""

import time
from flask import jsonify

from models import ExtractedEntities, ClassifiedResult, Intent, WooAPICall
from app_config import CLASSIFIER_PROVIDER_TAG
from woo_client import woo_client
from formatters import format_product, format_custom_product
from response_generator import _resolve_user_placeholders
from api_builder import build_api_calls
from conversation_flow import FlowState
from store_registry import get_store_loader as _get_loader
from handlers.chat_utils import default_pagination, build_pagination


def handle_suggestion_retry(body, message, session_id, customer_id, page, start_time):
    """
    Process a suggestion_retry payload. Returns a Flask response tuple or None.
    """
    suggestion_retry = body.get("suggestion_retry")
    if not suggestion_retry:
        return None

    sr_label = suggestion_retry.get("label", "suggestion retry")
    loader = _get_loader()
    sr_entities = ExtractedEntities()

    # Category
    cat_slug = suggestion_retry.get("category_slug", "")
    if cat_slug and loader:
        cat_obj = loader.resolve_category(cat_slug)
        if cat_obj:
            sr_entities.category_name = cat_obj.name
            sr_entities.category_id = cat_obj.backend_ref.get("id")

    for extra_slug in (suggestion_retry.get("extra_category_slugs") or []):
        if loader:
            extra_obj = loader.resolve_category(extra_slug)
            if extra_obj:
                sr_entities.extra_category_ids.append(extra_obj.backend_ref.get("id"))

    # Tags
    for tslug in (suggestion_retry.get("tag_slugs") or []):
        sr_entities.tag_slugs.append(tslug)
        if loader:
            tid = loader.get_tag_id_by_slug(tslug)
            if tid:
                sr_entities.tag_ids.append(tid)

    # Attributes
    sr_entities.attributes = dict(suggestion_retry.get("attributes") or {})

    sr_result = ClassifiedResult(
        intent=Intent.FILTER_BY_ATTRIBUTE,
        entities=sr_entities,
        confidence=1.0,
    )

    api_calls = build_api_calls(
        sr_result, page, user_message=message, session_id=session_id, customer_id=customer_id
    )
    if customer_id:
        _resolve_user_placeholders(api_calls, customer_id)

    responses = woo_client.execute_all(api_calls)

    products_raw = []
    for r in responses:
        if r.get("success"):
            d = r.get("data")
            if isinstance(d, dict) and "products" in d:
                products_raw.extend(d["products"])
            elif isinstance(d, list):
                products_raw.extend(d)
            elif isinstance(d, dict):
                products_raw.append(d)

    formatted = []
    for p in products_raw:
        if p.get("parent_id"):
            continue
        if "featured_image" in p:
            formatted.append(format_custom_product(p))
        else:
            formatted.append(format_product(p))
    formatted = [p for p in formatted if p.get("name")]

    bot_message = (
        f"Here are results for **{sr_label}**:"
        if formatted
        else f"No products found for **{sr_label}** either. Try a different filter."
    )
    pagination = build_pagination(page, responses, api_calls)
    elapsed = time.time() - start_time

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "filter",
        "products": formatted,
        "suggestions": [],
        "filter_suggestions": [],
        "session_id": session_id,
        "metadata": {
            "confidence": 1.0,
            "products_count": len(formatted),
            "provider": CLASSIFIER_PROVIDER_TAG,
            "response_time_ms": round(elapsed * 1000),
            "intent_raw": "suggestion_retry",
        },
        "pagination": pagination,
        "flow_state": FlowState.IDLE.value,
    })