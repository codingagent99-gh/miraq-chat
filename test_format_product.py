"""
Test format_product() function to ensure it handles both dict and string formats.
"""
import pytest
from formatters import format_product


class TestFormatProduct:
    """Test that format_product() handles both WooCommerce and custom API formats."""

    def test_standard_woocommerce_format_with_dicts(self):
        """Test standard WooCommerce format where images, categories, tags are dicts."""
        raw_product = {
            "id": 123,
            "name": "Marble Tile",
            "slug": "marble-tile",
            "sku": "MT-001",
            "permalink": "https://example.com/marble-tile",
            "price": "50.00",
            "regular_price": "60.00",
            "sale_price": "50.00",
            "on_sale": True,
            "stock_status": "instock",
            "total_sales": 100,
            "description": "<p>Beautiful marble tile</p>",
            "short_description": "<p>Great for floors</p>",
            "images": [
                {"src": "https://example.com/image1.jpg"},
                {"src": "https://example.com/image2.jpg"},
            ],
            "categories": [
                {"name": "Stone"},
                {"name": "Marble"},
            ],
            "tags": [
                {"name": "popular"},
                {"name": "bestseller"},
            ],
            "average_rating": "4.5",
            "rating_count": 20,
            "weight": "10",
            "dimensions": {"length": "12", "width": "12", "height": "0.5"},
            "attributes": [
                {"name": "Finish", "options": ["Matte", "Glossy"], "visible": True},
            ],
            "variations": [],
            "type": "simple",
        }

        formatted = format_product(raw_product)

        assert formatted["id"] == 123
        assert formatted["name"] == "Marble Tile"
        assert formatted["images"] == ["https://example.com/image1.jpg", "https://example.com/image2.jpg"]
        assert formatted["categories"] == ["Stone", "Marble"]
        assert formatted["tags"] == ["popular", "bestseller"]
        assert formatted["price"] == 50.0
        assert formatted["in_stock"] is True

    def test_custom_api_format_with_strings(self):
        """Test custom API format where images, categories, tags are strings."""
        raw_product = {
            "id": 456,
            "name": "Granite Tile",
            "slug": "granite-tile",
            "sku": "GT-001",
            "permalink": "https://example.com/granite-tile",
            "price": "40.00",
            "regular_price": "50.00",
            "sale_price": "",
            "on_sale": False,
            "stock_status": "instock",
            "total_sales": 50,
            "description": "<p>Durable granite</p>",
            "short_description": "",
            "images": ["https://example.com/granite1.jpg", "https://example.com/granite2.jpg"],
            "categories": ["Stone", "Granite", "Floor"],
            "tags": ["new", "premium"],
            "average_rating": "4.8",
            "rating_count": 15,
            "weight": "12",
            "dimensions": {"length": "24", "width": "24", "height": "0.5"},
            "attributes": [],
            "variations": [],
            "type": "simple",
        }

        formatted = format_product(raw_product)

        assert formatted["id"] == 456
        assert formatted["name"] == "Granite Tile"
        assert formatted["images"] == ["https://example.com/granite1.jpg", "https://example.com/granite2.jpg"]
        assert formatted["categories"] == ["Stone", "Granite", "Floor"]
        assert formatted["tags"] == ["new", "premium"]
        assert formatted["price"] == 40.0
        assert formatted["sale_price"] is None

    def test_mixed_format_dicts_and_strings(self):
        """Test mixed format where some items are dicts and some are strings."""
        raw_product = {
            "id": 789,
            "name": "Mixed Format Product",
            "slug": "mixed",
            "sku": "MIX-001",
            "permalink": "https://example.com/mixed",
            "price": "30.00",
            "regular_price": "30.00",
            "sale_price": "",
            "on_sale": False,
            "stock_status": "instock",
            "total_sales": 0,
            "description": "",
            "short_description": "",
            "images": [
                {"src": "https://example.com/img1.jpg"},
                "https://example.com/img2.jpg",
                {"src": "https://example.com/img3.jpg"},
            ],
            "categories": [
                {"name": "Category1"},
                "Category2",
                {"name": "Category3"},
            ],
            "tags": [
                "tag1",
                {"name": "tag2"},
                "tag3",
            ],
            "attributes": [],
            "variations": [],
        }

        formatted = format_product(raw_product)

        assert formatted["images"] == [
            "https://example.com/img1.jpg",
            "https://example.com/img2.jpg",
            "https://example.com/img3.jpg",
        ]
        assert formatted["categories"] == ["Category1", "Category2", "Category3"]
        assert formatted["tags"] == ["tag1", "tag2", "tag3"]

    def test_empty_lists(self):
        """Test that empty lists are handled correctly."""
        raw_product = {
            "id": 999,
            "name": "Empty Lists Product",
            "slug": "empty",
            "sku": "",
            "permalink": "",
            "price": "10.00",
            "regular_price": "10.00",
            "sale_price": "",
            "on_sale": False,
            "stock_status": "instock",
            "total_sales": 0,
            "description": "",
            "short_description": "",
            "images": [],
            "categories": [],
            "tags": [],
            "attributes": [],
            "variations": [],
        }

        formatted = format_product(raw_product)

        assert formatted["images"] == []
        assert formatted["categories"] == []
        assert formatted["tags"] == []
        assert formatted["attributes"] == []

    def test_minimal_product_dict_from_step_3_6(self):
        """Test the minimal product dict injected by Step 3.6 in routes/chat.py."""
        raw_product = {
            "id": 12345,
            "name": "Last Product Context",
            "price": "",
            "regular_price": "",
            "sale_price": "",
            "slug": "",
            "sku": "",
            "permalink": "",
            "on_sale": False,
            "stock_status": "instock",
            "total_sales": 0,
            "description": "",
            "short_description": "",
            "images": [],
            "categories": [],
            "tags": [],
            "attributes": [],
            "variations": [],
        }

        formatted = format_product(raw_product)

        assert formatted["id"] == 12345
        assert formatted["name"] == "Last Product Context"
        assert formatted["images"] == []
        assert formatted["categories"] == []
        assert formatted["tags"] == []
        assert formatted["price"] == 0.0
        assert formatted["regular_price"] == 0.0
        assert formatted["sale_price"] is None

    def test_images_with_empty_src(self):
        """Test that dict images with empty src are filtered out."""
        raw_product = {
            "id": 111,
            "name": "Test Product",
            "slug": "test",
            "price": "20.00",
            "stock_status": "instock",
            "images": [
                {"src": "https://example.com/valid.jpg"},
                {"src": ""},
                {"src": "https://example.com/valid2.jpg"},
                {},
            ],
            "categories": [],
            "tags": [],
        }

        formatted = format_product(raw_product)

        assert formatted["images"] == [
            "https://example.com/valid.jpg",
            "https://example.com/valid2.jpg",
        ]

    def test_categories_with_empty_names(self):
        """Test that dict categories with empty names are filtered out."""
        raw_product = {
            "id": 222,
            "name": "Test Product",
            "slug": "test",
            "price": "20.00",
            "stock_status": "instock",
            "images": [],
            "categories": [
                {"name": "Valid Category"},
                {"name": ""},
                {"name": "Another Valid"},
                {},
            ],
            "tags": [],
        }

        formatted = format_product(raw_product)

        assert formatted["categories"] == ["Valid Category", "Another Valid"]

    def test_tags_with_empty_names(self):
        """Test that dict tags with empty names are filtered out."""
        raw_product = {
            "id": 333,
            "name": "Test Product",
            "slug": "test",
            "price": "20.00",
            "stock_status": "instock",
            "images": [],
            "categories": [],
            "tags": [
                {"name": "valid-tag"},
                {"name": ""},
                {"name": "another-tag"},
                {},
            ],
        }

        formatted = format_product(raw_product)

        assert formatted["tags"] == ["valid-tag", "another-tag"]

    def test_empty_strings_in_lists_are_filtered(self):
        """Test that empty strings in lists are filtered out."""
        raw_product = {
            "id": 444,
            "name": "Test Product",
            "slug": "test",
            "price": "20.00",
            "stock_status": "instock",
            "images": ["https://example.com/img1.jpg", "", "https://example.com/img2.jpg"],
            "categories": ["Cat1", "", "Cat2"],
            "tags": ["tag1", "", "tag2"],
        }

        formatted = format_product(raw_product)

        assert formatted["images"] == ["https://example.com/img1.jpg", "https://example.com/img2.jpg"]
        assert formatted["categories"] == ["Cat1", "Cat2"]
        assert formatted["tags"] == ["tag1", "tag2"]

    def test_attributes_non_dict_items_are_skipped(self):
        """Test that non-dict items in attributes list are skipped safely."""
        raw_product = {
            "id": 555,
            "name": "Test Product",
            "slug": "test",
            "price": "20.00",
            "stock_status": "instock",
            "images": [],
            "categories": [],
            "tags": [],
            "attributes": [
                {"name": "Finish", "options": ["Matte"], "visible": True},
                "invalid_string_attribute",
                {"name": "Size", "options": ["12x12"], "visible": True},
                None,
                {"name": "Hidden", "options": ["Value"], "visible": False},
            ],
        }

        formatted = format_product(raw_product)

        # Should only include visible dict attributes
        assert len(formatted["attributes"]) == 2
        assert formatted["attributes"][0]["name"] == "Finish"
        assert formatted["attributes"][1]["name"] == "Size"

    def test_attributes_without_visible_flag(self):
        """Test that attributes without visible=True are filtered out."""
        raw_product = {
            "id": 666,
            "name": "Test Product",
            "slug": "test",
            "price": "20.00",
            "stock_status": "instock",
            "images": [],
            "categories": [],
            "tags": [],
            "attributes": [
                {"name": "Finish", "options": ["Matte"], "visible": True},
                {"name": "NotVisible", "options": ["Value"]},  # No visible flag
                {"name": "AlsoNotVisible", "options": ["Value"], "visible": False},
            ],
        }

        formatted = format_product(raw_product)

        assert len(formatted["attributes"]) == 1
        assert formatted["attributes"][0]["name"] == "Finish"
