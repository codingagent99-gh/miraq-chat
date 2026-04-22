"""
classifier — Intent classification and entity extraction pipeline.

Public API:
  - classify(utterance) → ClassifiedResult
"""

import re

from models import ExtractedEntities, ClassifiedResult
from store_registry import get_store_loader
from config.store_config import PRODUCT_TYPE_TERMS, GENERIC_NOISE_WORDS
from chat_logger import get_logger

from classifier.evaluators import get_default_pipeline
from classifier.consolidation import consolidate_entities
from classifier.extractors import (
    extract_exclusions,
    extract_product_name,
    extract_category,
    extract_quantity,
    extract_attributes,
    extract_tag,
    extract_collection_year,
    extract_order_id,
    extract_time_range,
    extract_order_item,
    extract_unresolved_descriptors,
    extract_price_range,
    extract_customer_updates,
    detect_tag_operator,
    extract_customer_fetch,
    extract_stock_status,
    isolate_unrecognized_terms,
)

logger = get_logger("miraq_chat")


def classify(utterance: str) -> ClassifiedResult:
    """Classify user utterance into intent + entities using the Evaluation Pipeline."""
    text = utterance.lower().strip()
    entities = ExtractedEntities()

    # ─── 1. Negative constraints ───
    text = extract_exclusions(text, entities)

    # ─── 1.5. Noise reduction ───
    entity_text = text
    for gw in GENERIC_NOISE_WORDS:
        entity_text = re.sub(rf'\b{re.escape(gw)}\b', ' ', entity_text, flags=re.IGNORECASE)

    # ─── 2. Core entity extraction ───
    extract_product_name(entity_text, entities)
    extract_category(entity_text, entities)
    extract_quantity(text, entities)
    extract_order_item(text, entities)

    # ─── 2.5. Prepare masked text for attribute/tag extraction ───
    attr_text = entity_text
    tag_text = text

    if entities.product_name:
        p_lower = entities.product_name.lower()
        attr_text = attr_text.replace(p_lower, " ")
        tag_text = tag_text.replace(p_lower, " ")
        
    # ── Also mask order_item_name so the product word isn't matched as an attribute ──
    _oi = getattr(entities, 'order_item_name', None)
    if _oi and _oi.lower() != (entities.product_name or "").lower():
        _oi_lower = _oi.lower()
        attr_text = attr_text.replace(_oi_lower, " ")
        tag_text = tag_text.replace(_oi_lower, " ")

    if entities.quantity:
        attr_text = re.sub(rf'(?<![\dxX\-])\b{entities.quantity}\b(?![\dxX\-])', ' ', attr_text, count=1)
        tag_text = re.sub(rf'(?<![\dxX\-])\b{entities.quantity}\b(?![\dxX\-])', ' ', tag_text, count=1)

    _loader = get_store_loader()
    type_mask_terms = set(pt.lower() for pt in PRODUCT_TYPE_TERMS)
    if _loader:
        type_mask_terms |= _loader._store_generic_terms
    for pt in type_mask_terms:
        attr_text = re.sub(rf'\b{re.escape(pt)}\b', ' ', attr_text).strip()

    # ─── 2.6. Question mask ───
    attr_text = _apply_question_mask(text, attr_text, entities)

    # ─── 3. Positive constraints ───
    extract_attributes(attr_text, entities)
    extract_tag(tag_text, entities)

    # ─── 4. Secondary extractions ───
    extract_collection_year(text, entities)
    extract_order_id(text, entities)
    logger.debug("ClassifierPipeline: Calling extract_time_range")
    extract_time_range(text, entities)
    logger.debug(f"ClassifierPipeline: After extract_time_range | entities={entities}")
    extract_unresolved_descriptors(text, entities)
    extract_price_range(text, entities)
    extract_customer_updates(text, entities)
    detect_tag_operator(text, entities)
    extract_customer_fetch(text, entities)
    extract_stock_status(text, entities)

    # ─── 4.5. Isolate leftovers for vector AI ───
    isolate_unrecognized_terms(text, entities)

    # ─── 5. Intent pipeline ───
    pipeline = get_default_pipeline()
    intent, confidence = pipeline.evaluate(text, entities)

    # ─── 6–7. Post-classification consolidation ───
    consolidate_entities(intent, entities, text)

    return ClassifiedResult(intent=intent, entities=entities, confidence=confidence)


def _apply_question_mask(full_text: str, attr_text: str, entities: ExtractedEntities) -> str:
    """
    Prevent "what colors/sizes/etc" from false attribute extraction.
    When the user asks *about* an attribute, mask it from the attribute extractor
    and record it as a target_attribute instead.
    """
    question_re = r'\b(?:what|which|how\s+many|tell\s+me\s+(?:the|about))\s+(?:[\w\']+\s+){0,3}'
    loader = get_store_loader()
    if not loader or not loader.all_attributes_raw:
        return attr_text

    sorted_attrs = sorted(loader.all_attributes_raw, key=lambda a: len(a.get("attribute_label", "")), reverse=True)
    words_to_mask = set()

    for attr in sorted_attrs:
        label = attr.get("attribute_label", "").lower().strip()
        if not label:
            continue
        for word in ([label] + label.split()):
            if len(word) < 4:
                continue
            if re.search(question_re + re.escape(word) + r's?\b', full_text, re.IGNORECASE):
                words_to_mask.add(re.escape(word) + r's?')
                if label not in entities.target_attributes:
                    entities.target_attributes.append(label)
                break

    for w in words_to_mask:
        attr_text = re.sub(w, ' ', attr_text, flags=re.IGNORECASE)

    if entities.target_attributes:
        entities.target_attribute = entities.target_attributes[0]

    return attr_text