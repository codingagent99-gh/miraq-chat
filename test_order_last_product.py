"""
Test order placement using last_product_ctx to ensure product info is correctly displayed.
"""
import pytest
from unittest.mock import Mock, patch
from models import Intent, ExtractedEntities
from server import generate_bot_message


class TestOrderLastProduct:
    """Test order placement with last_product context."""

    def test_generate_bot_message_with_products_array(self):
        """Test that product name is correctly extracted from products array."""
        intent = Intent.QUICK_ORDER
        entities = ExtractedEntities()
        products = [{"name": "Zelda Mosaic", "price": 45.99}]
        confidence = 0.95
        order_data = [{
            "id": 12345,
            "number": "12345",
            "total": "45.99",
            "line_items": [{"name": "Zelda Mosaic", "total": "45.99", "quantity": 1}]
        }]
        
        message = generate_bot_message(intent, entities, products, confidence, order_data)
        
        assert "Zelda Mosaic" in message
        assert "your item" not in message
        assert "$45.99" in message

    def test_generate_bot_message_fallback_to_line_items(self):
        """Test that product name falls back to line_items when products is empty."""
        intent = Intent.QUICK_ORDER
        entities = ExtractedEntities()
        products = []  # Empty products array (the bug scenario)
        confidence = 0.95
        order_data = [{
            "id": 12345,
            "number": "12345",
            "total": "45.99",
            "line_items": [{"name": "Zelda Mosaic", "total": "45.99", "quantity": 1}]
        }]
        
        message = generate_bot_message(intent, entities, products, confidence, order_data)
        
        # Should extract name from line_items
        assert "Zelda Mosaic" in message
        assert "your item" not in message

    def test_generate_bot_message_uses_line_item_total_when_order_total_is_zero(self):
        """Test that order total falls back to line_items total when order total is 0."""
        intent = Intent.PLACE_ORDER
        entities = ExtractedEntities()
        products = [{"name": "Zelda Mosaic", "price": 45.99}]
        confidence = 0.95
        order_data = [{
            "id": 12345,
            "number": "12345",
            "total": "0.00",  # Order total is 0
            "line_items": [{"name": "Zelda Mosaic", "total": "45.99", "quantity": 1}]
        }]
        
        message = generate_bot_message(intent, entities, products, confidence, order_data)
        
        # Should use line_items total instead of 0.00
        assert "$45.99" in message
        assert "$0.00" not in message

    def test_generate_bot_message_ultimate_fallback_to_your_item(self):
        """Test that 'your item' is used when both products and line_items are empty."""
        intent = Intent.ORDER_ITEM
        entities = ExtractedEntities()
        products = []
        confidence = 0.95
        order_data = [{
            "id": 12345,
            "number": "12345",
            "total": "0.00",
            "line_items": []  # No line items
        }]
        
        message = generate_bot_message(intent, entities, products, confidence, order_data)
        
        # Should fall back to "your item"
        assert "your item" in message

    def test_generate_bot_message_handles_line_items_without_name(self):
        """Test that fallback to 'your item' works when line_items exist but have no name."""
        intent = Intent.QUICK_ORDER
        entities = ExtractedEntities()
        products = []
        confidence = 0.95
        order_data = [{
            "id": 12345,
            "number": "12345",
            "total": "45.99",
            "line_items": [{"total": "45.99", "quantity": 1}]  # No name field
        }]
        
        message = generate_bot_message(intent, entities, products, confidence, order_data)
        
        # Should fall back to "your item" when line_item has no name
        assert "your item" in message

    def test_generate_bot_message_with_multiple_line_items(self):
        """Test that total is correctly calculated from multiple line items."""
        intent = Intent.QUICK_ORDER
        entities = ExtractedEntities()
        products = []
        confidence = 0.95
        order_data = [{
            "id": 12345,
            "number": "12345",
            "total": "0.00",
            "line_items": [
                {"name": "Zelda Mosaic", "total": "45.99", "quantity": 2},
                {"name": "Stone Tile", "total": "30.00", "quantity": 1}
            ]
        }]
        
        message = generate_bot_message(intent, entities, products, confidence, order_data)
        
        # Should sum line item totals: 45.99 + 30.00 = 75.99
        assert "$75.99" in message
        assert "$0.00" not in message
        # Should use first line item name
        assert "Zelda Mosaic" in message

    def test_generate_bot_message_order_item_intent(self):
        """Test ORDER_ITEM intent with product from last_product_ctx."""
        intent = Intent.ORDER_ITEM
        entities = ExtractedEntities()
        products = [{"name": "Marble Tile XL", "price": 89.50}]
        confidence = 0.92
        order_data = [{
            "id": 67890,
            "number": "67890",
            "total": "89.50",
            "line_items": [{"name": "Marble Tile XL", "total": "89.50", "quantity": 1}]
        }]
        
        message = generate_bot_message(intent, entities, products, confidence, order_data)
        
        assert "Marble Tile XL" in message
        assert "$89.50" in message
        assert "Order #67890 placed successfully" in message

    def test_generate_bot_message_place_order_intent(self):
        """Test PLACE_ORDER intent with product from last_product_ctx."""
        intent = Intent.PLACE_ORDER
        entities = ExtractedEntities()
        products = [{"name": "Ceramic Floor Tile", "price": 25.00}]
        confidence = 0.88
        order_data = [{
            "id": 11111,
            "number": "11111",
            "total": "25.00",
            "line_items": [{"name": "Ceramic Floor Tile", "total": "25.00", "quantity": 1}]
        }]
        
        message = generate_bot_message(intent, entities, products, confidence, order_data)
        
        assert "Ceramic Floor Tile" in message
        assert "$25.00" in message
        assert "Processing" in message
        assert "Cash on Delivery" in message
