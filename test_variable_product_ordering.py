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
        """Non-cancel, non-topic-change messages should pass through with resolve_variant=True."""
        for phrase in ["True White Matte", "Polished 6x6", "Charcoal Honed 12x24"]:
            result = self._call(phrase)
            assert result is not None, f"Expected result for phrase: {phrase}"
            assert result.get("pass_through") is True, f"Expected pass_through for: {phrase}"
            assert result.get("resolve_variant") is True, f"Expected resolve_variant for: {phrase}"
            assert result.get("flow_state") == FlowState.AWAITING_VARIANT_SELECTION.value

    def test_topic_change_resets_to_idle(self):
        """Messages that clearly start a new topic should return None (reset to IDLE)."""
        for phrase in [
            "show me products", "show products", "browse categories",
            "what categories do you have", "check my orders", "hello", "hi",
        ]:
            result = self._call(phrase)
            assert result is None, f"Expected None (topic-change reset) for: '{phrase}', got: {result}"

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


# ─────────────────────────────────────────────────────────────────────────────
# 9. _filter_variations_by_entities — sample_size filter
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterVariationsBySampleSize:
    """Verify that sample_size in entities correctly filters variations."""

    def _make_variations(self):
        """Return a small set of Allspice-like variations with 3 attributes."""
        base_attrs = [
            ("ALLSPICE Beleza", "Honed",    '1 7/8"x7 3/8" Chip Size'),
            ("ALLSPICE Beleza", "Honed",    '12"x24"'),
            ("ALLSPICE Beleza", "Polished", '1 7/8"x7 3/8" Chip Size'),
            ("ALLSPICE Brilho", "Honed",    '1 7/8"x7 3/8" Chip Size'),
        ]
        variations = []
        for i, (color, finish, sample) in enumerate(base_attrs):
            variations.append({
                "id": 1000 + i,
                "attributes": [
                    {"name": "Colors",      "option": color},
                    {"name": "Finish",      "option": finish},
                    {"name": "Sample Size", "option": sample},
                ],
            })
        return variations

    def test_sample_size_filters_correctly(self):
        from formatters import _filter_variations_by_entities

        entities = ExtractedEntities(sample_size='1 7/8"x7 3/8" Chip Size')
        matched = _filter_variations_by_entities(self._make_variations(), entities)
        assert len(matched) == 3, f"Expected 3 chip-size variants, got {len(matched)}"
        for v in matched:
            opts = {a["name"]: a["option"] for a in v["attributes"]}
            assert opts["Sample Size"] == '1 7/8"x7 3/8" Chip Size'

    def test_sample_size_combined_with_finish(self):
        from formatters import _filter_variations_by_entities

        entities = ExtractedEntities(finish="Honed", sample_size='1 7/8"x7 3/8" Chip Size')
        matched = _filter_variations_by_entities(self._make_variations(), entities)
        assert len(matched) == 2, f"Expected 2 Honed+chip-size variants, got {len(matched)}"

    def test_sample_size_combined_with_color_and_finish(self):
        from formatters import _filter_variations_by_entities

        entities = ExtractedEntities(
            color_tone="allspice beleza",
            finish="honed",
            sample_size='1 7/8"x7 3/8" chip size',
        )
        matched = _filter_variations_by_entities(self._make_variations(), entities)
        assert len(matched) == 1, f"Expected exactly 1 variation, got {len(matched)}"
        assert matched[0]["id"] == 1000


# ─────────────────────────────────────────────────────────────────────────────
# 10. Step 3.55 — raw-text fallback resolves Allspice scenario
# ─────────────────────────────────────────────────────────────────────────────

class TestStep355RawTextFallback:
    """
    The raw-text fallback in Step 3.55 should resolve to a single variation
    when the entity extractor doesn't recognise product-specific colour names
    like "Allspice Beleza" but they appear verbatim in the user's message.
    """

    def _allspice_variations(self):
        """51-variation-like set simplified to 4 Honed variants plus a ghost variation."""
        combos = [
            ("ALLSPICE Beleza",      "Honed",    '1 7/8"x7 3/8" Chip Size'),
            ("ALLSPICE Beleza",      "Honed",    '12"x24"'),
            ("ALLSPICE Brilho Azul", "Honed",    '1 7/8"x7 3/8" Chip Size'),
            ("ALLSPICE Calacatta",   "Honed",    '1 7/8"x7 3/8" Chip Size'),
        ]
        variations = []
        for i, (color, finish, sample) in enumerate(combos):
            variations.append({
                "id": 2000 + i,
                "parent_id": 7272,
                "attributes": [
                    {"name": "Colors",      "option": color},
                    {"name": "Finish",      "option": finish},
                    {"name": "Sample Size", "option": sample},
                ],
                "price": "15.00", "regular_price": "15.00", "sale_price": "",
                "sku": "", "stock_status": "instock", "on_sale": False,
                "slug": "", "image": {}, "purchasable": True,
            })
        # Ghost variation — empty attributes, not purchasable; should be filtered out
        variations.append({
            "id": 8611, "parent_id": 7272, "name": "", "attributes": [],
            "price": "", "purchasable": False, "sku": "", "stock_status": "instock",
            "on_sale": False, "slug": "", "image": {},
        })
        return variations

    def test_raw_text_fallback_resolves_to_single_variant(self):
        """
        When entity extraction yields 'finish=Honed' only (4 matches), the smart
        text-scoring fallback should use the message text to narrow down to exactly
        1 variation.  Uses natural/partial input ("7/8") rather than the full
        WooCommerce option string to confirm token-overlap scoring works.
        """
        from server import app

        mock_classify = Mock()
        mock_classify.intent = Intent.QUICK_ORDER
        mock_classify.confidence = 0.90
        # Classifier only extracts finish; colour and sample_size are product-specific
        mock_classify.entities = ExtractedEntities(
            product_name="Allspice", finish="Honed", quantity=None,
        )

        with app.test_client() as client:
            with patch("routes.chat.woo_client") as mock_woo, \
                 patch("routes.chat.classify", return_value=mock_classify):
                mock_woo.execute.return_value = {
                    "success": True,
                    "data": self._allspice_variations(),
                }

                resp = client.post("/chat", json={
                    "message": 'i will like color Allspice Beleza, finish Honed and sample size 7/8"',
                    "session_id": "test-rawtext-fallback",
                    "user_context": {
                        "customer_id": 130,
                        "flow_state": "awaiting_variant_selection",
                        "pending_product_id": 7272,
                        "pending_product_name": "Allspice",
                        "pending_quantity": 1,
                    },
                })
                data = resp.get_json()

                # The smart text-scoring fallback should match variation 2000 and create
                # the order, or at minimum narrow down to 1 and NOT stay in
                # awaiting_variant_selection
                flow = data.get("flow_state") or data.get("metadata", {}).get("flow_state", "")
                assert flow != "awaiting_variant_selection", \
                    f"Raw-text fallback should resolve the variation; still in selection loop. Response: {data}"

    def test_ghost_variation_filtered_out(self):
        """Ghost variation (empty attributes, purchasable=False) must not pollute candidate list."""
        from formatters import _filter_variations_by_entities

        all_vars = self._allspice_variations()
        # Without filtering, all_vars has 5 items (4 real + 1 ghost)
        assert len(all_vars) == 5

        entities = ExtractedEntities(finish="Honed")
        matched = _filter_variations_by_entities(all_vars, entities)
        # Ghost variation should never appear in the results
        ids = [v["id"] for v in matched]
        assert 8611 not in ids, "Ghost variation (id=8611) should be excluded from matches"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
