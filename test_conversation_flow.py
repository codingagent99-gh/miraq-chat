"""
Integration tests for conversation flow and classifier entity extraction.

Tests run against the REAL hosted server at localhost:5009 and use
REAL StoreLoader data from WooCommerce — no mocks.

Two test modes:
  1. Classifier tests  — call classify() directly with live StoreLoader data
  2. Server tests       — POST to the real /chat endpoint and validate responses
"""

import pytest
import requests
from classifier import classify
from models import Intent, ExtractedEntities


# ─── Server URL ───
SERVER_URL = "http://localhost:5009"


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def chat(message: str, session_id: str = "test-session") -> dict:
    """Send a message to the real /chat endpoint and return the JSON response."""
    resp = requests.post(
        f"{SERVER_URL}/chat",
        json={"message": message, "session_id": session_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════
#  A. CLASSIFIER TESTS (with live StoreLoader)
# ═══════════════════════════════════════════════════════════════

class TestProductAndCategorySearch:
    """Test product and category search functionality."""

    def test_search_specific_product_allspice(self):
        """Test searching for Allspice product."""
        result = classify("show me Allspice")
        assert result.intent == Intent.PRODUCT_SEARCH
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Allspice"
        assert result.entities.product_id is not None

    def test_search_specific_product_ansel(self):
        """Test searching for Ansel product."""
        result = classify("I want to see Ansel")
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Ansel"
        assert result.entities.product_id is not None

    def test_search_specific_product_waterfall(self):
        """Test searching for Waterfall product."""
        result = classify("show me Waterfall")
        assert result.intent == Intent.PRODUCT_SEARCH
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Waterfall"
        assert result.entities.product_id is not None

    def test_search_category_floor_tiles(self):
        """Test searching for floor tiles category."""
        result = classify("show me floor tiles")
        assert result.intent in [Intent.FILTER_BY_APPLICATION, Intent.CATEGORY_BROWSE]
        assert result.entities.category_id is not None


class TestOrderQuantityFlow:
    """Test order flows with quantity extraction."""

    def test_order_with_quantity_inline(self):
        """Test ordering a product with inline quantity."""
        result = classify("order for 5 Allspice")
        assert result.intent == Intent.QUICK_ORDER
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Allspice"
        assert result.entities.quantity is not None
        assert result.entities.quantity == 5

    def test_order_without_quantity_triggers_prompt(self):
        """Test ordering a product without quantity."""
        result = classify("I want to order Divine")
        assert result.intent == Intent.QUICK_ORDER
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Divine"


class TestFullConversationScenario:
    """Test full conversation scenarios."""

    def test_step_6_user_orders_with_quantity(self):
        """Test user ordering a product with quantity in conversation."""
        result = classify("order for 3 Cord")
        assert result.intent == Intent.QUICK_ORDER
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Cord"
        assert result.entities.quantity is not None
        assert result.entities.quantity == 3


class TestFreshLoginFlow:
    """Test fresh user login flows."""

    def test_fresh_user_directly_asks_product(self):
        """Test fresh user asking for a product directly."""
        result = classify("show me Ansel Mosaic")
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Ansel Mosaic"

    def test_fresh_user_orders_directly_with_customer(self):
        """Test fresh user ordering directly."""
        result = classify("I want to order S.S.S.")
        assert result.intent == Intent.QUICK_ORDER
        assert result.entities.product_name is not None
        assert result.entities.product_name == "S.S.S."


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_classifier_extracts_product_from_different_products(self):
        """Test classifier can extract different product names."""
        result = classify("show me Cairo Mosaic")
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Cairo Mosaic"

        result = classify("I need Divine")
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Divine"

        result = classify("what is Cord")
        assert result.entities.product_name is not None
        assert result.entities.product_name == "Cord"


class TestProductDetailsAndVariations:
    """Test product detail queries."""

    def test_product_detail_allspice(self):
        result = classify("tell me about Allspice")
        assert result.intent == Intent.PRODUCT_DETAIL
        assert result.entities.product_name == "Allspice"

    def test_product_detail_waterfall(self):
        result = classify("what are the specs for Waterfall")
        assert result.intent == Intent.PRODUCT_DETAIL
        assert result.entities.product_name == "Waterfall"


class TestCategoryBrowsing:
    """Test category browsing functionality."""

    def test_browse_mosaics_category(self):
        result = classify("show me mosaics")
        assert result.intent in [Intent.MOSAIC_PRODUCTS, Intent.CATEGORY_BROWSE]
        assert result.entities.category_name is not None

    def test_browse_countertop_category(self):
        result = classify("show countertop tiles")
        assert result.entities.category_name is not None

    def test_browse_interior_category(self):
        result = classify("interior tiles")
        assert result.entities.category_name is not None


class TestAttributeFiltering:
    """Test attribute-based filtering."""

    def test_filter_by_matte_finish(self):
        result = classify("show me matte tiles")
        assert result.intent in [Intent.FILTER_BY_FINISH, Intent.CATEGORY_BROWSE]
        assert result.entities.finish == "Matte"

    def test_filter_by_polished_finish(self):
        result = classify("show polished tiles")
        assert result.intent in [Intent.FILTER_BY_FINISH, Intent.CATEGORY_BROWSE]
        assert result.entities.finish == "Polished"

    def test_filter_by_size_24x48(self):
        result = classify("show me 24x48 tiles")
        assert result.intent in [Intent.FILTER_BY_SIZE, Intent.CATEGORY_BROWSE]
        assert result.entities.tile_size is not None


class TestOrderFlows:
    """Test various order flows."""

    def test_order_allspice_5_units(self):
        result = classify("order 5 Allspice")
        assert result.intent == Intent.QUICK_ORDER
        assert result.entities.product_name == "Allspice"
        assert result.entities.quantity == 5

    def test_order_ansel_no_quantity(self):
        result = classify("I want Ansel")
        assert result.entities.product_name == "Ansel"

    def test_order_divine_3_boxes(self):
        result = classify("order 3 boxes Divine")
        assert result.entities.product_name == "Divine"
        assert result.entities.quantity == 3

    def test_order_waterfall_inline(self):
        result = classify("buy 2 Waterfall")
        assert result.entities.product_name == "Waterfall"
        assert result.entities.quantity == 2


class TestProductSearch:
    """Test product search variations."""

    def test_search_cord(self):
        result = classify("find Cord")
        assert result.entities.product_name == "Cord"

    def test_search_sss(self):
        result = classify("show S.S.S.")
        assert result.entities.product_name == "S.S.S."

    def test_search_cairo_mosaic(self):
        result = classify("I'm looking for Cairo Mosaic")
        assert result.entities.product_name == "Cairo Mosaic"

    def test_search_ansel_mosaic(self):
        result = classify("show Ansel Mosaic")
        assert result.entities.product_name == "Ansel Mosaic"


class TestMiscellaneous:
    """Miscellaneous tests for coverage."""

    def test_quick_ship_inquiry(self):
        result = classify("what's in stock")
        assert result.intent == Intent.PRODUCT_QUICK_SHIP
        assert result.entities.quick_ship is True

    def test_sample_request(self):
        result = classify("can I get a sample")
        assert result.intent == Intent.SAMPLE_REQUEST

    def test_discount_inquiry(self):
        result = classify("any discount available")
        assert result.intent == Intent.DISCOUNT_INQUIRY

    def test_mosaic_products(self):
        result = classify("show me mosaics")
        assert result.intent in [Intent.MOSAIC_PRODUCTS, Intent.CATEGORY_BROWSE]

    def test_product_list_generic(self):
        result = classify("show me all tiles")
        assert result.intent in [Intent.PRODUCT_LIST, Intent.CATEGORY_BROWSE]

    def test_product_catalog(self):
        result = classify("show me your catalog")
        assert result.intent == Intent.PRODUCT_CATALOG

    def test_product_types(self):
        result = classify("what types of tiles do you have")
        assert result.intent in [Intent.PRODUCT_TYPES, Intent.CATEGORY_BROWSE]


class TestQuantityExtraction:
    """Test quantity extraction from various patterns."""

    def test_quantity_with_qty_keyword(self):
        result = classify("order 10 qty Allspice")
        assert result.entities.quantity == 10

    def test_quantity_with_pieces(self):
        result = classify("buy 25 pieces")
        assert result.entities.quantity == 25

    def test_quantity_with_boxes(self):
        result = classify("I need 5 boxes")
        assert result.entities.quantity == 5

    def test_quantity_of_this(self):
        result = classify("I want 7 of these")
        assert result.entities.quantity == 7


class TestColorFiltering:
    """Test color-based filtering."""

    def test_filter_gray_tiles(self):
        result = classify("show me gray tiles")
        assert result.entities.color_tone is not None

    def test_filter_white_tiles(self):
        result = classify("show white tiles")
        assert result.entities.color_tone is not None


class TestApplicationFiltering:
    """Test application-based filtering."""

    def test_filter_floor_application(self):
        result = classify("tiles for floor")
        assert result.intent in [Intent.FILTER_BY_APPLICATION, Intent.CATEGORY_BROWSE]
        assert result.entities.application is not None or result.entities.category_name is not None

    def test_filter_wall_application(self):
        result = classify("tiles for wall")
        assert result.entities.application is not None or result.entities.category_name is not None

    def test_filter_interior_application(self):
        result = classify("interior tiles")
        assert result.entities.application is not None or result.entities.category_name is not None


class TestOrderHistory:
    """Test order history related queries."""

    def test_last_order(self):
        result = classify("what was my last order")
        assert result.intent == Intent.LAST_ORDER

    def test_order_history(self):
        result = classify("show my order history")
        assert result.intent == Intent.ORDER_HISTORY

    def test_reorder(self):
        result = classify("reorder")
        assert result.intent == Intent.REORDER


class TestRelatedProducts:
    """Test related products queries."""

    def test_related_products_query(self):
        result = classify("what goes with this")
        assert result.intent == Intent.RELATED_PRODUCTS

    def test_similar_products(self):
        result = classify("show me similar tiles")
        assert result.intent == Intent.RELATED_PRODUCTS


# ═══════════════════════════════════════════════════════════════
#  B. SERVER INTEGRATION TESTS (POST to localhost:5009/chat)
# ═══════════════════════════════════════════════════════════════

class TestServerProductSearch:
    """Test product search via the real /chat endpoint."""

    def test_server_search_allspice(self):
        """Server should return Allspice product data."""
        data = chat("show me Allspice")
        assert data["success"] is True
        assert data["intent"] in ["search", "detail", "browse"]
        assert "products" in data

    def test_server_search_waterfall(self):
        data = chat("show me Waterfall")
        assert data["success"] is True
        assert "products" in data

    def test_server_search_divine(self):
        data = chat("I need Divine")
        assert data["success"] is True
        assert "bot_message" in data


class TestServerCategoryBrowse:
    """Test category browsing via the real /chat endpoint."""

    def test_server_browse_mosaics(self):
        data = chat("show me mosaics")
        assert data["success"] is True
        assert "products" in data

    def test_server_browse_interior(self):
        data = chat("show me interior tiles")
        assert data["success"] is True
        assert "bot_message" in data

    def test_server_browse_countertop(self):
        data = chat("show countertop tiles")
        assert data["success"] is True


class TestServerOrderFlow:
    """Test order flow via the real /chat endpoint."""

    def test_server_order_with_quantity(self):
        """Server should recognize order intent with quantity."""
        data = chat("order for 5 Allspice", session_id="order-test-1")
        assert data["success"] is True
        assert data["intent"] in ["order", "quick_order"]

    def test_server_order_without_quantity(self):
        """Server should prompt for quantity when missing."""
        data = chat("I want to order Divine", session_id="order-test-2")
        assert data["success"] is True
        assert "bot_message" in data


class TestServerAttributeFilter:
    """Test attribute filtering via the real /chat endpoint."""

    def test_server_filter_matte(self):
        data = chat("show me matte tiles")
        assert data["success"] is True
        assert "products" in data

    def test_server_filter_polished(self):
        data = chat("show polished tiles")
        assert data["success"] is True

    def test_server_filter_size(self):
        data = chat("show me 24x48 tiles")
        assert data["success"] is True


class TestServerMiscellaneous:
    """Test miscellaneous intents via the real /chat endpoint."""

    def test_server_quick_ship(self):
        data = chat("what's in stock")
        assert data["success"] is True

    def test_server_sample_request(self):
        data = chat("can I get a sample")
        assert data["success"] is True

    def test_server_discount(self):
        data = chat("any discount available")
        assert data["success"] is True

    def test_server_catalog(self):
        data = chat("show me your catalog")
        assert data["success"] is True

    def test_server_order_history(self):
        data = chat("show my order history")
        assert data["success"] is True

    def test_server_reorder(self):
        data = chat("reorder")
        assert data["success"] is True


class TestServerResponseStructure:
    """Verify that all server responses have the expected JSON structure."""

    @pytest.mark.parametrize("message", [
        "show me Allspice",
        "show me mosaics",
        "order 5 Allspice",
        "what's in stock",
        "can I get a sample",
        "any discount available",
        "show me matte tiles",
        "show me gray tiles",
        "tiles for floor",
        "what was my last order",
    ])
    def test_server_response_has_required_fields(self, message):
        """Every /chat response must include these top-level keys."""
        data = chat(message)
        assert "success" in data
        assert "bot_message" in data
        assert "intent" in data
        assert "session_id" in data


class TestServerConversationSession:
    """Test that session state is maintained across multiple messages."""

    def test_multi_turn_session(self):
        """Send multiple messages on the same session and verify continuity."""
        sid = "integration-multi-turn"

        r1 = chat("show me Allspice", session_id=sid)
        assert r1["success"] is True
        assert r1["session_id"] == sid

        r2 = chat("tell me more about it", session_id=sid)
        assert r2["success"] is True
        assert r2["session_id"] == sid

        r3 = chat("order 3 of this", session_id=sid)
        assert r3["success"] is True
        assert r3["session_id"] == sid
