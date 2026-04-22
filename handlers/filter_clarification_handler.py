"""
handlers/filter_clarification_handler.py — Resolves the AWAITING_FILTER_CLARIFICATION
flow state when the user accepts, rejects, skips, or cancels a semantic match.
"""

from models import ExtractedEntities, ClassifiedResult, Intent
from conversation_flow import FlowState
from utils.entity_helpers import restore_carryover, merge_attribute


def resolve_filter_clarification(message, user_context, pending_semantic):
    """
    Resolve user response to a semantic filter clarification prompt.

    Returns (ClassifiedResult, new_flow_state) if handled,
    or None if the message doesn't match any expected response.
    """
    msg_lower = message.lower().strip()

    # ── Detect user intent ──
    is_accept = False
    selected_match = None

    if "options" in pending_semantic:
        for opt in pending_semantic["options"]:
            if opt["suggested_name"].lower() in msg_lower:
                is_accept = True
                selected_match = opt
                break

    if not is_accept and (
        msg_lower == "yes - use these filters"
        or msg_lower.startswith("yes - use ")
        or msg_lower.startswith("yes - exclude ")
        or msg_lower.startswith("use ")
        or msg_lower.startswith("exclude ")
        or msg_lower in ["yes", "y", "yep", "sure", "ok"]
    ):
        is_accept = True
        if "options" in pending_semantic:
            selected_match = pending_semantic["options"][0]

    is_reject = msg_lower.startswith("no - ") or msg_lower in ["no", "n", "nope"]
    is_cancel = msg_lower in [
        "cancel", "exit", "stop", "nevermind", "never mind", "abort", "start over"
    ]
    is_skip = msg_lower in ["skip - use my current filters", "skip"]

    if is_cancel:
        user_context.pop("pending_semantic_match", None)

    if not (is_accept or is_reject or is_skip):
        return None

    # ── Build entities ──
    entities = ExtractedEntities()
    entities.target_category_slugs = set()

    if is_accept:
        options_to_apply = []
        if selected_match:
            options_to_apply.append(selected_match)
        elif "options" in pending_semantic:
            options_to_apply.extend(pending_semantic["options"])

        options_to_apply.extend(pending_semantic.get("extra_semantics", []))

        for opt in options_to_apply:
            is_neg = opt.get("is_negative", False)
            if opt["type"] == "tag":
                if is_neg:
                    if not hasattr(entities, 'excluded_tags'):
                        entities.excluded_tags = []
                    entities.excluded_tags.append(opt["slug"])
                else:
                    entities.tag_slugs.append(opt["slug"])
            elif opt["type"] == "category":
                if is_neg:
                    if not hasattr(entities, 'excluded_categories'):
                        entities.excluded_categories = []
                    entities.excluded_categories.append(opt["slug"])
                else:
                    entities.target_category_slugs.add(opt["slug"])
                    entities.category_name = opt["suggested_name"]
            elif opt["type"] == "attribute":
                taxonomy = opt["taxonomy"]
                if is_neg:
                    if not hasattr(entities, 'excluded_attributes'):
                        entities.excluded_attributes = {}
                    if taxonomy not in entities.excluded_attributes:
                        entities.excluded_attributes[taxonomy] = []
                    entities.excluded_attributes[taxonomy].append(opt["slug"])
                else:
                    merge_attribute(entities.attributes, taxonomy, opt["slug"])

    elif is_reject:
        if "rejected_semantic_terms" not in user_context:
            user_context["rejected_semantic_terms"] = []
        for opt in pending_semantic.get("options", []):
            user_context["rejected_semantic_terms"].append(opt["suggested_name"])

    # Restore leftover semantics
    leftovers = pending_semantic.get("pending_other_semantics", [])
    if leftovers:
        entities.semantic_matches.extend(leftovers)

    # Restore carryover
    restore_carryover(entities, pending_semantic)

    # Override search_term based on action
    if is_reject:
        entities.search_term = pending_semantic.get("options", [{}])[0].get("user_text", "")
    elif is_skip:
        entities.search_term = None
    else:
        # is_accept: only restore if nothing was matched
        if pending_semantic.get("carryover_search_term"):
            entities.search_term = pending_semantic["carryover_search_term"]

    user_context.pop("pending_semantic_match", None)

    bypass_intent = Intent.PRODUCT_SEARCH if getattr(entities, 'product_id', None) else Intent.FILTER_BY_ATTRIBUTE
    return ClassifiedResult(intent=bypass_intent, entities=entities, confidence=0.98)