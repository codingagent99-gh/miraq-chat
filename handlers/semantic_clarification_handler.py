"""
handlers/semantic_clarification_handler.py — Builds the clarification prompt
when the parser finds ambiguous semantic matches that need user confirmation.
"""

import time
from flask import jsonify

from conversation_flow import FlowState
from handlers.chat_utils import default_pagination


def build_semantic_clarification(
    entities,
    user_context: dict,
    session_id: str,
    page: int,
    start_time: float,
    flow_result,
):
    """
    Check if entities have semantic matches requiring user clarification.
    Returns a Flask response tuple if clarification is needed, else None.
    """
    if not entities.semantic_matches:
        return None

    rejected_list = user_context.get("rejected_semantic_terms", [])
    valid_term_groups = []

    for candidate_list in entities.semantic_matches:
        if isinstance(candidate_list, dict):
            candidate_list = [candidate_list]
        clean_list = [c for c in candidate_list if c["suggested_name"] not in rejected_list]
        if clean_list:
            valid_term_groups.append(clean_list)

    reject_flow = flow_result and flow_result.get("reject_semantic_match")

    if not valid_term_groups or reject_flow:
        return None

    has_ties = any(len(group) > 1 for group in valid_term_groups)

    stashed_semantic_data = {
        "carryover_search_term": entities.search_term,
        "carryover_tags": list(entities.tag_slugs),
        "carryover_attributes": entities.attributes,
        "carryover_excluded_tags": getattr(entities, 'excluded_tags', []),
        "carryover_excluded_categories": getattr(entities, 'excluded_categories', []),
        "carryover_excluded_attributes": getattr(entities, 'excluded_attributes', {}),
        "carryover_product_id": entities.product_id,
        "carryover_product_name": entities.product_name,
        "carryover_quantity": getattr(entities, 'quantity', None),
        "carryover_order_id": getattr(entities, 'order_id', None),
        "carryover_collection_year": getattr(entities, 'collection_year', None),
        "carryover_in_stock": getattr(entities, 'in_stock', None),
        "carryover_on_sale": getattr(entities, 'on_sale', None),
        "carryover_min_price": getattr(entities, 'min_price', None),
        "carryover_max_price": getattr(entities, 'max_price', None),
        "carryover_attr_tag_or_pairs": list(getattr(entities, 'attr_tag_or_pairs', [])),
        "carryover_categories": list(getattr(entities, 'target_category_slugs', set())),
        "carryover_category_name": getattr(entities, 'category_name', None),
    }

    has_strong_filters = bool(
        entities.target_category_slugs
        or entities.tag_slugs
        or entities.attributes
        or getattr(entities, 'attr_tag_or_pairs', [])
    )

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
        if has_strong_filters:
            suggestion_buttons.append("Skip - use my current filters")
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
                bot_message = f"I don't have an exact match for '{primary_semantics[0]['user_text']}', but I do have **{suggested_names[0]}**."
                suggestion_buttons.append(f"Yes - use {suggested_names[0]}")
            suggestion_buttons.append(f"No - search for '{primary_semantics[0]['user_text']}'")
            if has_strong_filters:
                suggestion_buttons.append("Skip - use my current filters")
        else:
            joined_names = " and ".join(suggested_names)
            bot_message = f"I don't have exact matches, but I found **{joined_names}**."
            suggestion_buttons.append("Yes - use these filters")
            suggestion_buttons.append("No - use my original text")
            if has_strong_filters:
                suggestion_buttons.append("Skip - use my current filters")

    suggestion_buttons.append("New Search")
    suggestion_buttons.append("Cancel")

    elapsed = time.time() - start_time
    user_context["pending_semantic_match"] = stashed_semantic_data

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
    })