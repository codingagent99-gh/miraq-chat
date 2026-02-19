"""
Test custom API response formatting.
"""
import pytest
from formatters import format_custom_product


class TestCustomProductFormatter:
    """Test that custom API products are formatted correctly."""

    def test_format_custom_product_basic(self):
        """Test basic custom product formatting."""
        raw_product = {
            "id": 7275,
            "name": "Ansel",
            "slug": "ansel-2",
            "description": "<p>Beautiful tile</p>",
            "short_description": "",
            "sku": "ansel",
            "price": "30",
            "regular_price": "",
            "sale_price": "",
            "stock_status": "instock",
            "stock_quantity": None,
            "manage_stock": False,
            "images": ["https://example.com/image1.jpg", "https://example.com/image2.jpg"],
            "featured_image": "https://example.com/featured.jpg",
            "attributes": {
                "pa_visual": {},
                "pa_pricing": {},
                "pa_colors": {},
            },
            "categories": ["Exterior", "Floor", "Tile", "Wall"],
            "permalink": "https://wgc.net.in/hn/product/ansel-2/"
        }

        formatted = format_custom_product(raw_product)

        assert formatted["id"] == 7275
        assert formatted["name"] == "Ansel"
        assert formatted["slug"] == "ansel-2"
        assert formatted["sku"] == "ansel"
        assert formatted["price"] == 30.0
        assert formatted["regular_price"] == 0.0
        assert formatted["sale_price"] is None
        assert formatted["on_sale"] is False
        assert formatted["in_stock"] is True
        assert formatted["stock_status"] == "instock"
        assert formatted["permalink"] == "https://wgc.net.in/hn/product/ansel-2/"
        assert formatted["images"] == ["https://example.com/image1.jpg", "https://example.com/image2.jpg"]
        assert formatted["categories"] == ["Exterior", "Floor", "Tile", "Wall"]
        assert formatted["description"] == "Beautiful tile"
        assert formatted["type"] == "simple"

    def test_format_custom_product_with_sale_price(self):
        """Test custom product with sale price."""
        raw_product = {
            "id": 123,
            "name": "Test Product",
            "slug": "test-product",
            "description": "",
            "short_description": "",
            "sku": "test-123",
            "price": "25.50",
            "regular_price": "30.00",
            "sale_price": "25.50",
            "stock_status": "instock",
            "images": [],
            "featured_image": "",
            "attributes": {},
            "categories": ["Test"],
            "permalink": "https://example.com/product/test-product/"
        }

        formatted = format_custom_product(raw_product)

        assert formatted["price"] == 25.50
        assert formatted["regular_price"] == 30.00
        assert formatted["sale_price"] == 25.50
        assert formatted["on_sale"] is True  # Derived from sale_price being non-empty

    def test_format_custom_product_out_of_stock(self):
        """Test custom product that's out of stock."""
        raw_product = {
            "id": 456,
            "name": "Out of Stock",
            "slug": "out-of-stock",
            "description": "",
            "short_description": "",
            "sku": "oos-123",
            "price": "50",
            "regular_price": "",
            "sale_price": "",
            "stock_status": "outofstock",
            "images": [],
            "featured_image": "",
            "attributes": {},
            "categories": [],
            "permalink": "https://example.com/product/out-of-stock/"
        }

        formatted = format_custom_product(raw_product)

        assert formatted["in_stock"] is False
        assert formatted["stock_status"] == "outofstock"

    def test_format_custom_product_attributes_conversion(self):
        """Test that attributes dict is converted to list format."""
        raw_product = {
            "id": 789,
            "name": "Attribute Test",
            "slug": "attribute-test",
            "description": "",
            "short_description": "",
            "sku": "attr-123",
            "price": "40",
            "regular_price": "",
            "sale_price": "",
            "stock_status": "instock",
            "images": [],
            "featured_image": "",
            "attributes": {
                "pa_finish": {"options": ["Matte", "Glossy"]},
                "pa_tile-size": {"options": ["12x12", "24x24"]},
            },
            "categories": [],
            "permalink": "https://example.com/product/attribute-test/"
        }

        formatted = format_custom_product(raw_product)

        # Attributes should be converted from dict to list
        assert isinstance(formatted["attributes"], list)
        assert len(formatted["attributes"]) == 2
        
        # Check that attribute names are converted from slugs
        attr_names = [attr["name"] for attr in formatted["attributes"]]
        assert "Finish" in attr_names
        assert "Tile Size" in attr_names

    def test_format_custom_product_defaults_for_missing_fields(self):
        """Test that missing fields get sensible defaults."""
        raw_product = {
            "id": 999,
            "name": "Minimal Product",
            "slug": "minimal",
            "sku": "",
            "price": "10",
            "stock_status": "instock",
            "featured_image": "",
            "categories": [],
            "permalink": ""
        }

        formatted = format_custom_product(raw_product)

        # Check defaults for fields not provided by custom API
        assert formatted["tags"] == []
        assert formatted["average_rating"] == "0.00"
        assert formatted["rating_count"] == 0
        assert formatted["weight"] == ""
        assert formatted["dimensions"] == {"length": "", "width": "", "height": ""}
        assert formatted["variations"] == []
        assert formatted["total_sales"] == 0
