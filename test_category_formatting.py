"""
Test category formatting and CATEGORY_LIST intent handling.
"""
import pytest
from formatters import format_category
from response_generator import generate_bot_message
from models import Intent, ExtractedEntities


class TestFormatCategory:
    """Test that format_category() correctly maps WooCommerce category fields."""

    def test_basic_category_formatting(self):
        """Test basic category formatting with all fields."""
        raw_category = {
            "id": 101,
            "name": "Floor Tiles",
            "slug": "floor-tiles",
            "parent": 0,
            "count": 25,
            "description": "<p>High quality floor tiles</p>",
            "image": {
                "src": "https://example.com/floor-tiles.jpg",
            },
        }

        formatted = format_category(raw_category)

        assert formatted["id"] == 101
        assert formatted["name"] == "Floor Tiles"
        assert formatted["slug"] == "floor-tiles"
        assert formatted["parent"] == 0
        assert formatted["count"] == 25
        assert formatted["description"] == "High quality floor tiles"
        assert formatted["image"] == "https://example.com/floor-tiles.jpg"
        assert formatted["images"] == ["https://example.com/floor-tiles.jpg"]
        assert formatted["type"] == "category"

    def test_category_product_compatible_fields(self):
        """Test that category has product-compatible fields for schema consistency."""
        raw_category = {
            "id": 102,
            "name": "Wall Tiles",
            "slug": "wall-tiles",
            "parent": 0,
            "count": 15,
            "description": "",
            "image": None,
        }

        formatted = format_category(raw_category)

        # Product-compatible fields should exist with appropriate defaults
        assert formatted["price"] == 0.0
        assert formatted["regular_price"] == 0.0
        assert formatted["sale_price"] is None
        assert formatted["on_sale"] is False
        assert formatted["in_stock"] is True
        assert formatted["stock_status"] == ""
        assert formatted["sku"] == ""
        assert formatted["permalink"] == ""
        assert formatted["total_sales"] == 0
        assert formatted["categories"] == []
        assert formatted["tags"] == []
        assert formatted["average_rating"] == "0.00"
        assert formatted["rating_count"] == 0
        assert formatted["weight"] == ""
        assert formatted["dimensions"] == {"length": "", "width": "", "height": ""}
        assert formatted["attributes"] == []
        assert formatted["variations"] == []

    def test_category_with_no_image(self):
        """Test category formatting when image is None or empty."""
        raw_category = {
            "id": 103,
            "name": "Mosaic",
            "slug": "mosaic",
            "parent": 5,
            "count": 8,
            "description": "",
            "image": None,
        }

        formatted = format_category(raw_category)

        assert formatted["image"] == ""
        assert formatted["images"] == []

    def test_category_with_empty_image_src(self):
        """Test category formatting when image dict has empty src."""
        raw_category = {
            "id": 104,
            "name": "Trim",
            "slug": "trim",
            "parent": 0,
            "count": 3,
            "description": "",
            "image": {"src": ""},
        }

        formatted = format_category(raw_category)

        assert formatted["image"] == ""
        assert formatted["images"] == []

    def test_category_html_description_cleaned(self):
        """Test that HTML is stripped from category description."""
        raw_category = {
            "id": 105,
            "name": "Luxury Tiles",
            "slug": "luxury-tiles",
            "parent": 0,
            "count": 50,
            "description": "<p>Premium <strong>luxury</strong> tiles with <em>elegant</em> finish</p>",
            "image": None,
        }

        formatted = format_category(raw_category)

        assert "<p>" not in formatted["description"]
        assert "<strong>" not in formatted["description"]
        assert "<em>" not in formatted["description"]
        assert "Premium luxury tiles with elegant finish" == formatted["description"]
        assert formatted["short_description"] == formatted["description"]

    def test_category_with_parent(self):
        """Test category formatting with parent category."""
        raw_category = {
            "id": 106,
            "name": "Ceramic Floor",
            "slug": "ceramic-floor",
            "parent": 101,  # Parent is "Floor Tiles"
            "count": 12,
            "description": "",
            "image": None,
        }

        formatted = format_category(raw_category)

        assert formatted["parent"] == 101

    def test_category_minimal_fields(self):
        """Test category formatting with minimal required fields."""
        raw_category = {
            "id": 107,
            "name": "Basic Category",
        }

        formatted = format_category(raw_category)

        assert formatted["id"] == 107
        assert formatted["name"] == "Basic Category"
        assert formatted["slug"] == ""
        assert formatted["parent"] == 0
        assert formatted["count"] == 0
        assert formatted["description"] == ""
        assert formatted["image"] == ""


class TestCategoryListBotMessage:
    """Test that CATEGORY_LIST intent generates correct bot messages."""

    def test_category_list_message_format(self):
        """Test that category list shows product counts, not prices."""
        categories = [
            {"id": 1, "name": "Floor", "count": 25, "price": 0.0, "type": "category"},
            {"id": 2, "name": "Wall", "count": 15, "price": 0.0, "type": "category"},
            {"id": 3, "name": "Mosaic", "count": 8, "price": 0.0, "type": "category"},
        ]

        entities = ExtractedEntities()
        msg = generate_bot_message(Intent.CATEGORY_LIST, entities, categories, 0.95)

        assert "Here are our product categories! 📂" in msg
        assert "Floor" in msg
        assert "Wall" in msg
        assert "Mosaic" in msg
        assert "(25 products)" in msg
        assert "(15 products)" in msg
        assert "(8 products)" in msg
        # Should NOT show prices
        assert "$" not in msg
        assert "Contact for price" not in msg

    def test_category_list_truncation_message(self):
        """Test that category list says 'more categories' not 'more products'."""
        # Create 5 categories (more than MAX_DISPLAYED_ITEMS which is 3)
        categories = [
            {"id": i, "name": f"Category {i}", "count": 10, "price": 0.0, "type": "category"}
            for i in range(1, 6)
        ]

        entities = ExtractedEntities()
        msg = generate_bot_message(Intent.CATEGORY_LIST, entities, categories, 0.95)

        assert "more categories" in msg
        assert "more products" not in msg
        # Should show first 3 categories (MAX_DISPLAYED_ITEMS = 3)
        assert "Category 1" in msg
        assert "Category 2" in msg
        assert "Category 3" in msg
        # Should say "and 2 more categories"
        assert "2 more categories" in msg

    def test_category_with_zero_count(self):
        """Test category with zero product count shows no count."""
        categories = [
            {"id": 1, "name": "Empty Category", "count": 0, "price": 0.0, "type": "category"},
            {"id": 2, "name": "Full Category", "count": 10, "price": 0.0, "type": "category"},
        ]

        entities = ExtractedEntities()
        msg = generate_bot_message(Intent.CATEGORY_LIST, entities, categories, 0.95)

        # Category with count=0 should not show count
        assert "Empty Category" in msg
        # Should not have "(0 products)" shown
        lines = msg.split("\n")
        empty_cat_line = [line for line in lines if "Empty Category" in line][0]
        assert "(0 products)" not in empty_cat_line
        # Full category should show count
        assert "(10 products)" in msg

    def test_single_category(self):
        """Test single category doesn't use multiple products logic."""
        categories = [
            {"id": 1, "name": "Single Category", "count": 20, "price": 0.0, "type": "category"},
        ]

        entities = ExtractedEntities()
        msg = generate_bot_message(Intent.CATEGORY_LIST, entities, categories, 0.95)

        # For single item, the function returns early with single product message
        # This should show a single product format, not category list format
        assert "Single Category" in msg
