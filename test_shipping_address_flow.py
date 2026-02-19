"""
Tests for the shipping address confirmation step in the order flow.

Covers:
- New FlowState enums
- AWAITING_SHIPPING_CONFIRM handler transitions
- AWAITING_NEW_ADDRESS handler transitions
- AWAITING_ADDRESS_CONFIRM handler transitions
- parse_address() helper
- Order creation with and without shipping override (via mocked WooCommerce API)
- "No address on file" path
"""
import pytest
from unittest.mock import patch, MagicMock

from conversation_flow import FlowState, handle_flow_state
from routes.chat import parse_address


# ═══════════════════════════════════════════════════════════════
#  A. FlowState enum tests
# ═══════════════════════════════════════════════════════════════

class TestNewFlowStateEnums:
    """Verify that the three new FlowState values exist."""

    def test_awaiting_shipping_confirm_exists(self):
        assert FlowState.AWAITING_SHIPPING_CONFIRM.value == "awaiting_shipping_confirm"

    def test_awaiting_new_address_exists(self):
        assert FlowState.AWAITING_NEW_ADDRESS.value == "awaiting_new_address"

    def test_awaiting_address_confirm_exists(self):
        assert FlowState.AWAITING_ADDRESS_CONFIRM.value == "awaiting_address_confirm"

    def test_new_states_parseable_from_string(self):
        assert FlowState("awaiting_shipping_confirm") == FlowState.AWAITING_SHIPPING_CONFIRM
        assert FlowState("awaiting_new_address") == FlowState.AWAITING_NEW_ADDRESS
        assert FlowState("awaiting_address_confirm") == FlowState.AWAITING_ADDRESS_CONFIRM


# ═══════════════════════════════════════════════════════════════
#  B. AWAITING_SHIPPING_CONFIRM handler
# ═══════════════════════════════════════════════════════════════

class TestAwaitingShippingConfirm:
    """Test the AWAITING_SHIPPING_CONFIRM flow state handler."""

    def _call(self, message):
        return handle_flow_state(
            state=FlowState.AWAITING_SHIPPING_CONFIRM,
            message=message,
            entities={},
            confidence=0.9,
        )

    def test_yes_triggers_create_order_with_existing_address(self):
        for phrase in ["yes", "Yes", "ok", "confirm", "correct", "sure", "use this", "ship here"]:
            result = self._call(phrase)
            assert result is not None, f"Expected result for phrase: {phrase}"
            assert result.get("create_order") is True, f"create_order should be True for: {phrase}"
            assert result.get("use_existing_address") is True, f"use_existing_address should be True for: {phrase}"
            assert result.get("pass_through") is True, f"pass_through should be True for: {phrase}"
            assert result.get("flow_state") == FlowState.ORDER_COMPLETE.value

    def test_change_transitions_to_awaiting_new_address(self):
        for phrase in ["change", "new address", "different", "update"]:
            result = self._call(phrase)
            assert result is not None, f"Expected result for phrase: {phrase}"
            assert result.get("flow_state") == FlowState.AWAITING_NEW_ADDRESS.value, f"Failed for: {phrase}"
            assert result.get("pass_through") is not True, f"Should NOT pass through for: {phrase}"
            assert "bot_message" in result

    def test_cancel_aborts_order(self):
        for phrase in ["cancel", "no", "stop"]:
            result = self._call(phrase)
            assert result is not None, f"Expected result for phrase: {phrase}"
            assert result.get("flow_state") == FlowState.AWAITING_ANYTHING_ELSE.value, f"Failed for: {phrase}"
            assert result.get("pass_through") is not True
            assert "cancelled" in result.get("bot_message", "").lower(), f"Missing 'cancelled' for: {phrase}"


# ═══════════════════════════════════════════════════════════════
#  C. AWAITING_NEW_ADDRESS handler
# ═══════════════════════════════════════════════════════════════

class TestAwaitingNewAddress:
    """Test the AWAITING_NEW_ADDRESS flow state handler."""

    def _call(self, message):
        return handle_flow_state(
            state=FlowState.AWAITING_NEW_ADDRESS,
            message=message,
            entities={},
            confidence=0.9,
        )

    def test_free_text_transitions_to_address_confirm(self):
        address = "123 Main St, Los Angeles, CA 90001"
        result = self._call(address)
        assert result is not None
        assert result.get("flow_state") == FlowState.AWAITING_ADDRESS_CONFIRM.value
        assert result.get("pending_shipping_address") == address
        assert "123 Main St" in result.get("bot_message", "")
        assert result.get("pass_through") is not True

    def test_address_preserved_with_original_casing(self):
        address = "456 Oak Avenue, New York, NY 10001"
        result = self._call(address)
        assert result.get("pending_shipping_address") == address

    def test_cancel_aborts_order(self):
        for phrase in ["cancel", "stop"]:
            result = self._call(phrase)
            assert result is not None, f"Expected result for phrase: {phrase}"
            assert result.get("flow_state") == FlowState.AWAITING_ANYTHING_ELSE.value
            assert result.get("pass_through") is not True


# ═══════════════════════════════════════════════════════════════
#  D. AWAITING_ADDRESS_CONFIRM handler
# ═══════════════════════════════════════════════════════════════

class TestAwaitingAddressConfirm:
    """Test the AWAITING_ADDRESS_CONFIRM flow state handler."""

    def _call(self, message):
        return handle_flow_state(
            state=FlowState.AWAITING_ADDRESS_CONFIRM,
            message=message,
            entities={},
            confidence=0.9,
        )

    def test_yes_triggers_create_order_with_new_address(self):
        for phrase in ["yes", "confirm", "correct", "ok", "sure"]:
            result = self._call(phrase)
            assert result is not None, f"Expected result for phrase: {phrase}"
            assert result.get("create_order") is True, f"create_order should be True for: {phrase}"
            assert result.get("use_new_address") is True, f"use_new_address should be True for: {phrase}"
            assert result.get("pass_through") is True
            assert result.get("flow_state") == FlowState.ORDER_COMPLETE.value

    def test_reenter_goes_back_to_new_address(self):
        for phrase in ["re-enter", "change", "wrong", "different", "no"]:
            result = self._call(phrase)
            assert result is not None, f"Expected result for phrase: {phrase}"
            assert result.get("flow_state") == FlowState.AWAITING_NEW_ADDRESS.value, f"Failed for: {phrase}"
            assert result.get("pass_through") is not True

    def test_cancel_aborts_order(self):
        for phrase in ["cancel", "stop"]:
            result = self._call(phrase)
            assert result is not None
            assert result.get("flow_state") == FlowState.AWAITING_ANYTHING_ELSE.value


# ═══════════════════════════════════════════════════════════════
#  E. parse_address() tests
# ═══════════════════════════════════════════════════════════════

class TestParseAddress:
    """Test the free-text address parser."""

    def test_full_address_four_parts(self):
        result = parse_address("123 Main St, Los Angeles, CA, 90001")
        assert result["address_1"] == "123 Main St"
        assert result["city"] == "Los Angeles"
        assert result["state"] == "CA"
        assert result["postcode"] == "90001"
        assert result["country"] == "US"

    def test_three_parts_state_zip_combined(self):
        result = parse_address("456 Oak Ave, Chicago, IL 60601")
        assert result["address_1"] == "456 Oak Ave"
        assert result["city"] == "Chicago"
        assert result["state"] == "IL"
        assert result["postcode"] == "60601"

    def test_minimal_one_part(self):
        result = parse_address("789 Pine Rd")
        assert result["address_1"] == "789 Pine Rd"
        assert result["country"] == "US"

    def test_two_parts(self):
        result = parse_address("100 Broadway, New York")
        assert result["address_1"] == "100 Broadway"
        assert result["city"] == "New York"

    def test_country_always_us(self):
        result = parse_address("1 Infinite Loop, Cupertino, CA 95014")
        assert result["country"] == "US"

    def test_strips_whitespace(self):
        result = parse_address("  10 Elm St ,  Seattle ,  WA 98101  ")
        assert result["address_1"] == "10 Elm St"
        assert result["city"] == "Seattle"
        assert result["state"] == "WA"
        assert result["postcode"] == "98101"


# ═══════════════════════════════════════════════════════════════
#  F. Order creation with/without shipping override (unit tests)
# ═══════════════════════════════════════════════════════════════

class TestOrderCreationShippingOverride:
    """Test that order body includes shipping override only when a new address was provided."""

    def _make_app_client(self):
        from server import app
        return app.test_client()

    @patch("routes.chat.woo_client")
    def test_create_order_with_new_address_includes_shipping(self, mock_woo):
        """When use_new_address=True, order body should contain a shipping dict."""
        # Arrange
        mock_order_resp = {
            "success": True,
            "data": {
                "id": 999,
                "number": "999",
                "total": "50.00",
                "currency_symbol": "$",
                "line_items": [{"name": "Test Tile", "quantity": 2, "total": "50.00"}],
            },
        }
        mock_woo.execute.return_value = mock_order_resp

        client = self._make_app_client()
        resp = client.post("/chat", json={
            "message": "yes",
            "session_id": "test-shipping-new",
            "user_context": {
                "customer_id": 123,
                "flow_state": "awaiting_address_confirm",
                "pending_product_id": 42,
                "pending_product_name": "Test Tile",
                "pending_quantity": 2,
                "pending_shipping_address": "321 Elm St, Portland, OR 97201",
            },
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

        # The woo_client.execute call should have included a "shipping" key in the body
        call_args = mock_woo.execute.call_args
        order_body = call_args[0][0].body
        assert "shipping" in order_body, "Expected shipping override in order body"
        assert order_body["shipping"]["address_1"] == "321 Elm St"
        assert order_body["shipping"]["city"] == "Portland"

    @patch("routes.chat.woo_client")
    def test_create_order_with_existing_address_no_shipping_override(self, mock_woo):
        """When use_existing_address=True, order body should NOT contain a shipping dict."""
        mock_order_resp = {
            "success": True,
            "data": {
                "id": 998,
                "number": "998",
                "total": "30.00",
                "currency_symbol": "$",
                "line_items": [{"name": "Test Tile", "quantity": 1, "total": "30.00"}],
            },
        }
        mock_woo.execute.return_value = mock_order_resp

        client = self._make_app_client()
        resp = client.post("/chat", json={
            "message": "yes, use this address",
            "session_id": "test-shipping-existing",
            "user_context": {
                "customer_id": 123,
                "flow_state": "awaiting_shipping_confirm",
                "pending_product_id": 42,
                "pending_product_name": "Test Tile",
                "pending_quantity": 1,
            },
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

        call_args = mock_woo.execute.call_args
        order_body = call_args[0][0].body
        assert "shipping" not in order_body, "Should NOT include shipping override when using existing address"

    @patch("routes.chat.woo_client")
    def test_no_address_on_file_prompts_for_address(self, mock_woo):
        """When customer has no shipping address, user should be prompted to enter one."""
        # First call: GET /customers/{id} → no address
        # (fetch_customer_address path)
        mock_woo.execute.return_value = {
            "success": True,
            "data": {
                "id": 123,
                "shipping": {"address_1": "", "city": "", "state": "", "postcode": "", "country": ""},
            },
        }

        client = self._make_app_client()
        resp = client.post("/chat", json={
            "message": "yes",
            "session_id": "test-no-address",
            "user_context": {
                "customer_id": 123,
                "flow_state": "awaiting_order_confirm",
                "pending_product_id": 42,
                "pending_product_name": "Test Tile",
                "pending_quantity": 3,
            },
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["flow_state"] == FlowState.AWAITING_NEW_ADDRESS.value
        assert "No shipping address" in data["bot_message"] or "address" in data["bot_message"].lower()

    @patch("routes.chat.woo_client")
    def test_address_on_file_shows_address_to_user(self, mock_woo):
        """When customer has a shipping address, it should be shown to the user."""
        mock_woo.execute.return_value = {
            "success": True,
            "data": {
                "id": 123,
                "shipping": {
                    "address_1": "100 Main St",
                    "address_2": "",
                    "city": "Austin",
                    "state": "TX",
                    "postcode": "78701",
                    "country": "US",
                },
            },
        }

        client = self._make_app_client()
        resp = client.post("/chat", json={
            "message": "yes",
            "session_id": "test-has-address",
            "user_context": {
                "customer_id": 123,
                "flow_state": "awaiting_order_confirm",
                "pending_product_id": 42,
                "pending_product_name": "Test Tile",
                "pending_quantity": 5,
            },
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["flow_state"] == FlowState.AWAITING_SHIPPING_CONFIRM.value
        assert "100 Main St" in data["bot_message"]
        assert "Austin" in data["bot_message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
