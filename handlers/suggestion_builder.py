"""
handlers/suggestion_builder.py — Build filter suggestions for zero-result responses.

When a search returns 0 products, this module inspects the failed entities and
generates up to 3 actionable alternatives the user can tap to retry:

  1. Similar tags      — failed tag slug → similar slugs from live store data
  2. Related categories — failed category → parent / sibling / component categories
  3. Sibling attributes — failed attribute value → other values for same taxonomy
  4. Broaden           — multiple filters → drop one filter to widen results

Each suggestion is a dict the frontend can render as a tappable chip. When tapped,
the frontend sends the suggestion back as `suggestion_retry` in the /chat payload,
bypassing the classifier and jumping straight to API call construction.

Priority order for top-3 selection:
  Similar tags > Related categories > Sibling attributes > Broaden
"""

from typing import List, Dict
from chat_logger import get_logger

logger = get_logger("miraq_chat")


def build_suggestions(entities, store_loader, limit: int = 3) -> List[Dict]:
    """
    Build up to `limit` filter suggestions from failed entities.

    Args:
        entities:     ExtractedEntities from the failed query
        store_loader: live StoreLoader instance
        limit:        max suggestions to return (default 3)

    Returns:
        List of suggestion dicts, each containing:
          label                — display text e.g. "Try 'Wilde Mosaic' tag"
          type                 — "tag" | "category" | "attribute" | "broaden"
          tag_slugs            — list of tag slugs for the retry
          category_slug        — primary category slug ("" = no category filter)
          extra_category_slugs — additional AND-filter category slugs
          attributes           — attribute filters {attr_name: term}
    """
    if not store_loader:
        return []

    suggestions = []

    # ── Snapshot current filter state ──────────────────────────────────────
    current_tag_slugs = list(getattr(entities, "tag_slugs", []) or [])
    current_category_name = getattr(entities, "category_name", None)
    current_attributes = dict(getattr(entities, "attributes", {}) or {})
    extra_category_ids = list(getattr(entities, "extra_category_ids", []) or [])

    # Resolve primary category → slug + id
    current_category_slug = ""
    current_category_id = None
    if current_category_name:
        cat = store_loader.category_by_name_lower.get(current_category_name.lower())
        if cat:
            current_category_slug = cat.get("slug", "")
            current_category_id = cat.get("id")

    # Resolve extra category slugs
    current_extra_slugs = []
    for cid in extra_category_ids:
        c = store_loader.category_by_id.get(cid)
        if c:
            current_extra_slugs.append(c.get("slug", ""))

    # ── 1. Similar tag suggestions ──────────────────────────────────────────
    # For each failed tag slug, find similar tags. Keep category + attributes.
    for failed_slug in current_tag_slugs:
        if len(suggestions) >= limit:
            break
        similar = store_loader.get_similar_tags(failed_slug, limit=limit)
        for tag in similar:
            if len(suggestions) >= limit:
                break
            new_slugs = [s for s in current_tag_slugs if s != failed_slug] + [tag["slug"]]
            if set(new_slugs) == set(current_tag_slugs):
                continue
            suggestions.append({
                "label": f"Try '{tag['name']}' tag",
                "type": "tag",
                "tag_slugs": new_slugs,
                "category_slug": current_category_slug,
                "extra_category_slugs": current_extra_slugs,
                "attributes": current_attributes,
            })
            logger.debug(
                f"SuggestionBuilder: similar tag | failed={failed_slug!r} → {tag['slug']!r}"
            )

    # ── 2. Related category suggestions ─────────────────────────────────────
    # Category + filters produced 0 → suggest related categories, keep tags + attrs.
    if current_category_id and len(suggestions) < limit:
        related = store_loader.get_related_categories(current_category_id, limit=limit)
        for rel_cat in related:
            if len(suggestions) >= limit:
                break
            rel_slug = rel_cat.get("slug", "")
            if rel_slug == current_category_slug:
                continue
            suggestions.append({
                "label": f"Try '{rel_cat['name']}' category",
                "type": "category",
                "tag_slugs": current_tag_slugs,
                "category_slug": rel_slug,
                "extra_category_slugs": [],
                "attributes": current_attributes,
            })
            logger.debug(
                f"SuggestionBuilder: related category | "
                f"failed={current_category_name!r} → {rel_cat['name']!r}"
            )

    # ── 3. Sibling attribute suggestions ────────────────────────────────────
    # Attribute value produced 0 → suggest other values for same taxonomy.
    if current_attributes and len(suggestions) < limit:
        attr_name_to_slug = {
            "finish":      "pa_finish",
            "visual":      "pa_visual",
            "application": "pa_application",
            "origin":      "pa_origin",
            "edge":        "pa_edge",
            "tile size":   "pa_tile-size",
            "thickness":   "pa_thickness",
        }
        for attr_name, attr_value in current_attributes.items():
            if len(suggestions) >= limit:
                break
            attr_slug = attr_name_to_slug.get(attr_name.lower())
            if not attr_slug:
                attr_slug = f"pa_{attr_name.lower().replace(' ', '-')}"
                if not store_loader.attribute_by_slug.get(attr_slug):
                    continue
            siblings = store_loader.get_sibling_attribute_terms(
                attr_slug, attr_value, limit=limit
            )
            for sibling_term in siblings:
                if len(suggestions) >= limit:
                    break
                new_attrs = {**current_attributes, attr_name: sibling_term}
                suggestions.append({
                    "label": f"Try {sibling_term} {attr_name}",
                    "type": "attribute",
                    "tag_slugs": current_tag_slugs,
                    "category_slug": current_category_slug,
                    "extra_category_slugs": current_extra_slugs,
                    "attributes": new_attrs,
                })
                logger.debug(
                    f"SuggestionBuilder: sibling attribute | "
                    f"failed={attr_name}={attr_value!r} → {sibling_term!r}"
                )
                break  # one sibling per attribute

    # ── 4. Broaden suggestions ───────────────────────────────────────────────
    # Multiple filters AND nothing found above → suggest dropping one filter.
    filter_count = (
        len(current_tag_slugs)
        + (1 if current_category_slug else 0)
        + len(current_attributes)
    )
    if filter_count > 1 and len(suggestions) < limit:
        # Drop category, keep tags + attributes
        if current_category_slug and current_tag_slugs:
            suggestions.append({
                "label": "Search without category filter",
                "type": "broaden",
                "tag_slugs": current_tag_slugs,
                "category_slug": "",
                "extra_category_slugs": [],
                "attributes": current_attributes,
            })
        # Drop attributes, keep category + tags
        if current_attributes and len(suggestions) < limit:
            suggestions.append({
                "label": "Remove attribute filters",
                "type": "broaden",
                "tag_slugs": current_tag_slugs,
                "category_slug": current_category_slug,
                "extra_category_slugs": current_extra_slugs,
                "attributes": {},
            })

    logger.info(
        f"SuggestionBuilder: {len(suggestions)} suggestion(s) | "
        f"types={[s['type'] for s in suggestions]}"
    )
    return suggestions[:limit]