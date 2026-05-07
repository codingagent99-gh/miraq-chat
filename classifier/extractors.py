"""
classifier/extractors.py — All entity extraction functions.

Each function mutates the ExtractedEntities object in-place,
extracting structured data from the raw user text.
"""

import re
import html
from datetime import datetime, timedelta
from typing import Optional

from models import ExtractedEntities
from store_registry import get_store_loader
from config.store_config import (
    PRODUCT_TYPE_TERMS,
    ORIGIN_KEYWORDS,
    GENERIC_NOISE_WORDS,
)
from chat_logger import get_logger
from classifier.utils import (
    normalize_for_tag_compare,
    normalize_dimension,
    create_flexible_pattern,
)

try:
    from dateparser.search import search_dates
    DATEPARSER_AVAILABLE = True
except ImportError:
    DATEPARSER_AVAILABLE = False

logger = get_logger("miraq_chat")


def _legacy_attr_key_from_taxonomy(taxonomy: str) -> str:
    return taxonomy.removeprefix("pa_").replace("-", " ")


def _resolve_attr_key_with_fallback(loader, taxonomy: str, fallback_key: str) -> str:
    neutral_key = taxonomy.removeprefix("pa_").lower().strip()
    if loader and neutral_key and hasattr(loader, "resolve_attribute"):
        attr = loader.resolve_attribute(neutral_key)
        if attr and getattr(attr, "key", ""):
            # Phase 4b.1 keeps legacy "hyphen -> space" output for consumer safety.
            return attr.key.replace("-", " ")
        logger.debug(
            "Classifier: resolve_attribute failed for taxonomy '%s' (neutral_key='%s'); using legacy key '%s'",
            taxonomy,
            neutral_key,
            fallback_key,
        )
    return fallback_key


def _resolve_attr_term_key_with_fallback(loader, taxonomy: str, raw_value: str, fallback_value: str) -> str:
    neutral_key = taxonomy.removeprefix("pa_").lower().strip()
    if loader and neutral_key and hasattr(loader, "resolve_attribute") and hasattr(loader, "resolve_attribute_term"):
        attr = loader.resolve_attribute(neutral_key)
        if not attr:
            logger.debug(
                "Classifier: resolve_attribute failed for taxonomy '%s' while resolving term '%s'; using legacy value '%s'",
                taxonomy,
                raw_value,
                fallback_value,
            )
            return fallback_value
        term = loader.resolve_attribute_term(attr.key, raw_value)
        if term and getattr(term, "key", ""):
            return term.key
        logger.debug(
            "Classifier: resolve_attribute_term failed for attr_key '%s' and raw '%s'; using legacy value '%s'",
            attr.key,
            raw_value,
            fallback_value,
        )
    return fallback_value


def _resolve_tag_key_with_fallback(loader, fallback_slug: str) -> str:
    if loader and fallback_slug and hasattr(loader, "resolve_tag"):
        tag = loader.resolve_tag(fallback_slug)
        if tag and getattr(tag, "key", ""):
            return tag.key
        logger.debug(
            "Classifier: resolve_tag failed for slug '%s'; using legacy slug",
            fallback_slug,
        )
    return fallback_slug


def _resolve_category_key_with_fallback(loader, fallback_slug: str) -> str:
    if loader and fallback_slug and hasattr(loader, "resolve_category"):
        cat = loader.resolve_category(fallback_slug)
        if cat and getattr(cat, "key", ""):
            return cat.key
        logger.debug(
            "Classifier: resolve_category failed for slug '%s'; using legacy slug",
            fallback_slug,
        )
    return fallback_slug


# ══════════════════════════════════════════════════════════════
# DATE PARSING HELPERS
# ══════════════════════════════════════════════════════════════

_MONTH_MAP = {
    'jan': 1, 'january': 1,
    'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,
    'apr': 4, 'april': 4,
    'may': 5,
    'jun': 6, 'june': 6,
    'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,
    'sep': 9, 'september': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}

_MONTH_NAMES_PAT = (
    r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
)


def _parse_day_month(day_str: str, month_str: str, year_str: str = None) -> Optional[datetime]:
    """Parse day + month-name string into a datetime. Defaults year to current."""
    month_str = month_str.lower().strip()
    month_num = None
    for key, val in _MONTH_MAP.items():
        if month_str.startswith(key):
            month_num = val
            break
    if not month_num:
        return None
    year = int(year_str) if year_str else datetime.now().year
    try:
        return datetime(year, month_num, int(day_str))
    except ValueError:
        return None
    
# ══════════════════════════════════════════════════════════════
# NEGATION / EXCLUSION EXTRACTION
# ══════════════════════════════════════════════════════════════

_NEGATION_PATTERNS = [
    r'\bwithout\s+(.+?)(?:\s+(?:items?|products?|ones?)|$)',
    r'\bno\s+(.+?)(?:\s+(?:items?|products?|ones?)|$)',
    r'\bnot\s+(.+?)(?:\s+(?:items?|products?|ones?)|$)',
    r'\bexclude?\s+(.+?)(?:\s+(?:items?|products?|ones?)|$)',
    r'\bavoid\s+(.+?)(?:\s+(?:items?|products?|ones?)|$)',
]


def extract_exclusions(text: str, entities: ExtractedEntities) -> str:
    """Extract negation phrases and resolve them to excluded tags/categories/attributes."""
    loader = get_store_loader()
    if not loader:
        return text

    masked_text = text

    for pattern in _NEGATION_PATTERNS:
        for match in re.finditer(pattern, masked_text, re.IGNORECASE):
            phrase = match.group(1).strip().lower()
            full_match = match.group(0)
            if not phrase or len(phrase) < 2:
                continue

            resolved = False

            for candidate in [phrase] + phrase.split():
                if len(candidate) < 3:
                    continue
                tag_entry = loader.tag_by_name_lower.get(candidate)
                if not tag_entry:
                    tag_obj = loader.resolve_tag(candidate.replace(" ", "-"))
                    if tag_obj:
                        tag_entry = {"slug": tag_obj.key, "id": tag_obj.backend_ref.get("id")}
                if tag_entry and tag_entry.get("slug"):
                    slug = tag_entry["slug"]
                    if slug not in entities.excluded_tags and slug not in entities.tag_slugs:
                        entities.excluded_tags.append(slug)
                        resolved = True
                    break

            if not resolved:
                cat = loader.category_by_name_lower.get(phrase)
                if not cat:
                    cat_id = loader.category_keywords.get(phrase)
                    cat = loader.category_by_id.get(cat_id) if cat_id else None
                if cat and cat.get("slug") and cat["slug"] != "uncategorized":
                    if cat["slug"] not in entities.excluded_categories:
                        entities.excluded_categories.append(cat["slug"])
                        resolved = True

            if not resolved and loader.all_attributes_raw:
                for attr in loader.all_attributes_raw:
                    for term in attr.get("terms", []):
                        if phrase == term.get("name", "").lower() or phrase == term.get("slug", "").replace("-", " "):
                            if not hasattr(entities, 'excluded_attributes'):
                                entities.excluded_attributes = {}
                            tax = attr.get("taxonomy")
                            term_slug = term.get("slug", "")
                            term_key = _resolve_attr_term_key_with_fallback(loader, tax, phrase, term_slug)
                            if tax not in entities.excluded_attributes:
                                entities.excluded_attributes[tax] = []
                            if term_key not in entities.excluded_attributes[tax]:
                                entities.excluded_attributes[tax].append(term_key)
                            resolved = True
                            break
                    if resolved:
                        break

            if resolved:
                masked_text = masked_text.replace(full_match, " ")

    return masked_text


# ══════════════════════════════════════════════════════════════
# PRODUCT NAME EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_product_name(text: str, entities: ExtractedEntities):
    """Extract product name, slug, and ID from text using the store catalog."""
    loader = get_store_loader()
    if not loader:
        return

    match = loader.get_product_for_text(text)
    if not match:
        return

    generic_words = {"product", "products", "item", "items"} | set(PRODUCT_TYPE_TERMS)
    if match["name"].lower().strip() in generic_words:
        return

    matched_name_lower = match["name"].lower()
    if matched_name_lower not in text:
        text_words = set(re.split(r'[\s\-_/]+', text.lower()))
        product_tokens = set(re.split(r'[\s\-_/]+', matched_name_lower))
        overlapping_tokens = text_words & product_tokens
        tag_names_lower = set(loader.tag_by_name_lower.keys())
        tag_words = {t for tag_name in tag_names_lower for t in re.split(r'[\s\-_/]+', tag_name) if t and len(t) > 2}
        if not overlapping_tokens or overlapping_tokens.issubset(tag_words):
            return

    entities.product_name = match["name"]
    entities.product_slug = match.get("slug", "")
    entities.product_id = match.get("id")

    # Dynamic variant upgrade
    if hasattr(loader, 'product_by_name_lower'):
        base_name_lower = match["name"].lower()
        for catalog_name, prod_data in loader.product_by_name_lower.items():
            if catalog_name != base_name_lower and catalog_name.startswith(base_name_lower + " "):
                suffix = catalog_name[len(base_name_lower):].strip()
                if suffix and suffix in text:
                    entities.product_name = prod_data.get("name")
                    entities.product_slug = prod_data.get("slug", "")
                    entities.product_id = prod_data.get("id")
                    break


# ══════════════════════════════════════════════════════════════
# CATEGORY EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_category(text: str, entities: ExtractedEntities) -> str:
    """Extract category matches from text using the store catalog."""
    loader = get_store_loader()
    if not loader or not loader.category_by_key:
        return text

    extracted_cats = []
    longest_match = ""

    for slug, cat_obj in loader.category_by_key.items():
        name_lower = cat_obj.name.lower().strip()
        if len(name_lower) < 3 or cat_obj.count == 0:
            continue
        pattern = create_flexible_pattern(name_lower)
        try:
            if re.search(pattern, text, re.IGNORECASE):
                if len(name_lower) > len(longest_match):
                    longest_match = name_lower
                    entities.category_name = cat_obj.name
                extracted_cats.append({"name": cat_obj.name, "slug": cat_obj.key, "count": cat_obj.count, "id": cat_obj.backend_ref.get("id"), "parent": cat_obj.backend_ref.get("parent_id", 0)})
        except re.error:
            pass

    if not extracted_cats:
        return text

    # Remove eclipsed (subset) categories
    token_sets = [normalize_for_tag_compare(c.get("name", "")) for c in extracted_cats]
    survivors = []
    for i, cat in enumerate(extracted_cats):
        is_eclipsed = any(token_sets[i] < token_sets[j] for j in range(len(extracted_cats)) if i != j)
        if not is_eclipsed:
            survivors.append(cat)
    extracted_cats = survivors

    # Prefer linked children
    extracted_ids = {c.get("id") for c in extracted_cats}
    linked_children = [c for c in extracted_cats if c.get("parent") in extracted_ids]

    if not hasattr(entities, 'target_category_slugs'):
        entities.target_category_slugs = set()

    if linked_children:
        for child in linked_children:
            entities.target_category_slugs.add(_resolve_category_key_with_fallback(loader, child.get("slug")))
    else:
        for cat in extracted_cats:
            entities.target_category_slugs.add(_resolve_category_key_with_fallback(loader, cat.get("slug")))

    return text


# ══════════════════════════════════════════════════════════════
# ATTRIBUTE EXTRACTION
# ═══════════════════════════════��══════════════════════════════

def extract_attributes(text: str, entities: ExtractedEntities) -> str:
    """Extract WooCommerce product attributes from text."""
    loader = get_store_loader()
    if not loader or not loader.all_attributes_raw:
        return text

    masked_text = text

    for attr in loader.all_attributes_raw:
        label = attr.get("attribute_label", "").lower().strip()
        taxonomy = attr.get("taxonomy", "")
        terms = attr.get("terms", [])
        if not label or not taxonomy or not terms:
            continue

        is_dimensional = any(kw in label for kw in ['size', 'thickness', 'weight', 'width', 'length', 'depth', 'dimension'])

        # Origin keyword handling
        if "origin" in label:
            if _try_origin_match(masked_text, entities, loader, taxonomy):
                continue

        product_name_lower = (entities.product_name or "").lower()

        for term in terms:
            term_name = term.get("name", "")
            term_name_lower = term_name.lower().strip()
            if not term_name_lower or len(term_name_lower) < 1:
                continue
            if product_name_lower and term_name_lower in product_name_lower:
                continue

            try:
                matched_pattern = _match_term_in_text(masked_text, term_name_lower, is_dimensional)
                if not matched_pattern:
                    continue

                _resolve_attribute_or_tag(
                    entities, loader, text, taxonomy, label, term,
                    term_name_lower, is_dimensional, matched_pattern,
                )
                break
            except re.error:
                pass

    return masked_text


def _try_origin_match(text: str, entities, loader, taxonomy: str) -> bool:
    """Try matching origin keywords. Returns True if matched."""
    for keyword, normalized in ORIGIN_KEYWORDS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            tag_ids = loader.get_tag_ids_for_keyword(normalized)
            if not tag_ids:
                tag_ids = loader.get_tag_ids_for_keyword(f"made in {normalized}")
            legacy_attr_key = _legacy_attr_key_from_taxonomy(taxonomy)
            attr_key = _resolve_attr_key_with_fallback(loader, taxonomy, legacy_attr_key)
            term_key = _resolve_attr_term_key_with_fallback(loader, taxonomy, normalized, normalized)
            entities.attributes[attr_key] = term_key
            entities.tag_ids.extend(tag_ids)
            for tid in tag_ids:
                tag = loader.tag_by_id.get(tid)
                if tag:
                    entities.tag_slugs.append(_resolve_tag_key_with_fallback(loader, tag["slug"]))
            return True
    return False


def _match_term_in_text(text: str, term_lower: str, is_dimensional: bool) -> Optional[str]:
    """Try to match a term in the text. Returns the matched regex pattern or None."""
    if is_dimensional:
        term_dim = normalize_dimension(term_lower)
        if term_dim and re.search(r'\d', term_dim):
            escaped_dim = re.escape(term_dim)
            if 'x' in escaped_dim:
                escaped_dim = escaped_dim.replace('x', r'\s*"?\s*(?:x|by|×)\s*')
            dim_pattern = rf"(?<!\d){escaped_dim}\s*(?:\"|'|mm|cm|inch(?:es)?|in\.?|thick|lbs?|oz|kg|g)?(?!\d)"
            match = re.search(dim_pattern, text, re.IGNORECASE)
            if match:
                if 'x' not in term_dim:
                    start, end = match.span()
                    ctx_before = text[max(0, start - 8):start]
                    ctx_after = text[end:min(len(text), end + 8)]
                    if re.search(r'\d\s*(?:\"|\')?\s*(?:x|X|by|×)\s*$', ctx_before) or \
                       re.search(r'^\s*(?:\"|\')?\s*(?:x|X|by|×)\s*\d', ctx_after):
                        return None
                return dim_pattern

    if re.search(rf"\b{re.escape(term_lower)}\b", text):
        return rf"\b{re.escape(term_lower)}\b"
    if re.search(rf"\b{re.escape(term_lower)}s\b", text):
        return rf"\b{re.escape(term_lower)}s\b"
    if len(term_lower) > 4 and re.search(rf"\b{re.escape(term_lower[:-1])}\b", text):
        return rf"\b{re.escape(term_lower[:-1])}\b"
    return None


def _resolve_attribute_or_tag(
    entities, loader, original_text: str, taxonomy: str, label: str,
    term: dict, term_name_lower: str, is_dimensional: bool, matched_pattern: str,
):
    """Decide whether a matched term becomes an attribute, tag, or OR pair."""
    covered_by_tag = False
    covering_tag_slug = None
    covering_tag_id = None
    exact_tag_matched = False

    if loader:
        norm_text = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9/.]', ' ', html.unescape(original_text))).strip()

        for tag_name_lower, tag_entry in loader.tag_by_name_lower.items():
            if tag_entry.get("count", 0) == 0:
                continue

            if is_dimensional:
                tag_digits = re.sub(r'[^0-9]', '', tag_entry.get("slug", ""))
                term_digits = re.sub(r'[^0-9]', '', term_name_lower)
                if tag_digits and term_digits and tag_digits == term_digits:
                    covered_by_tag = True
                    covering_tag_slug = tag_entry.get("slug", "")
                    covering_tag_id = tag_entry.get("id")
                    norm_tag = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9/.]', ' ', html.unescape(tag_name_lower))).strip()
                    if (norm_tag and re.search(rf'\b{re.escape(norm_tag)}\b', norm_text)) or \
                       re.search(create_flexible_pattern(tag_name_lower), original_text):
                        exact_tag_matched = True
                    break
            else:
                term_tokens = normalize_for_tag_compare(term_name_lower)
                tag_tokens = normalize_for_tag_compare(tag_name_lower)
                if term_tokens and term_tokens <= tag_tokens:
                    covered_by_tag = True
                    covering_tag_slug = tag_entry.get("slug", "")
                    covering_tag_id = tag_entry.get("id")
                    norm_tag = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9/.]', ' ', html.unescape(tag_name_lower))).strip()
                    if (norm_tag and re.search(rf'\b{re.escape(norm_tag)}\b', norm_text)) or \
                       re.search(create_flexible_pattern(tag_name_lower), original_text):
                        exact_tag_matched = True
                    break

    # ── Has product context = product_name OR order_item_name is set ──
    # When the user says "order Aura matte white", order_item_name="Aura" is set
    # early, so attributes should route to entities.attributes not tag/or_pair mode.
    _has_product_ctx = bool(entities.product_name or getattr(entities, 'order_item_name', None))

    if exact_tag_matched and not _has_product_ctx:
        if covering_tag_id not in entities.tag_ids:
            entities.tag_ids.append(covering_tag_id)
            entities.tag_slugs.append(_resolve_tag_key_with_fallback(loader, covering_tag_slug))
    elif covered_by_tag and not _has_product_ctx:
        entities.attr_tag_or_pairs.append({
            "tag_slug": _resolve_tag_key_with_fallback(loader, covering_tag_slug),
            "attr_taxonomy": taxonomy,
            # Prefer matching by human term name for neutral resolution, but keep
            # WooCommerce slug fallback so output strings remain identical for current consumers.
            "attr_term": _resolve_attr_term_key_with_fallback(
                loader,
                taxonomy,
                term.get("name", term.get("slug", "")),
                term.get("slug", term.get("name", "")),
            ),
        })
    else:
        attr_key = _resolve_attr_key_with_fallback(loader, taxonomy, label)
        term_key = _resolve_attr_term_key_with_fallback(
            loader,
            taxonomy,
            term.get("name", term.get("slug", "")),
            term.get("slug", term.get("name", "")),
        )
        entities.attributes[attr_key] = term_key
        entities.attribute_slug = taxonomy
        entities.attribute_term_ids = [term["id"]]

# ══════════════════════════════════════════════════════════════
# TAG EXTRACTION
# ═════════════════════════════════════════════════════════���════

def extract_tag(text: str, entities: ExtractedEntities) -> str:
    """Extract WooCommerce product tags from text."""
    loader = get_store_loader()
    if not loader:
        return text

    masked_text = text
    existing_ids = set(entities.tag_ids)
    resolved_attr_token_sets = [normalize_for_tag_compare(v) for v in entities.attributes.values() if v]

    # Build category base words to avoid matching
    cat_base_words = _build_cat_base_words(entities, loader)

    # Collect candidates
    candidates = []
    for name_lower, tag in loader.tag_by_name_lower.items():
        if tag["id"] in existing_ids or tag.get("count", 0) == 0 or len(name_lower) < 4:
            continue
        if name_lower in cat_base_words:
            continue
        tag_tokens = normalize_for_tag_compare(name_lower)
        if tag_tokens and any(tag_tokens <= ats for ats in resolved_attr_token_sets):
            continue

        matched = _try_tag_match(name_lower, tag, masked_text)
        if matched:
            candidates.append((tag, name_lower, matched))

    # Deduplicate: remove tags whose tokens are a strict subset of another candidate
    all_token_sets = [normalize_for_tag_compare(n) for _, n, _ in candidates]
    for i, (tag, name_lower, pattern) in enumerate(candidates):
        is_subset = all_token_sets[i] and any(
            all_token_sets[i] < other for j, other in enumerate(all_token_sets) if j != i
        )
        if not is_subset:
            entities.tag_ids.append(tag["id"])
            entities.tag_slugs.append(_resolve_tag_key_with_fallback(loader, tag["slug"]))

    return masked_text


def _build_cat_base_words(entities, loader) -> set:
    """Collect category name words to prevent tag false-positives."""
    cat_base_words = set()
    all_cat_names = []
    if entities.category_name:
        all_cat_names.append(entities.category_name.lower())
    if hasattr(entities, 'target_category_slugs'):
        for slug in entities.target_category_slugs:
            cat_obj = loader.resolve_category(slug)
            if cat_obj and cat_obj.name:
                all_cat_names.append(cat_obj.name.lower())
    for cname in all_cat_names:
        cat_base_words.add(cname)
        if cname.endswith("s") and len(cname) > 3:
            cat_base_words.add(cname[:-1])
    return cat_base_words


def _try_tag_match(name_lower: str, tag: dict, text: str) -> Optional[str]:
    """Try multiple matching strategies for a tag. Returns matched pattern or None."""
    # Exact name match
    try:
        if re.search(rf'\b{re.escape(name_lower)}\b', text):
            return rf'\b{re.escape(name_lower)}\b'
    except re.error:
        pass

    # Flexible plural match
    pattern = create_flexible_pattern(name_lower)
    if pattern != rf'\b{re.escape(name_lower)}\b':
        try:
            if re.search(pattern, text):
                return pattern
        except re.error:
            pass

    # Slug-based match
    slug_words = tag["slug"].replace("-", " ")
    if slug_words != name_lower and len(slug_words) >= 4:
        pattern = create_flexible_pattern(slug_words)
        try:
            if re.search(pattern, text):
                return pattern
        except re.error:
            pass

    # Raw slug match
    if len(tag["slug"]) >= 4:
        try:
            if re.search(rf'(?<![\w]){re.escape(tag["slug"])}(?![\w])', text):
                return rf'(?<![\w]){re.escape(tag["slug"])}(?![\w])'
        except re.error:
            pass

    return None


# ══════════════════════════════════════════════════════════════
# SIMPLE EXTRACTION FUNCTIONS
# ══════════════════════════════════════════════════════════════

def extract_quantity(text: str, entities: ExtractedEntities):
    """Extract numeric quantity from order-related text."""
    match = re.search(r'(\d+)\s*(qty|quantity|pcs|pieces|units|boxes|sq\s*ft)', text)
    if match:
        entities.quantity = int(match.group(1))
        return
    match = re.search(r'\b(?:order|buy|purchase|place\s+(?:an?\s+)?order)(?:\s+for)?\s+(\d+)\b', text)
    if match:
        entities.quantity = int(match.group(1))
        return
    match = re.search(r'\b(\d+)\s+of\s+(?:this|these|them|it|the)\b', text)
    if match:
        entities.quantity = int(match.group(1))


def extract_order_id(text: str, entities: ExtractedEntities):
    """Extract order ID from text."""
    match = re.search(r'order\s*#?\s*(\d+)', text)
    if match:
        entities.order_id = int(match.group(1))


def extract_collection_year(text: str, entities: ExtractedEntities):
    """Extract collection year and associated tags."""
    loader = get_store_loader()
    year_match = re.search(r'\b(20[12]\d)\s*(collection|series)?\b', text)
    if year_match:
        year = year_match.group(1)
        entities.collection_year = year
        if loader:
            tag_ids = loader.get_tag_ids_for_keyword(year)
            entities.tag_ids.extend(tag_ids)
            for tid in tag_ids:
                tag = loader.tag_by_id.get(tid)
                if tag:
                    entities.tag_slugs.append(_resolve_tag_key_with_fallback(loader, tag["slug"]))


def extract_stock_status(text: str, entities: ExtractedEntities):
    """Extract stock and sale status flags."""
    if re.search(r"\b(?:out\s+of\s+stock|no\s+stock|unavailable)\b", text, re.IGNORECASE):
        entities.in_stock = False
    elif re.search(r"\b(?:in\s+stock|available)\b", text, re.IGNORECASE):
        entities.in_stock = True
    if re.search(r"\b(?:on\s+sale|discount(?:ed)?|clearance)\b", text, re.IGNORECASE):
        entities.on_sale = True


def extract_price_range(text: str, entities: ExtractedEntities):
    """Extract min/max price filters."""
    for pattern in [
        r'between\s+\$?(\d+(?:\.\d+)?)\s+(?:and|to|-)\s+\$?(\d+(?:\.\d+)?)',
        r'\$(\d+(?:\.\d+)?)\s+to\s+\$?(\d+(?:\.\d+)?)',
        r'\$(\d+(?:\.\d+)?)\s*[-–]\s*\$?(\d+(?:\.\d+)?)',
    ]:
        m = re.search(pattern, text)
        if m:
            entities.min_price, entities.max_price = float(m.group(1)), float(m.group(2))
            return
    for pattern in [
        r'(?:under|below|less\s+than|cheaper\s+than|max(?:imum)?|at\s+most|up\s+to)\s+\$?(\d+(?:\.\d+)?)',
        r'\$(\d+(?:\.\d+)?)\s+(?:or\s+)?(?:less|under|below)',
    ]:
        m = re.search(pattern, text)
        if m:
            entities.max_price = float(m.group(1))
            return
    for pattern in [
        r'(?:over|above|more\s+than|at\s+least|min(?:imum)?)\s+\$?(\d+(?:\.\d+)?)',
        r'\$(\d+(?:\.\d+)?)\s+(?:or\s+)?(?:more|above|over)',
    ]:
        m = re.search(pattern, text)
        if m:
            entities.min_price = float(m.group(1))
            return


def extract_order_item(text: str, entities: ExtractedEntities):
    """Extract the product name from an order intent."""
    if not re.search(r"\b(order|buy|purchase|get|want)\b", text):
        return
    if re.search(r"\b(history|track|tracking|status|before|past|previous|show|tell|about|detail)\b", text):
        return
    loader = get_store_loader()
    if loader:
        match = loader.get_product_for_text(text)
        if match:
            entities.order_item_name = match["name"]
            return
    for pattern in [
        r"\b(?:order|buy|purchase|get|want)\b.*?\b(?:this\s+item\s+)?([A-Z][a-zA-Z]+)",
        r"\bi\s+want\s+(?:to\s+)?(?:order|buy|purchase|get)\s+(\w+)",
    ]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().lower()
            stop = {"this", "that", "item", "product", "items", "some", "the", "a", "an", "my", "again", "more", "it", "them", "these", "those", "for", "to", "of"} | set(PRODUCT_TYPE_TERMS)
            if candidate not in stop:
                entities.order_item_name = candidate.title()
                return


_UNRESOLVABLE_DESCRIPTORS = [
    "durable", "heavy-duty", "heavy duty", "premium", "luxury",
    "rustic", "modern", "classic", "natural", "affordable", "budget",
]


def extract_unresolved_descriptors(text: str, entities: ExtractedEntities):
    """Tag unresolvable descriptors as search hints."""
    hints = []
    for descriptor in _UNRESOLVABLE_DESCRIPTORS:
        if re.search(rf'\b{re.escape(descriptor.lower())}\b', text):
            if not (descriptor.lower() in {v.lower() for v in entities.attributes.values()}
                    or any(descriptor.lower() in slug.replace("-", " ") for slug in entities.tag_slugs)):
                hints.append(descriptor)
    if hints and hasattr(entities, 'search_hints'):
        entities.search_hints = hints


def detect_tag_operator(text: str, entities: ExtractedEntities):
    """Detect OR operator between multiple tags."""
    if len(entities.tag_slugs) < 2 or not re.search(r'\bor\b|\beither\b', text):
        return
    slug_word_sets = [set(slug.replace("-", " ").split()) for slug in entities.tag_slugs]
    text_words = set(text.split())
    if sum(1 for words in slug_word_sets if words & text_words) >= 2:
        entities.tag_operator = "OR"


# ══════════════════════════════════════════════════════════════
# TIME RANGE EXTRACTION
# ══════════════════════════════════════════════════════════════

def _normalize_fused_dates(text: str) -> str:
    """Fix missing spaces in fused date expressions."""
    text = re.sub(r'\b(last|past|this)(day|week|month|year)s?\b', r'\1 \2', text)
    text = re.sub(r'\b(last|past|this)(\d+)(days|weeks|months|years)\b', r'\1 \2 \3', text)
    text = re.sub(r'\b(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(20\d{2})\b', r'\1-\2-\3', text)
    return text


def extract_time_range(text: str, entities: ExtractedEntities):
    """Extract date/time ranges for order history queries."""
    text_lower = text.lower()
    now = datetime.now()

    # ── Relative: "last/past N days/weeks/months/years" ──────────────────────
    m_rel = re.search(r'(?:last|past)\s+(\d+)\s+(day|week|month|year)s?', text_lower)
    if m_rel:
        amount = int(m_rel.group(1))
        unit = m_rel.group(2)
        if unit == 'day':
            start = now - timedelta(days=amount)
        elif unit == 'week':
            start = now - timedelta(weeks=amount)
        elif unit == 'month':
            start = now - timedelta(days=amount * 30)
        else:
            start = now - timedelta(days=amount * 365)
        entities.date_after  = start.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
        entities.date_before = now.replace(  hour=23, minute=59, second=59, microsecond=999999).isoformat()
        return

    # ── "this week/month/year" ────────────────────────────────────────────────
    m_this = re.search(r'\b(?:this)\s+(week|month|year)\b', text_lower)
    if m_this:
        unit = m_this.group(1)
        if unit == 'week':
            start = now - timedelta(days=now.weekday())
        elif unit == 'month':
            start = now.replace(day=1)
        else:
            start = now.replace(month=1, day=1)
        entities.date_after  = start.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
        entities.date_before = now.replace(  hour=23, minute=59, second=59, microsecond=999999).isoformat()
        return

    # ── "between 7 feb and 31 march" (month on both sides) ───────────────────
    m_between = re.search(
        rf'\bbetween\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES_PAT})(?:\s+(\d{{4}}))?\s+'
        rf'and\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES_PAT})(?:\s+(\d{{4}}))?',
        text_lower
    )
    if m_between:
        d1, m1, y1 = m_between.group(1), m_between.group(2), m_between.group(3)
        d2, m2, y2 = m_between.group(4), m_between.group(5), m_between.group(6)
        start = _parse_day_month(d1, m1, y1)
        end   = _parse_day_month(d2, m2, y2)
        if start and end:
            if end < start:
                end = end.replace(year=end.year + 1)
            entities.date_after  = start.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
            entities.date_before = end.replace(  hour=23, minute=59, second=59, microsecond=999999).isoformat()
            return

    # ── "between 6th and 7th dec" (shared month at end) ──────────────────────
    m_between_shared = re.search(
        rf'\bbetween\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+and\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES_PAT})(?:\s+(\d{{4}}))?',
        text_lower
    )
    if m_between_shared:
        d1  = m_between_shared.group(1)
        d2  = m_between_shared.group(2)
        mon = m_between_shared.group(3)
        yr  = m_between_shared.group(4)
        start = _parse_day_month(d1, mon, yr)
        end   = _parse_day_month(d2, mon, yr)
        if start and end:
            entities.date_after  = start.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
            entities.date_before = end.replace(  hour=23, minute=59, second=59, microsecond=999999).isoformat()
            return

    # ── Guard: only run dateparser if order-related ───────────────────────────
    if not re.search(r'\b(order|orders|ordered|purchase|purchased|bought|buy|history)\b', text_lower):
        return

    if not DATEPARSER_AVAILABLE:
        return

    normalized = _normalize_fused_dates(text_lower)

    # Strip tile/sample-size dimension patterns before dateparser
    normalized = re.sub(r'\d+\s*["\']?\s*[xX×]\s*["\']?\d+\s*["\']?', ' ', normalized)
    normalized = re.sub(r'\b\d+\s*["\'](?!\s*(?:[xX×]|\d))', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    parsed = search_dates(normalized, settings={'PREFER_DATES_FROM': 'past', 'RETURN_AS_TIMEZONE_AWARE': False})
    if parsed:
        dates = sorted(
            p[1].replace(tzinfo=None) if p[1].tzinfo else p[1]
            for p in parsed
        )
        entities.date_after  = dates[0].replace( hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
        entities.date_before = dates[-1].replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()

# ══════════════════════════════════════════════════════════════
# CUSTOMER UPDATE / FETCH EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_customer_updates(text: str, entities: ExtractedEntities):
    """Extract customer profile update fields."""
    TOP_LEVEL = {"first name": "first_name", "last name": "last_name", "username": "username", "first_name": "first_name", "last_name": "last_name"}
    BILLING = {"billing first name": "first_name", "billing last name": "last_name", "billing company": "company", "billing address": "address_1", "billing address 1": "address_1", "billing city": "city", "billing state": "state", "billing postcode": "postcode", "billing zip": "postcode", "billing country": "country", "billing email": "email", "billing phone": "phone"}
    SHIPPING = {"shipping first name": "first_name", "shipping last name": "last_name", "shipping company": "company", "shipping address": "address_1", "shipping address 1": "address_1", "shipping city": "city", "shipping state": "state", "shipping postcode": "postcode", "shipping zip": "postcode", "shipping country": "country"}

    _UPDATE_RE = r"\b(?:change|update|set|edit|modify)\b"

    def _val(phrase):
        m = re.search(rf"{_UPDATE_RE}[^.]*?\b{re.escape(phrase)}\b[^.]*?\bto\b\s+(.+?)(?:\s*[.,]|$)", text, re.IGNORECASE)
        if m:
            return m.group(1).strip().strip("\"'")
        m = re.search(rf"\bmy\b[^.]*?\b{re.escape(phrase)}\b[^.]*?\b(?:is|should be|will be)\b\s+(.+?)(?:\s*[.,]|$)", text, re.IGNORECASE)
        if m:
            return m.group(1).strip().strip("\"'")
        return None

    for phrase, key in TOP_LEVEL.items():
        v = _val(phrase)
        if v:
            entities.customer_updates[key] = v

    if not entities.customer_updates.get("first_name"):
        m = re.search(r"\b(?:change|update|set|edit|modify)\b[^.]*?\bmy\s+name\b[^.]*?\bto\b\s+(.+?)(?:\s*[.,]|$)", text, re.IGNORECASE)
        if m:
            parts = m.group(1).strip().strip("\"'").split()
            if len(parts) >= 2:
                entities.customer_updates["first_name"] = parts[0]
                entities.customer_updates["last_name"] = " ".join(parts[1:])
            elif len(parts) == 1:
                entities.customer_updates["first_name"] = parts[0]

    for phrase, key in BILLING.items():
        v = _val(phrase)
        if v:
            entities.billing_updates[key] = v

    for phrase, key in SHIPPING.items():
        v = _val(phrase)
        if v:
            entities.shipping_updates[key] = v


def extract_customer_fetch(text: str, entities: ExtractedEntities):
    """Extract customer field fetch requests."""
    FETCH_RE = r"\b(?:show|get|what(?:'?s| is)|display|tell me)\b"
    FIELDS = {
        "first name": "first_name", "last name": "last_name", "username": "username",
        "name": "full_name", "billing phone": "billing.phone", "billing address": "billing.address_1",
        "shipping address": "shipping.address_1", "email": "email", "phone": "billing.phone",
    }
    for phrase, key in FIELDS.items():
        m = re.search(rf"{FETCH_RE}[^.]*?\bmy\b[^.]*?\b{re.escape(phrase)}\b", text, re.IGNORECASE)
        if m:
            entities.customer_fields_requested.append(key)


# ══════════════════════════════════════════════════════════════
# LEFTOVER ISOLATOR (for Vector AI)
# ══════════════════════════════════════════════════════════════

_CONVERSATIONAL_FILLER = {
    "do", "you", "have", "are", "there", "any", "what", "is", "the",
    "show", "me", "find", "looking", "for", "i", "want", "to", "buy",
    "get", "a", "an", "can", "could", "some", "like", "use", "using",
    "please", "give", "would", "need", "has", "that",
    "all", "this", "these", "those", "it", "them", "my", "our", "their",
    "only", "just", "very", "much", "many",
    "which", "who", "when", "where", "how", "whose", "whom",
}


def isolate_unrecognized_terms(text: str, entities: ExtractedEntities):
    """Mask out extracted entities and route leftovers to positive/negative search terms."""
    loader = get_store_loader()
    used_tokens = set()

    # Collect matched tokens
    if getattr(entities, 'product_name', None):
        used_tokens.update(normalize_for_tag_compare(entities.product_name))
    if getattr(entities, 'category_name', None):
        used_tokens.update(normalize_for_tag_compare(entities.category_name))
    for cat in getattr(entities, 'target_category_slugs', set()):
        used_tokens.update(normalize_for_tag_compare(cat.replace("-", " ")))
    for slug in getattr(entities, 'tag_slugs', []):
        used_tokens.update(normalize_for_tag_compare(slug.replace("-", " ")))
    for term in getattr(entities, 'attributes', {}).values():
        used_tokens.update(normalize_for_tag_compare(term.replace("-", " ")))

    if getattr(entities, 'in_stock', None) is not None:
        used_tokens.update(["out", "of", "stock", "in", "available", "unavailable"])

    used_tokens.update(_CONVERSATIONAL_FILLER)
    used_tokens.update(set(kw.lower() for kw in GENERIC_NOISE_WORDS))
    used_tokens.update(set(kw.lower() for kw in PRODUCT_TYPE_TERMS))
    if loader and hasattr(loader, '_store_generic_terms'):
        used_tokens.update(loader._store_generic_terms)

    leftover_text = text.lower()
    for token in used_tokens:
        if len(token) > 2 or token in _CONVERSATIONAL_FILLER:
            pattern = create_flexible_pattern(token)
            leftover_text = re.sub(pattern, ' ', leftover_text, flags=re.IGNORECASE)

    leftover_phrase = re.sub(r'[^a-z0-9, ]', ' ', leftover_text)
    leftover_phrase = re.sub(r'\s+', ' ', leftover_phrase).strip()

    positive = []
    negative = []
    for chunk in leftover_phrase.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        neg_match = re.match(r'^(without|no|not|exclude|avoid)\s+(.+)$', chunk)
        if neg_match:
            clean = neg_match.group(2).strip()
            if len(clean) >= 3:
                negative.append(clean)
        else:
            if len(chunk) >= 3:
                positive.append(chunk)

    if positive:
        entities.search_term = ", ".join(positive)
    if negative:
        if not hasattr(entities, 'excluded_search_term'):
            entities.excluded_search_term = None
        entities.excluded_search_term = ", ".join(negative)
