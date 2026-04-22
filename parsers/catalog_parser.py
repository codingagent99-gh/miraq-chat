"""
parsers/catalog_parser.py — Hybrid parser combining catalog matching, NLP fallback,
semantic vector search, and intent resolution.

Replaces the old monolithic parse_csv_message() in chat.py.
"""

import re
from models import ExtractedEntities, ClassifiedResult, Intent
from classifier import classify
from store_registry import get_store_loader
from chat_logger import get_logger
from utils.entity_helpers import (
    append_category_name, merge_attribute, merge_tags, merge_entities,
    clean_leftovers, STOP_WORDS,
)

logger = get_logger("miraq_chat")


# ══════════════════════════════════════════════════════════════
# PHASE 1: Longest-String Catalog Match
# ══════════════════════════════════════════════════════════════

def phase1_catalog_match(msg: str, loader) -> tuple[ExtractedEntities, str]:
    """
    Run longest-string substring matching against the store catalog.
    Returns (entities, unmatched_text).
    """
    msg_lower = msg.lower()
    entities = ExtractedEntities()
    if not hasattr(entities, 'target_category_slugs'):
        entities.target_category_slugs = set()

    catalog_items = getattr(loader, 'longest_match_catalog', [])

    # Group by identical string (preserves length-sorted order)
    grouped_catalog = {}
    for name, match_type, data in catalog_items:
        if name not in grouped_catalog:
            grouped_catalog[name] = []
        grouped_catalog[name].append((match_type, data))

    unmatched_text = msg_lower

    for name, matches in grouped_catalog.items():
        if len(name) < 3:
            continue

        # Make Phase 1 plural/singular tolerant
        normalized_name = name.replace('-', ' ')
        parts = []
        for w in normalized_name.split():
            if w.endswith('s') and not w.endswith('ss') and len(w) > 3:
                parts.append(rf'{re.escape(w[:-1])}s?')
            else:
                parts.append(re.escape(w))
        flexible_name = r'\s+'.join(parts)
        pattern = r'(?<!\w)(' + flexible_name + r')(?!\w)'

        if not re.search(pattern, unmatched_text):
            continue

        types_matched = [m[0] for m in matches]
        is_collision = 'tag' in types_matched and 'attribute' in types_matched

        if is_collision:
            tag_data = next(m[1] for m in matches if m[0] == 'tag')
            attr_data = next(m[1] for m in matches if m[0] == 'attribute')

            if not hasattr(entities, 'attr_tag_or_pairs'):
                entities.attr_tag_or_pairs = []

            entities.attr_tag_or_pairs.append({
                "tag_slug":      tag_data.get("slug"),
                "attr_taxonomy": attr_data.get("label", ""),
                "attr_term":     attr_data.get("slug"),
                "display_text":  name,
            })
        else:
            for match_type, data in matches:
                if match_type == 'tag':
                    entities.tag_slugs.append(data.get("slug"))
                    entities.tag_ids.append(data.get("id"))
                elif match_type == 'product':
                    if not entities.product_id:
                        entities.product_name = data.get("name")
                        entities.product_slug = data.get("slug", "")
                        entities.product_id = data.get("id")
                elif match_type == 'category':
                    name_lower = data.get("name", "").lower()
                    all_slugs = getattr(loader, 'category_slugs_by_name', {}).get(name_lower, [data.get("slug")])
                    entities.target_category_slugs.update(all_slugs)
                    append_category_name(entities, data.get("name") or "")
                elif match_type == 'attribute':
                    label = data['label']
                    term_slug = data['slug']
                    merge_attribute(entities.attributes, label, term_slug)

        unmatched_text = re.sub(pattern, " ", unmatched_text)

    return entities, unmatched_text


# ══════════════════════════════════════════════════════════════
# PHASE 2: NLP Fallback Merge
# ══════════════════════════════════════════════════════════════

def phase2_nlp_merge(
    unmatched_text: str,
    entities: ExtractedEntities,
    original_nlp_result: ClassifiedResult,
    loader,
    original_msg: str = "",
) -> None:
    """
    Classify the unmatched text and merge NLP entities into the accumulator.
    Mutates `entities` in place.

    Optimization: if Phase 1 matched nothing, unmatched_text == original_msg,
    so re-classifying it would be an exact duplicate of original_nlp_result.
    We reuse it directly instead of making a second classify() call.
    """
    if original_msg and unmatched_text.strip() == original_msg.strip():
        logger.debug("phase2_nlp_merge: Phase 1 matched nothing — reusing original_nlp_result, skipping re-classify")
        nlp_result = original_nlp_result
    else:
        nlp_result = classify(unmatched_text)

    nlp_entities = nlp_result.entities

    # Purge zero-count categories injected by classify()
    if loader and getattr(nlp_entities, 'target_category_slugs', None):
        alive_slugs = {
            s for s in nlp_entities.target_category_slugs
            if loader.category_by_slug.get(s, {}).get("count", 0) > 0
        }
        nlp_entities.target_category_slugs = alive_slugs
        if not alive_slugs:
            nlp_entities.category_name = None

    # Merge positive entities
    merge_entities(entities, nlp_entities)

    # Merge target_attribute(s) from original result (run on full text)
    if getattr(original_nlp_result.entities, 'target_attribute', None):
        entities.target_attribute = original_nlp_result.entities.target_attribute

    if getattr(original_nlp_result.entities, 'target_attributes', None):
        if not hasattr(entities, 'target_attributes'):
            entities.target_attributes = []
        for t_attr in original_nlp_result.entities.target_attributes:
            if t_attr not in entities.target_attributes:
                entities.target_attributes.append(t_attr)

    # Merge action fields from original NLP (full text)
    # Salvage Product & Action fields that were being dropped during masking
    _action_fields = [
        'product_id', 'product_name', 'product_slug',
        'quantity', 'order_id', 'reorder', 'explicit_last_order', 'order_item_name',
        'quick_ship', 'customer_updates', 'billing_updates', 'shipping_updates',
        'customer_fields_requested'
    ]
    for _f in _action_fields:
        _val = getattr(original_nlp_result.entities, _f, None)
        if _val is not None and _val != [] and _val != {}:
            setattr(entities, _f, _val)

    # Merge OR pairs from original result
    if getattr(original_nlp_result.entities, 'attr_tag_or_pairs', None):
        if not hasattr(entities, 'attr_tag_or_pairs'):
            entities.attr_tag_or_pairs = []

        for pair in original_nlp_result.entities.attr_tag_or_pairs:
            if pair not in entities.attr_tag_or_pairs:
                entities.attr_tag_or_pairs.append(pair)

            # Clean up redundant attributes
            attr_term = pair.get("attr_term", "")
            if attr_term and hasattr(entities, 'attributes'):
                keys_to_remove = []
                for k, v in entities.attributes.items():
                    vals = [x.strip() for x in v.split(",")]
                    if attr_term in vals:
                        vals.remove(attr_term)
                        if not vals:
                            keys_to_remove.append(k)
                        else:
                            entities.attributes[k] = ",".join(vals)
                for k in keys_to_remove:
                    del entities.attributes[k]

            # Clean up redundant categories
            cat_slugs = pair.get("cat_slugs", [])
            if cat_slugs and hasattr(entities, 'target_category_slugs'):
                for c_slug in cat_slugs:
                    if c_slug in entities.target_category_slugs:
                        entities.target_category_slugs.remove(c_slug)
                if not entities.target_category_slugs:
                    entities.category_name = None

    return nlp_entities  # caller needs search_term from this

# ══════════════════════════════════════════════════════════════
# PHASE 3: Semantic Vector Search
# ══════════════════════════════════════════════════════════════

def phase3_semantic_search(
    unmatched_text: str,
    nlp_entities,
    entities: ExtractedEntities,
    loader,
) -> None:
    """
    Run semantic vector matching on leftover text.
    Mutates `entities` in place (semantic_matches, search_term).
    """
    strict_search_term = getattr(nlp_entities, 'search_term', None)
    raw_pos_for_vectors = unmatched_text if unmatched_text else strict_search_term
    raw_neg = getattr(nlp_entities, 'excluded_search_term', None)

    cleaned_pos_text = clean_leftovers(raw_pos_for_vectors)
    cleaned_neg_text = clean_leftovers(raw_neg)

    still_unmatched_pos = []
    still_unmatched_neg = []

    if (cleaned_pos_text or cleaned_neg_text) and loader:
        import torch
        from sentence_transformers import util
        SEMANTIC_THRESHOLD = 0.55

        if not hasattr(loader, 'semantic_tensors') or loader.semantic_tensors is None:
            if cleaned_pos_text:
                still_unmatched_pos.append(cleaned_pos_text)
            if cleaned_neg_text:
                still_unmatched_neg.append(cleaned_neg_text)
        else:
            def _process_vectors(term_string, is_negative=False):
                unmatched = []
                phrase = term_string.strip()
                if not phrase:
                    return unmatched

                user_vector = loader.vector_model.encode(phrase, convert_to_tensor=True)
                cosine_scores = util.cos_sim(user_vector, loader.semantic_tensors)[0]
                top_results = torch.topk(cosine_scores, k=3)

                candidates = []
                for score, idx in zip(top_results[0], top_results[1]):
                    if score.item() >= SEMANTIC_THRESHOLD:
                        matched_slug = loader.semantic_keys[idx]
                        candidate_data = loader.semantic_dictionary[matched_slug].copy()
                        candidate_data["user_text"] = phrase
                        candidate_data["score"] = score.item()
                        candidate_data["is_negative"] = is_negative
                        candidates.append(candidate_data)

                if candidates:
                    if not hasattr(entities, 'semantic_matches'):
                        entities.semantic_matches = []
                    entities.semantic_matches.append(candidates)
                else:
                    words = phrase.split()
                    if len(words) > 1:
                        for word in words:
                            w_vector = loader.vector_model.encode(word, convert_to_tensor=True)
                            w_scores = util.cos_sim(w_vector, loader.semantic_tensors)[0]
                            w_top = torch.topk(w_scores, k=3)

                            w_candidates = []
                            for w_score, w_idx in zip(w_top[0], w_top[1]):
                                if w_score.item() >= SEMANTIC_THRESHOLD:
                                    matched_slug = loader.semantic_keys[w_idx]
                                    candidate_data = loader.semantic_dictionary[matched_slug].copy()
                                    candidate_data["user_text"] = word
                                    candidate_data["score"] = w_score.item()
                                    candidate_data["is_negative"] = is_negative
                                    w_candidates.append(candidate_data)

                            if w_candidates:
                                if not hasattr(entities, 'semantic_matches'):
                                    entities.semantic_matches = []
                                entities.semantic_matches.append(w_candidates)
                            else:
                                unmatched.append(word)
                    else:
                        unmatched.append(phrase)

                return unmatched

            if cleaned_pos_text:
                still_unmatched_pos.extend(_process_vectors(cleaned_pos_text, is_negative=False))
            if cleaned_neg_text:
                still_unmatched_neg.extend(_process_vectors(cleaned_neg_text, is_negative=True))
    else:
        if cleaned_pos_text:
            still_unmatched_pos.append(cleaned_pos_text)
        if cleaned_neg_text:
            still_unmatched_neg.append(cleaned_neg_text)

    # Assign search_term from survivors
    if still_unmatched_pos:
        entities.search_term = " ".join(still_unmatched_pos)
    else:
        fallback = clean_leftovers(strict_search_term)
        entities.search_term = fallback if fallback else None

    entities.excluded_search_term = " ".join(still_unmatched_neg) if still_unmatched_neg else None

    # Auto-materialize high-confidence matches
    _auto_materialize(entities)


def _auto_materialize(entities: ExtractedEntities):
    """Promote high-confidence semantic matches into concrete filters."""
    if not (hasattr(entities, 'semantic_matches') and entities.semantic_matches):
        return

    AUTO_APPLY_THRESHOLD = 0.85
    surviving_matches = []

    for candidates in entities.semantic_matches:
        if not candidates:
            continue
        best = max(candidates, key=lambda c: c.get("score", 0))

        if best.get("score", 0) >= AUTO_APPLY_THRESHOLD:
            match_type = best.get("type")
            slug = best.get("slug")

            if match_type == "category" and slug and not entities.target_category_slugs:
                entities.target_category_slugs.add(slug)
                entities.category_name = best.get("suggested_name", slug)
            elif match_type == "tag" and slug and slug not in entities.tag_slugs:
                entities.tag_slugs.append(slug)
                l = get_store_loader()
                if l:
                    tag = l.tag_by_slug.get(slug)
                    if tag:
                        entities.tag_ids.append(tag["id"])
            elif match_type == "attribute" and slug:
                taxonomy = best.get("taxonomy", "")
                if taxonomy and taxonomy not in entities.attributes:
                    entities.attributes[taxonomy] = slug

            entities.search_term = None
        else:
            surviving_matches.append(candidates)

    entities.semantic_matches = surviving_matches


# ══════════════════════════════════════════════════════════════
# PHASE 4: Intent Resolution
# ══════════════════════════════════════════════════════════════

def resolve_final_intent(
    entities: ExtractedEntities,
    original_intent: Intent,
    original_confidence: float,
) -> tuple[Intent, float]:
    """Apply safety lock: ensure the intent matches the extracted entities."""
    catalog_intent_values = {
        "filter_by_attribute", "product_search", "product_variations",
        "product_by_category", "product_by_tag", "product_by_attribute",
        "catalog_search", "category_browse", "product_by_collection", "product_list",
    }
    ACTION_INTENTS = {
        "place_order", "quick_order", "order_item",
        "product_variations", "product_detail", "product_attribute_info",
    }

    resolved_intent = original_intent
    final_confidence = original_confidence

    if resolved_intent.value in ACTION_INTENTS:
        pass
    elif resolved_intent.value in catalog_intent_values or entities.product_id:
        resolved_intent = Intent.PRODUCT_SEARCH if entities.product_id else Intent.FILTER_BY_ATTRIBUTE
        final_confidence = max(final_confidence, 0.95)

    return resolved_intent, final_confidence


# ══════════════════════════════════════════════════════════════
# PUBLIC API — replaces old parse_csv_message()
# ══════════════════════════════════════════════════════════════

def parse_csv_message(msg: str, loader) -> ClassifiedResult | None:
    """Hybrid Parser: Uses Longest-String Substring Matching on natural language."""
    original_nlp_result = classify(msg)

    if not loader:
        return original_nlp_result

    # Phase 1: Catalog match
    entities, unmatched_text = phase1_catalog_match(msg, loader)

    # Phase 2: NLP fallback merge
    # Pass original_msg so phase2 can skip re-classify when Phase 1 matched nothing
    nlp_entities = phase2_nlp_merge(unmatched_text, entities, original_nlp_result, loader, original_msg=msg)

    # Phase 3: Semantic vector search
    phase3_semantic_search(unmatched_text, nlp_entities, entities, loader)

    # Phase 4: Intent resolution
    resolved_intent, final_confidence = resolve_final_intent(
        entities, original_nlp_result.intent, original_nlp_result.confidence
    )

    return ClassifiedResult(intent=resolved_intent, entities=entities, confidence=final_confidence)
