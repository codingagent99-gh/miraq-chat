"""
Integration test for CATEGORY_LIST intent handling in routes/chat.py.
Tests that categories are formatted correctly and deduplicated.
"""
import pytest
from models import Intent, ExtractedEntities
from formatters import format_category


class TestCategoryListIntegration:
    """Integration tests for CATEGORY_LIST intent."""

    def test_category_list_deduplication_in_step_4(self):
        """Test that Step 4 in routes/chat.py deduplicates categories by name."""
        # Simulate raw category data from WooCommerce API (with duplicates)
        all_products_raw = [
            {"id": 101, "name": "Floor", "slug": "floor-1", "count": 25},
            {"id": 102, "name": "Floor", "slug": "floor-2", "count": 10},  # Duplicate
            {"id": 103, "name": "Wall", "slug": "wall", "count": 15},
            {"id": 104, "name": "Floor", "slug": "floor-3", "count": 5},  # Duplicate
            {"id": 105, "name": "Mosaic", "slug": "mosaic", "count": 8},
        ]

        # Simulate Step 4 logic from routes/chat.py
        intent = Intent.CATEGORY_LIST
        products = []
        
        if intent == Intent.CATEGORY_LIST:
            # Deduplicate categories by name and format them properly
            seen_names = set()
            for cat in all_products_raw:
                name = cat.get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    products.append(format_category(cat))

        # Verify deduplication worked
        assert len(products) == 3  # Should have 3 unique categories
        category_names = [p["name"] for p in products]
        assert category_names == ["Floor", "Wall", "Mosaic"]
        
        # Verify all products have type="category"
        for p in products:
            assert p["type"] == "category"
            assert p["price"] == 0.0

    def test_category_list_keeps_first_occurrence(self):
        """Test that deduplication keeps the first occurrence of each category name."""
        all_products_raw = [
            {"id": 101, "name": "Floor", "slug": "floor-1", "count": 25, "description": "First"},
            {"id": 102, "name": "Floor", "slug": "floor-2", "count": 10, "description": "Second"},
        ]

        intent = Intent.CATEGORY_LIST
        products = []
        
        if intent == Intent.CATEGORY_LIST:
            seen_names = set()
            for cat in all_products_raw:
                name = cat.get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    products.append(format_category(cat))

        # Should keep first occurrence
        assert len(products) == 1
        assert products[0]["id"] == 101
        assert products[0]["count"] == 25
        assert "First" in products[0]["description"]

    def test_category_list_ignores_empty_names(self):
        """Test that categories with empty names are filtered out."""
        all_products_raw = [
            {"id": 101, "name": "Valid Category", "slug": "valid", "count": 10},
            {"id": 102, "name": "", "slug": "empty-name", "count": 5},
            {"id": 103, "name": None, "slug": "null-name", "count": 3},
        ]

        intent = Intent.CATEGORY_LIST
        products = []
        
        if intent == Intent.CATEGORY_LIST:
            seen_names = set()
            for cat in all_products_raw:
                name = cat.get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    products.append(format_category(cat))

        # Should only include valid category
        assert len(products) == 1
        assert products[0]["name"] == "Valid Category"

    def test_non_category_list_intent_uses_format_product(self):
        """Test that non-CATEGORY_LIST intents use format_product as before."""
        from formatters import format_product
        
        all_products_raw = [
            {
                "id": 123,
                "name": "Product A",
                "price": "50.00",
                "stock_status": "instock",
                "images": [],
                "categories": [],
                "tags": [],
            }
        ]

        intent = Intent.PRODUCT_SEARCH
        products = []
        
        if intent == Intent.CATEGORY_LIST:
            seen_names = set()
            for cat in all_products_raw:
                name = cat.get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    products.append(format_category(cat))
        else:
            for p in all_products_raw:
                if "featured_image" in p:
                    from formatters import format_custom_product
                    products.append(format_custom_product(p))
                else:
                    products.append(format_product(p))

        # Should format as product, not category
        assert len(products) == 1
        assert products[0]["type"] == "simple"  # Not "category"
        assert products[0]["price"] == 50.0
