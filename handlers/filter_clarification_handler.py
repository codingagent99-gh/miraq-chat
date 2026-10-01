"""
handlers/filter_clarification_handler.py — Resolves the AWAITING_FILTER_CLARIFICATION
flow state when the user accepts, rejects, skips, or cancels a semantic match.
"""

from models import ExtractedEntities, ClassifiedResult, Intent
from conversation_flow import FlowState
from utils.entity_helpers import restore_carryover, merge_attribute
from platform_config import current_backend


def apply_semantic_match(entities, opt):
    """
    Apply a single semantic match option to entities in-place.

    Shared by both the manual-accept path (resolve_filter_clarification) and
    the auto-apply path (chat.py Step 5), so both paths produce identical
    entity mutations for the same match.
    """
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


def _group_term(group) -> str:
    """The word the shopper typed that produced this group of candidates."""
    first = group[0] if isinstance(group, list) and group else group
    return str((first or {}).get("user_text") or "").strip().lower() if isinstance(first, dict) else ""


def _answered_terms(pending_semantic) -> set:
    """Words the prompt just answered covers (the options on screen plus any
    extra single matches applied with them)."""
    terms = set()
    for opt in list(pending_semantic.get("options") or []) + list(pending_semantic.get("extra_semantics") or []):
        term = str(opt.get("user_text") or "").strip().lower()
        if term:
            terms.add(term)
    return terms


def _stem(text) -> str:
    """'Mosaics', 'mosaic', 'MOSAIC-S' → 'mosaic'. Whole-phrase compare only,
    so 'Wall Tiles' never matches 'tile'."""
    import re
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words)


def _drop_filters_named_by(entities, word) -> None:
    """Remove categories, tags, attribute values and OR-pairs whose name is
    exactly `word` (plural-insensitive) — the ones the parser derived from it."""
    target = _stem(word)
    if not target:
        return

    from store_registry import get_store_loader
    loader = get_store_loader()

    def _named(slug, name=None) -> bool:
        return _stem(slug) == target or (name is not None and _stem(name) == target)

    def _cat_name(slug):
        obj = loader.resolve_category(slug) if loader and hasattr(loader, "resolve_category") else None
        return getattr(obj, "name", None)

    # Categories
    dropped = {s for s in entities.target_category_slugs if _named(s, _cat_name(s))}
    if dropped:
        entities.target_category_slugs -= dropped
        entities.category_groups = [g - dropped for g in entities.category_groups if g - dropped]
        if not entities.target_category_slugs:
            entities.category_name = None

    # Tags
    entities.tag_slugs = [t for t in entities.tag_slugs if not _named(t)]

    # Attribute values (comma-joined per taxonomy)
    for key in list(entities.attributes):
        kept = [v for v in str(entities.attributes[key]).split(",") if v.strip() and not _named(v)]
        if kept:
            entities.attributes[key] = ",".join(kept)
        else:
            del entities.attributes[key]

    # OR-pairs built from the word (tag/attribute/category collisions)
    entities.attr_tag_or_pairs = [
        op for op in (entities.attr_tag_or_pairs or [])
        if not (
            _named(op.get("display_text") or "")
            or _named(op.get("attr_term") or "")
            or _named(op.get("tag_slug") or "")
            or any(_named(c, _cat_name(c)) for c in (op.get("cat_slugs") or []))
        )
    ]


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
            if opt["suggested_name"].lower() == msg_lower:
                is_accept = True
                selected_match = opt
                break
        if not is_accept:
            candidates = [
                opt for opt in pending_semantic["options"]
                if opt["suggested_name"].lower() in msg_lower
            ]
            if candidates:
                selected_match = max(candidates, key=lambda o: len(o["suggested_name"]))
                is_accept = True

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

    is_reject = bool(pending_semantic.get("reject_label")) and msg_lower == pending_semantic["reject_label"].lower()
    is_cancel = msg_lower in [
        "cancel", "exit", "stop", "nevermind", "never mind", "abort", "start over"
    ]
    is_skip = bool(pending_semantic.get("skip_label")) and msg_lower == pending_semantic["skip_label"].lower()
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
            apply_semantic_match(entities, opt)

    elif is_reject:
        if "rejected_semantic_terms" not in user_context:
            user_context["rejected_semantic_terms"] = []
        for opt in pending_semantic.get("options", []):
            user_context["rejected_semantic_terms"].append(opt["suggested_name"])

    # Restore leftover semantics — but only for OTHER words in the message.
    # One word can match terms in several taxonomies (e.g. "mosaic" against
    # colour terms under Colors, Color and Colous); each taxonomy is its own
    # group, asked one at a time. The shopper has just answered for this
    # word, so the remaining groups for the same word are the same question
    # again: re-asking them made every chip (search, skip, pick) come back
    # with an identical-looking prompt, once per taxonomy.
    #   skip   → "search with my filters only": nothing left to ask at all.
    #   reject → same word dropped, and recorded as rejected like the first
    #            group, so it isn't re-raised later in the session either.
    #   accept → same word already resolved by the pick.
    # Shopify only — WooCommerce stores keep the original behaviour.
    _shopify = current_backend() == "shopify"
    leftovers = pending_semantic.get("pending_other_semantics", [])
    if _shopify and is_skip:
        leftovers = []
    elif _shopify and leftovers:
        answered = _answered_terms(pending_semantic)
        same_word = [g for g in leftovers if _group_term(g) in answered]
        leftovers = [g for g in leftovers if _group_term(g) not in answered]
        if is_reject:
            for group in same_word:
                for opt in (group if isinstance(group, list) else [group]):
                    user_context["rejected_semantic_terms"].append(opt["suggested_name"])
    if leftovers:
        entities.semantic_matches.extend(leftovers)

    # Restore carryover
    restore_carryover(entities, pending_semantic)

    # Override search_term based on action
    if is_reject:
        entities.search_term = pending_semantic.get("options", [{}])[0].get("user_text", "")
        if _shopify:
            # "Search '<word>'" = search the store for that text. Any filter
            # the parser resolved from the SAME word (typing "mosaic" also
            # selects the Mosaics category) must go, or it wins: with a
            # category/tag/attribute set, the product-search builder drops
            # search_term and the chip silently becomes a category browse.
            # Filters from other words stay.
            _drop_filters_named_by(entities, entities.search_term)
            # The shopper chose this text, so it must survive alongside any
            # filters from their other words (see ExtractedEntities).
            entities.search_term_explicit = bool(entities.search_term)
    elif is_skip:
        entities.search_term = None
    else:
        # is_accept: only restore if nothing was matched
        if pending_semantic.get("carryover_search_term"):
            entities.search_term = pending_semantic["carryover_search_term"]

    user_context.pop("pending_semantic_match", None)

    if is_reject:
        bypass_intent = Intent.PRODUCT_SEARCH
    else:
        bypass_intent = Intent.PRODUCT_SEARCH if getattr(entities, 'product_id', None) else Intent.FILTER_BY_ATTRIBUTE
    return ClassifiedResult(intent=bypass_intent, entities=entities, confidence=0.98)