"""
Tests for variable product ordering flow.

Validates:
1. FlowState.AWAITING_VARIANT_SELECTION is defined
2. ConversationContext has pending_variation_id
3. handle_flow_state() handles AWAITING_VARIANT_SELECTION (cancel vs pass-through)
4. _build_variant_prompt() helper builds correct prompt
5. Step 3.6 logic: variable vs simple product detection
6. Variations are separated from parent products in all_products_raw
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from models import Intent, ExtractedEntities
from conversation_flow import FlowState, ConversationContext, handle_flow_state


# ─────────────────────────────────────────────────────────────────────────────
# 1. FlowState enum includes AWAITING_VARIANT_SELECTION
# ─────────────────────────────────────────────────────────────────────────────

class TestFlowStateEnum:
    def test_awaiting_variant_selection_exists(self):
        """AWAITING_VARIANT_SELECTION must be in FlowState enum."""
        assert hasattr(FlowState, "AWAITING_VARIANT_SELECTION")
        assert FlowState.AWAITING_VARIANT_SELECTION.value == "awaiting_variant_selection"

    def test_all_expected_states_present(self):
        """Ensure no previously existing states were removed."""
        expected = [
            "idle", "awaiting_intent_choice", "awaiting_product_or_category",
            "showing_results", "awaiting_quantity", "awaiting_order_confirm",
            "awaiting_shipping_confirm", "awaiting_new_address",
            "awaiting_address_confirm", "order_complete",
            "awaiting_anything_else", "closing", "awaiting_variant_selection",
        ]
        actual_values = {s.value for s in FlowState}
        for state in expected:
            assert state in actual_values, f"Missing FlowState: {state}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. ConversationContext has pending_variation_id
# ─────────────────────────────────────────────────────────────────────────────

class TestConversationContext:
    def test_pending_variation_id_defaults_to_none(self):
        ctx = ConversationContext()
        assert ctx.pending_variation_id is None

    def test_pending_variation_id_can_be_set(self):
        ctx = ConversationContext(pending_variation_id=12345)
        assert ctx.pending_variation_id == 12345


# ─────────────────────────────────────────────────────────────────────────────
# 3. handle_flow_state() for AWAITING_VARIANT_SELECTION
# ─────────────────────────────────────────────────────────────────────────────

class TestHandleFlowStateVariantSelection:
    def _call(self, message):
        return handle_flow_state(
            state=FlowState.AWAITING_VARIANT_SELECTION,
            message=message,
            entities={},
            confidence=0.9,
        )

    def test_cancel_exits_flow(self):
        result = self._call("cancel")
        assert result is not None
        assert result.get("pass_through") is not True
        assert result.get("flow_state") == FlowState.AWAITING_ANYTHING_ELSE.value
        assert "bot_message" in result

    def test_stop_exits_flow(self):
        result = self._call("stop")
        assert result is not None
        assert result.get("flow_state") == FlowState.AWAITING_ANYTHING_ELSE.value

    def test_variant_description_passes_through(self):
        """Any non-cancel message should pass through to the classifier pipeline."""
        for phrase in ["True White Matte", "Polished 6x6", "Charcoal Honed 12x24"]:
            result = self._call(phrase)
            assert result is not None, f"Expected result for phrase: {phrase}"
            assert result.get("pass_through") is True, f"Expected pass_through for: {phrase}"
            assert result.get("flow_state") == FlowState.AWAITING_VARIANT_SELECTION.value

    def test_cancellation_phrases(self):
        for phrase in ["cancel", "stop", "nevermind", "never mind"]:
            result = self._call(phrase)
            assert result is not None
            assert result.get("pass_through") is not True, f"Should not pass_through on: {phrase}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. _build_variant_prompt helper
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildVariantPrompt:
    def _import_fn(self):
        from routes.chat import _build_variant_prompt
        return _build_variant_prompt

    def test_prompt_with_variation_attributes(self):
        build_prompt = self._import_fn()
        product_raw = {
            "attributes": [
                {"name": "Colors", "variation": True, "options": ["True White", "Charcoal", "Smoke"]},
                {"name": "Finish", "variation": True, "options": ["Matte", "Polished"]},
                {"name": "Sample Size", "variation": True, "options": ['6"x6"', '12"x12"']},
                {"name": "Shade", "variation": False, "options": ["Light"]},  # non-variation attr
            ]
        }
        msg = build_prompt(product_raw, "Ansel")
        assert "Ansel" in msg
        assert "Colors" in msg
        assert "Finish" in msg
        assert "Sample Size" in msg
        assert "True White" in msg
        assert "Matte" in msg
        # Non-variation attribute should not appear
        assert "Shade" not in msg

    def test_prompt_without_variation_attributes(self):
        build_prompt = self._import_fn()
        product_raw = {"attributes": []}
        msg = build_prompt(product_raw, "Waterfall")
        assert "Waterfall" in msg
        assert "variant" in msg.lower() or "option" in msg.lower()

    def test_prompt_for_missing_attributes_key(self):
        build_prompt = self._import_fn()
        product_raw = {}
        msg = build_prompt(product_raw, "Allspice")
        assert "Allspice" in msg


# ─────────────────────────────────────────────────────────────────────────────
# 5. Step 3.6 variable product detection — unit tests via Flask test client
# ─────────────────────────────────────────────────────────────────────────────

class TestStep36VariableProductDetection:
    """
    Test Step 3.6 directly using the Flask test client with mocked woo_client.
    These tests validate the variable product routing logic without real API calls.
    """

    def _make_request(self, client, message, user_context=None):
        body = {
            "message": message,
            "session_id": "test-variable-product",
            "user_context": user_context or {"customer_id": 130},
        }
        return client.post("/chat", json=body)

    def test_simple_product_order_does_not_ask_for_variant(self):
        """A simple product order should not trigger variant selection."""
        from server import app

        # Mock the woo_client to return a simple product and a successful order
        simple_product = {
            "id": 7266, "name": "Lager", "type": "simple",
            "price": "10.00", "regular_price": "10.00", "sale_price": "",
            "slug": "lager", "sku": "", "permalink": "",
            "on_sale": False, "stock_status": "instock", "total_sales": 0,
            "description": "", "short_description": "", "images": [],
            "categories": [], "tags": [], "attributes": [], "variations": [],
            "average_rating": "0.00", "rating_count": 0, "weight": "",
            "dimensions": {"length": "", "width": "", "height": ""},
        }
        created_order = {
            "id": 1001, "number": "1001", "total": "10.00",
            "currency_symbol": "$", "line_items": [{"name": "Lager", "quantity": 3, "total": "30.00"}],
        }

        with app.test_client() as client:
            with patch("routes.chat.woo_client") as mock_woo:
                mock_woo.execute_all.return_value = [
                    {"success": True, "data": [simple_product], "total": "1", "total_pages": "1"}
                ]
                mock_woo.execute.return_value = {"success": True, "data": created_order}

                resp = self._make_request(client, "order 3 Lager")
                data = resp.get_json()

                # Should NOT set AWAITING_VARIANT_SELECTION
                flow = data.get("flow_state") or data.get("metadata", {}).get("flow_state", "")
                assert flow != FlowState.AWAITING_VARIANT_SELECTION.value, \
                    "Simple product should not trigger variant selection"

    def test_variable_product_no_attrs_asks_for_variant(self):
        """A variable product ordered without attributes should trigger variant selection."""
        from server import app

        variable_product = {
            "id": 7275, "name": "Ansel", "type": "variable",
            "price": "15.00", "regular_price": "15.00", "sale_price": "",
            "slug": "ansel", "sku": "", "permalink": "",
            "on_sale": False, "stock_status": "instock", "total_sales": 0,
            "description": "", "short_description": "", "images": [],
            "categories": [], "tags": [],
            "attributes": [
                {"name": "Colors", "variation": True, "visible": True,
                 "options": ["ANSEL True White", "ANSEL Charcoal", "ANSEL Smoke"]},
                {"name": "Finish", "variation": True, "visible": True,
                 "options": ["Matte", "Polished"]},
                {"name": "Sample Size", "variation": True, "visible": True,
                 "options": ['6"x6"', '12"x12"']},
            ],
            "variations": [1001, 1002, 1003],
            "average_rating": "0.00", "rating_count": 0, "weight": "",
            "dimensions": {"length": "", "width": "", "height": ""},
        }

        with app.test_client() as client:
            with patch("routes.chat.woo_client") as mock_woo:
                mock_woo.execute_all.return_value = [
                    {"success": True, "data": [variable_product], "total": "1", "total_pages": "1"}
                ]
                # execute() should not be called for order creation

                resp = self._make_request(client, "order 5 Ansel")
                data = resp.get_json()

                # Should set AWAITING_VARIANT_SELECTION
                flow = data.get("flow_state") or data.get("metadata", {}).get("flow_state", "")
                assert flow == FlowState.AWAITING_VARIANT_SELECTION.value, \
                    f"Variable product should trigger variant selection, got flow_state={flow}"

                # Should include pending context for next turn
                meta = data.get("metadata", {})
                assert meta.get("pending_product_id") == 7275
                assert meta.get("pending_product_name") == "Ansel"
                assert meta.get("pending_quantity") == 5

                # Bot message should mention the product and available options
                bot_msg = data.get("bot_message", "")
                assert "Ansel" in bot_msg


# ─────────────────────────────────────────────────────────────────────────────
# 6. Pre-fetched variations are separated from parent products
# ─────────────────────────────────────────────────────────────────────────────

class TestVariationSeparation:
    """Ensure variations (parent_id set) are excluded from the formatted product list."""

    def test_variations_excluded_from_product_list(self):
        """Variations returned by api_builder should not appear as standalone products."""
        from server import app

        parent_product = {
            "id": 7275, "name": "Ansel", "type": "variable",
            "price": "", "regular_price": "", "sale_price": "", "slug": "ansel",
            "sku": "", "permalink": "", "on_sale": False, "stock_status": "instock",
            "total_sales": 0, "description": "", "short_description": "", "images": [],
            "categories": [], "tags": [], "attributes": [], "variations": [8001, 8002],
            "average_rating": "0.00", "rating_count": 0, "weight": "",
            "dimensions": {"length": "", "width": "", "height": ""},
        }
        variation_1 = {
            "id": 8001, "parent_id": 7275, "name": "Ansel — True White / Matte",
            "attributes": [{"name": "Colors", "option": "True White"}, {"name": "Finish", "option": "Matte"}],
            "price": "15.00", "regular_price": "15.00", "sale_price": "", "sku": "",
            "stock_status": "instock", "on_sale": False, "slug": "", "image": {},
        }
        variation_2 = {
            "id": 8002, "parent_id": 7275, "name": "Ansel — Charcoal / Polished",
            "attributes": [{"name": "Colors", "option": "Charcoal"}, {"name": "Finish", "option": "Polished"}],
            "price": "15.00", "regular_price": "15.00", "sale_price": "", "sku": "",
            "stock_status": "instock", "on_sale": False, "slug": "", "image": {},
        }

        with app.test_client() as client:
            with patch("routes.chat.woo_client") as mock_woo:
                mock_woo.execute_all.return_value = [
                    {"success": True, "data": parent_product},
                    {"success": True, "data": [variation_1, variation_2]},
                ]
                mock_woo.execute.return_value = {"success": True, "data": {"id": 1001, "total": "15.00", "number": "1001", "currency_symbol": "$", "line_items": []}}

                resp = client.post("/chat", json={
                    "message": "show Ansel",
                    "session_id": "test-sep",
                    "user_context": {},
                })
                data = resp.get_json()
                products = data.get("products", [])

                # Variations should not appear as standalone products in the result
                product_ids = [p.get("id") for p in products]
                assert 8001 not in product_ids, "Variation 8001 should not appear as a standalone product"
                assert 8002 not in product_ids, "Variation 8002 should not appear as a standalone product"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Step 5.5 — variable product without quantity → AWAITING_VARIANT_SELECTION
# ─────────────────────────────────────────────────────────────────────────────

class TestStep55VariableProductNoQuantity:
    """
    When a user orders a variable product without specifying quantity,
    Step 5.5 should redirect to AWAITING_VARIANT_SELECTION (not AWAITING_QUANTITY).
    """

    def _variable_product(self):
        return {
            "id": 7272, "name": "Allspice", "type": "variable",
            "price": "15.00", "regular_price": "15.00", "sale_price": "",
            "slug": "allspice", "sku": "", "permalink": "",
            "on_sale": False, "stock_status": "instock", "total_sales": 0,
            "description": "", "short_description": "", "images": [],
            "categories": [], "tags": [],
            "attributes": [
                {"name": "Size", "variation": True, "visible": True, "options": ["100g", "500g", "1kg"]},
            ],
            "variations": [9001, 9002, 9003],
            "average_rating": "0.00", "rating_count": 0, "weight": "",
            "dimensions": {"length": "", "width": "", "height": ""},
        }

    def test_variable_product_no_quantity_asks_for_variant(self):
        """Step 5.5: variable product without quantity → AWAITING_VARIANT_SELECTION."""
        from server import app

        with app.test_client() as client:
            with patch("routes.chat.woo_client") as mock_woo:
                mock_woo.execute_all.return_value = [
                    {"success": True, "data": [self._variable_product()], "total": "1", "total_pages": "1"}
                ]

                resp = client.post("/chat", json={
                    "message": "can you place an order allspice",
                    "session_id": "test-step55-variable",
                    "user_context": {"customer_id": 130},
                })
                data = resp.get_json()

                flow = data.get("flow_state") or data.get("metadata", {}).get("flow_state", "")
                assert flow == FlowState.AWAITING_VARIANT_SELECTION.value, \
                    f"Variable product without quantity should go to AWAITING_VARIANT_SELECTION, got: {flow}"

                meta = data.get("metadata", {})
                assert meta.get("pending_product_id") == 7272
                assert meta.get("pending_product_name") == "Allspice"
                # No pending_quantity since we don't have it yet
                assert "pending_quantity" not in meta or meta.get("pending_quantity") is None

    def test_simple_product_no_quantity_asks_for_quantity(self):
        """Step 5.5: simple product without quantity → AWAITING_QUANTITY (unchanged behavior)."""
        from server import app

        simple_product = {
            "id": 7266, "name": "Lager", "type": "simple",
            "price": "10.00", "regular_price": "10.00", "sale_price": "",
            "slug": "lager", "sku": "", "permalink": "",
            "on_sale": False, "stock_status": "instock", "total_sales": 0,
            "description": "", "short_description": "", "images": [],
            "categories": [], "tags": [], "attributes": [], "variations": [],
            "average_rating": "0.00", "rating_count": 0, "weight": "",
            "dimensions": {"length": "", "width": "", "height": ""},
        }

        with app.test_client() as client:
            with patch("routes.chat.woo_client") as mock_woo:
                mock_woo.execute_all.return_value = [
                    {"success": True, "data": [simple_product], "total": "1", "total_pages": "1"}
                ]

                resp = client.post("/chat", json={
                    "message": "order lager",
                    "session_id": "test-step55-simple",
                    "user_context": {"customer_id": 130},
                })
                data = resp.get_json()

                flow = data.get("flow_state") or data.get("metadata", {}).get("flow_state", "")
                assert flow == FlowState.AWAITING_QUANTITY.value, \
                    f"Simple product without quantity should go to AWAITING_QUANTITY, got: {flow}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Step 3.55 — variant resolved but quantity missing → AWAITING_QUANTITY
#    with pending_variation_id preserved
# ─────────────────────────────────────────────────────────────────────────────

class TestStep355VariantResolvedNoQuantity:
    """
    When variant is resolved in Step 3.55 but pending_quantity is None
    (because the user was asked for variant before quantity), the response
    should ask for quantity and preserve pending_variation_id in metadata.
    """

    def test_variant_resolved_no_quantity_asks_for_quantity(self):
        """Step 3.55: variant resolved but no quantity → AWAITING_QUANTITY with pending_variation_id."""
        from server import app
        from models import Intent, ExtractedEntities

        variation = {
            "id": 9001, "parent_id": 7272, "name": "Allspice — 100g",
            "attributes": [{"name": "Size", "option": "100g"}],
            "price": "5.00", "regular_price": "5.00", "sale_price": "",
            "sku": "", "stock_status": "instock", "on_sale": False, "slug": "", "image": {},
        }

        # Build a mock classify result that bypasses the LLM disambiguation check
        mock_classify_result = Mock()
        mock_classify_result.intent = Intent.QUICK_ORDER
        mock_classify_result.confidence = 0.95
        mock_classify_result.entities = ExtractedEntities(
            product_name="Allspice", order_item_name="100g", quantity=None,
        )

        with app.test_client() as client:
            with patch("routes.chat.woo_client") as mock_woo, \
                 patch("routes.chat.classify", return_value=mock_classify_result):
                mock_woo.execute.return_value = {
                    "success": True, "data": [variation],
                }

                resp = client.post("/chat", json={
                    "message": "100g",
                    "session_id": "test-step355-no-qty",
                    "user_context": {
                        "customer_id": 130,
                        "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                        "pending_product_id": 7272,
                        "pending_product_name": "Allspice",
                        # No pending_quantity — this is the scenario from the bug
                    },
                })
                data = resp.get_json()

                flow = data.get("flow_state") or data.get("metadata", {}).get("flow_state", "")
                assert flow == FlowState.AWAITING_QUANTITY.value, \
                    f"Resolved variant without quantity should go to AWAITING_QUANTITY, got: {flow}"

                meta = data.get("metadata", {})
                assert meta.get("pending_product_id") == 7272
                assert meta.get("pending_product_name") == "Allspice"
                assert meta.get("pending_variation_id") == 9001, \
                    f"pending_variation_id should be preserved, got: {meta.get('pending_variation_id')}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
