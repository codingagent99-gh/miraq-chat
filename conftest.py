"""
Pytest configuration and fixtures for miraq-chat tests.

Provides a mock StoreLoader with test product/category/tag/attribute data
to support classifier entity extraction tests.
"""

import pytest
from store_registry import set_store_loader, get_store_loader
from typing import Dict, List, Optional


class MockStoreLoader:
    """
    Mock StoreLoader for testing classifier entity extraction.
    Populated with real product data from the store.
    """

    def __init__(self):
        # Product data (from products.txt)
        self.products = [
            {"id": 7272, "name": "Allspice", "slug": "allspice"},
            {"id": 7275, "name": "Ansel", "slug": "ansel-2"},
            {"id": 7276, "name": "Ansel Mosaic", "slug": "ansel-mosaic-2"},
            {"id": 7265, "name": "Cairo Mosaic", "slug": "cairo-mosaic-2"},
            {"id": 7261, "name": "Cord", "slug": "cord-2"},
            {"id": 7263, "name": "Divine", "slug": "divine"},
            {"id": 7262, "name": "S.S.S.", "slug": "s-s-s-2"},
            {"id": 7270, "name": "Waterfall", "slug": "waterfall-2"},
        ]

        # Category data (from product-category.txt)
        self.categories = [
            {"id": 3120, "name": "Countertop", "slug": "countertop", "parent": 0, "count": 10},
            {"id": 3104, "name": "Exterior", "slug": "exterior", "parent": 0, "count": 20},
            {"id": 3119, "name": "Floor", "slug": "floor-exterior", "parent": 3104, "count": 5},
            {"id": 3106, "name": "Floor", "slug": "floor-interior", "parent": 3105, "count": 15},
            {"id": 3107, "name": "Floor", "slug": "floor", "parent": 0, "count": 25},
            {"id": 3105, "name": "Interior", "slug": "interior", "parent": 0, "count": 30},
            {"id": 3113, "name": "Mosaics", "slug": "mosaics", "parent": 0, "count": 12},
            {"id": 3125, "name": "New Releases", "slug": "new-releases", "parent": 0, "count": 8},
            {"id": 3117, "name": "Panels", "slug": "panels", "parent": 0, "count": 6},
            {"id": 3118, "name": "Pavers", "slug": "pavers", "parent": 0, "count": 7},
        ]

        # Tag data (minimal set for testing)
        self.tags = [
            {"id": 1001, "name": "Chip Card", "slug": "chip-card", "count": 5},
            {"id": 1002, "name": "Quick Ship", "slug": "quick-ship", "count": 10},
            {"id": 1003, "name": "Gray Tones", "slug": "gray-tones", "count": 15},
            {"id": 1004, "name": "White Tones", "slug": "white-tones", "count": 12},
            {"id": 1005, "name": "Matte Finish", "slug": "matte-finish", "count": 8},
        ]

        # Attribute data
        self.attributes = [
            {"id": 1, "name": "Tile Size", "slug": "pa_tile-size"},
            {"id": 2, "name": "Finish", "slug": "pa_finish"},
            {"id": 3, "name": "Visual", "slug": "pa_visual"},
            {"id": 4, "name": "Application", "slug": "pa_application"},
            {"id": 5, "name": "Thickness", "slug": "pa_thickness"},
        ]

        # Attribute terms
        self.attribute_terms = {
            1: [  # pa_tile-size
                {"id": 101, "name": '24"x48"', "slug": "24x48"},
                {"id": 102, "name": '12"x24"', "slug": "12x24"},
                {"id": 103, "name": '48"x48"', "slug": "48x48"},
            ],
            2: [  # pa_finish
                {"id": 201, "name": "Matte", "slug": "matte"},
                {"id": 202, "name": "Polished", "slug": "polished"},
                {"id": 203, "name": "Honed", "slug": "honed"},
            ],
            3: [  # pa_visual
                {"id": 301, "name": "Stone", "slug": "stone"},
                {"id": 302, "name": "Marble", "slug": "marble"},
                {"id": 303, "name": "Mosaic", "slug": "mosaic"},
            ],
            4: [  # pa_application
                {"id": 401, "name": "Floor", "slug": "floor"},
                {"id": 402, "name": "Wall", "slug": "wall"},
                {"id": 403, "name": "Interior", "slug": "interior"},
                {"id": 404, "name": "Exterior", "slug": "exterior"},
            ],
            5: [  # pa_thickness
                {"id": 501, "name": "10mm", "slug": "10mm"},
                {"id": 502, "name": "12mm", "slug": "12mm"},
            ],
        }

        # Build lookup dictionaries
        self._build_lookups()

    def _build_lookups(self):
        """Build lookup dictionaries from the loaded data."""
        # Product lookups
        self.product_by_name_lower = {}
        self.product_name_tokens = []
        for product in self.products:
            name = product["name"]
            self.product_by_name_lower[name.lower()] = product
            # Tokenize product names
            import re
            stop = {"tile", "tiles", "the", "a", "an", "and", "or", "of", "series"}
            for token in re.split(r'[\s\-_/]+', name.lower()):
                token = token.strip()
                if token and token not in stop and len(token) > 2:
                    self.product_name_tokens.append((token, product))

        # Category lookups
        self.category_by_id = {}
        self.category_by_name_lower = {}
        self.category_by_slug = {}
        self.category_keywords = {}
        for cat in self.categories:
            self.category_by_id[cat["id"]] = cat
            self.category_by_name_lower[cat["name"].lower()] = cat
            self.category_by_slug[cat["slug"]] = cat
            # Build category keywords
            self._generate_category_keywords(cat)

        # Tag lookups
        self.tag_by_id = {}
        self.tag_by_slug = {}
        self.tag_by_name_lower = {}
        for tag in self.tags:
            self.tag_by_id[tag["id"]] = tag
            self.tag_by_slug[tag["slug"]] = tag
            self.tag_by_name_lower[tag["name"].lower()] = tag

        # Attribute lookups
        self.attribute_by_slug = {}
        self.attribute_by_id = {}
        for attr in self.attributes:
            self.attribute_by_slug[attr["slug"]] = attr
            self.attribute_by_id[attr["id"]] = attr

    def _generate_category_keywords(self, cat_entry: Dict):
        """Generate NLP keywords from category name/slug."""
        import re
        cat_id = cat_entry["id"]
        name = cat_entry["name"].lower().strip()
        slug = cat_entry["slug"]

        # Full name
        self.category_keywords[name] = cat_id

        # Split by common separators
        stop_words = {"the", "a", "an", "and", "or", "of", "for", "in", "on", "to", "is", "all", "our", "new"}
        words = re.split(r'[\s\-_/&]+', name)
        for word in words:
            word = word.strip().lower()
            if word and word not in stop_words and len(word) > 2:
                if word not in self.category_keywords:
                    self.category_keywords[word] = cat_id

        # Slug as words
        slug_words = slug.replace("-", " ")
        if slug_words != name:
            self.category_keywords[slug_words] = cat_id

        # Add "X tiles" variations
        for suffix in ["tiles", "tile"]:
            combo = f"{name} {suffix}"
            if combo not in self.category_keywords:
                self.category_keywords[combo] = cat_id

    def get_product_for_text(self, text: str) -> Optional[Dict]:
        """Find a product by name or token in the text."""
        text_lower = text.lower()
        best_match = None
        best_match_len = 0

        # Try full name match first
        for name_lower, entry in self.product_by_name_lower.items():
            if name_lower in text_lower and len(name_lower) > best_match_len:
                best_match = entry
                best_match_len = len(name_lower)

        if best_match:
            return best_match

        # Try token matching
        for token, entry in self.product_name_tokens:
            if token in text_lower and len(token) > best_match_len:
                best_match = entry
                best_match_len = len(token)

        return best_match

    def get_category_for_text(self, text: str) -> Optional[Dict]:
        """Find a category by keyword in the text."""
        text_lower = text.lower()
        best_match = None
        best_match_len = 0

        for keyword, cat_id in sorted(
            self.category_keywords.items(),
            key=lambda x: len(x[0]),
            reverse=True,
        ):
            if keyword in text_lower and len(keyword) > best_match_len:
                cat = self.category_by_id.get(cat_id)
                if cat and cat["count"] > 0:
                    best_match = cat
                    best_match_len = len(keyword)

        return best_match

    def get_tag_ids_for_keyword(self, keyword: str) -> List[int]:
        """Find tag IDs matching a keyword."""
        import re
        needle = keyword.lower().strip()
        results = []
        seen = set()

        for name_lower, entry in self.tag_by_name_lower.items():
            if entry["id"] in seen:
                continue
            if needle in name_lower or name_lower in needle:
                results.append(entry["id"])
                seen.add(entry["id"])

        return results

    def get_attribute_term_ids(self, attr_slug: str, user_value: str) -> List[int]:
        """Find attribute term IDs matching a user value."""
        import re
        attr = self.attribute_by_slug.get(attr_slug)
        if not attr:
            return []
        
        attr_id = attr["id"]
        terms = self.attribute_terms.get(attr_id, [])
        if not terms:
            return []

        needle = user_value.lower().strip()
        needle = re.sub(r'[\"\'`]', '', needle).strip()

        exact = []
        partial = []
        for term in terms:
            term_name = term.get("name", "").lower()
            term_slug = term.get("slug", "").lower()
            term_clean = re.sub(r'[\"\'`]', '', term_name).strip()

            if term_clean == needle or term_slug == needle:
                exact.append(term["id"])
            elif needle in term_clean or term_clean in needle:
                partial.append(term["id"])
            # Match numeric parts
            elif re.sub(r'[^\dx]', '', needle) and re.sub(r'[^\dx]', '', needle) in re.sub(r'[^\dx]', '', term_clean):
                partial.append(term["id"])

        return exact if exact else partial

    def get_all_attribute_terms(self, attr_slug: str) -> List[Dict]:
        """Return all terms for an attribute slug."""
        attr = self.attribute_by_slug.get(attr_slug)
        if not attr:
            return []
        return self.attribute_terms.get(attr["id"], [])

    def get_chip_card_tag_id(self) -> Optional[int]:
        """Return the Chip Card tag ID."""
        tag = self.tag_by_slug.get("chip-card")
        return tag["id"] if tag else None

    def get_quick_ship_tag_id(self) -> Optional[int]:
        """Return the Quick Ship tag ID."""
        tag = self.tag_by_slug.get("quick-ship")
        return tag["id"] if tag else None


@pytest.fixture(scope="session", autouse=True)
def mock_store_loader():
    """
    Session-scoped fixture that provides a mock StoreLoader for all tests.
    Automatically registered with store_registry.
    """
    mock_loader = MockStoreLoader()
    set_store_loader(mock_loader)
    yield mock_loader
    # Cleanup
    set_store_loader(None)


@pytest.fixture(scope="function")
def store_loader():
    """
    Function-scoped fixture that returns the current StoreLoader.
    Useful for tests that need direct access to the loader.
    """
    return get_store_loader()
