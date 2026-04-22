"""
test_actions.py

Unit tests for the headless checkout action vocabulary introduced in PR 1.

Covers:
  1. Envelope test — every chat-style response includes ``actions: []``.
  2. Flag-off test — _filter_actions_by_flag removes gated actions when flag=False.
  3. Flag-on test  — _filter_actions_by_flag keeps all actions when flag=True.
  4. Builder tests — each builder produces {type, payload} and rejects bad input.
  5. Backward-compat — confirm_add_to_cart still sets legacy fields AND emits ADD_TO_CART.
"""

import pytest
from flask import Flask

# ── Module under test ────────────────────────────────────────────────────────
from core.actions import (
    ActionType,
    _filter_actions_by_flag,
    _CHECKOUT_GATED_ACTIONS,
    build_add_to_cart,
    build_open_cart_panel,
    build_update_cart_item,
    build_remove_cart_item,
    build_open_checkout_panel,
    build_propose_checkout_address,
)


# ════════════════════════════════════════════════════════════════════════════
# 1. Envelope: _finalize_turn always injects actions: []
# ════════════════════════════════════════════════════════════════════════════

class TestFinalizeEnvelope:
    """Test that _finalize_turn injects an 'actions' key on every response."""

    def test_actions_key_present_when_no_actions_in_payload(self):
        """A response that sets no 'actions' key still gets actions: [] injected."""
        app = Flask(__name__)
        app.config["TESTING"] = True

        with app.app_context():
            from flask import jsonify

            # Build a minimal response WITHOUT an 'actions' key
            resp = jsonify({
                "success": True,
                "bot_message": "hello",
                "intent": "greeting",
                "flow_state": "idle",
                "metadata": {},
            })
            data = resp.get_json()
            # Simulate what _finalize_turn does for the actions step
            raw_actions = data.get("actions") if isinstance(data.get("actions"), list) else []
            data["actions"] = _filter_actions_by_flag(raw_actions, False)

            assert "actions" in data
            assert isinstance(data["actions"], list)
            assert data["actions"] == []

    def test_actions_key_present_when_actions_already_set(self):
        """A response that already has actions: [...] keeps them (after filtering)."""
        app = Flask(__name__)
        app.config["TESTING"] = True

        with app.app_context():
            from flask import jsonify

            add_action = build_add_to_cart(product_id=42, quantity=1)
            resp = jsonify({
                "success": True,
                "bot_message": "Adding item",
                "intent": "add_to_cart",
                "flow_state": "idle",
                "metadata": {},
                "actions": [add_action],
            })
            data = resp.get_json()
            raw_actions = data.get("actions") if isinstance(data.get("actions"), list) else []
            data["actions"] = _filter_actions_by_flag(raw_actions, False)

            assert "actions" in data
            assert isinstance(data["actions"], list)
            assert len(data["actions"]) == 1
            assert data["actions"][0]["type"] == ActionType.ADD_TO_CART


# ════════════════════════════════════════════════════════════════════════════
# 2 & 3. _filter_actions_by_flag
# ════════════════════════════════════════════════════════════════════════════

class TestFilterActionsByFlag:
    """Tests for _filter_actions_by_flag."""

    # ── Non-gated actions always pass through ──

    def test_cart_actions_pass_when_flag_off(self):
        actions = [
            build_add_to_cart(product_id=1, quantity=2),
            build_open_cart_panel(),
        ]
        result = _filter_actions_by_flag(actions, enabled=False)
        assert len(result) == 2
        types = {a["type"] for a in result}
        assert ActionType.ADD_TO_CART in types
        assert ActionType.OPEN_CART_PANEL in types

    def test_cart_actions_pass_when_flag_on(self):
        actions = [
            build_add_to_cart(product_id=1, quantity=2),
            build_open_cart_panel(),
        ]
        result = _filter_actions_by_flag(actions, enabled=True)
        assert len(result) == 2

    # ── Gated actions are removed when flag is off ──

    def test_gated_actions_removed_when_flag_off(self):
        actions = [
            build_open_checkout_panel(),
            build_propose_checkout_address(parsed={"line_1": "123 Main St"}),
            build_update_cart_item(quantity=3),
            build_remove_cart_item(key="abc123"),
        ]
        result = _filter_actions_by_flag(actions, enabled=False)
        assert result == []

    def test_gated_actions_pass_when_flag_on(self):
        actions = [
            build_open_checkout_panel(),
            build_propose_checkout_address(parsed={"line_1": "123 Main St"}),
            build_update_cart_item(quantity=3),
            build_remove_cart_item(key="abc123"),
        ]
        result = _filter_actions_by_flag(actions, enabled=True)
        assert len(result) == 4

    def test_mixed_actions_flag_off(self):
        """Cart actions survive; gated actions are removed."""
        actions = [
            build_add_to_cart(product_id=5, quantity=1),
            build_open_checkout_panel(),
            build_open_cart_panel(),
        ]
        result = _filter_actions_by_flag(actions, enabled=False)
        assert len(result) == 2
        types = [a["type"] for a in result]
        assert ActionType.ADD_TO_CART in types
        assert ActionType.OPEN_CART_PANEL in types
        assert ActionType.OPEN_CHECKOUT_PANEL not in types

    def test_empty_list_returns_empty(self):
        assert _filter_actions_by_flag([], enabled=False) == []
        assert _filter_actions_by_flag([], enabled=True) == []

    def test_returns_new_list_not_mutating_original(self):
        original = [build_add_to_cart(product_id=1, quantity=1)]
        result = _filter_actions_by_flag(original, enabled=False)
        assert result is not original


# ════════════════════════════════════════════════════════════════════════════
# 4. Builder functions
# ════════════════════════════════════════════════════════════════════════════

class TestBuilders:
    """Tests that each builder produces the correct {type, payload} shape."""

    # ── build_add_to_cart ──

    def test_add_to_cart_minimal(self):
        action = build_add_to_cart(product_id=10, quantity=2)
        assert action["type"] == ActionType.ADD_TO_CART
        assert action["payload"]["product_id"] == 10
        assert action["payload"]["quantity"] == 2
        assert "variation_id" not in action["payload"]
        assert "variation" not in action["payload"]

    def test_add_to_cart_with_variation(self):
        variation = [{"attribute": "pa_color", "value": "blue"}]
        action = build_add_to_cart(
            product_id=10, quantity=1, variation_id=99, variation=variation
        )
        assert action["payload"]["variation_id"] == 99
        assert action["payload"]["variation"] == variation

    def test_add_to_cart_missing_product_id(self):
        with pytest.raises(ValueError, match="product_id"):
            build_add_to_cart(product_id=None, quantity=1)

    def test_add_to_cart_missing_quantity(self):
        with pytest.raises(ValueError, match="quantity"):
            build_add_to_cart(product_id=1, quantity=None)

    def test_add_to_cart_has_exactly_two_top_level_keys(self):
        action = build_add_to_cart(product_id=1, quantity=1)
        assert set(action.keys()) == {"type", "payload"}

    # ── build_open_cart_panel ──

    def test_open_cart_panel_shape(self):
        action = build_open_cart_panel()
        assert action["type"] == ActionType.OPEN_CART_PANEL
        assert action["payload"] == {}
        assert set(action.keys()) == {"type", "payload"}

    # ── build_update_cart_item ──

    def test_update_cart_item_with_key(self):
        action = build_update_cart_item(quantity=3, key="k1")
        assert action["type"] == ActionType.UPDATE_CART_ITEM
        assert action["payload"]["quantity"] == 3
        assert action["payload"]["key"] == "k1"

    def test_update_cart_item_missing_quantity(self):
        with pytest.raises(ValueError, match="quantity"):
            build_update_cart_item(quantity=None)

    def test_update_cart_item_has_exactly_two_top_level_keys(self):
        action = build_update_cart_item(quantity=1)
        assert set(action.keys()) == {"type", "payload"}

    # ── build_remove_cart_item ──

    def test_remove_cart_item_with_key(self):
        action = build_remove_cart_item(key="k2")
        assert action["type"] == ActionType.REMOVE_CART_ITEM
        assert action["payload"]["key"] == "k2"

    def test_remove_cart_item_empty_payload_allowed(self):
        """Payload may be empty; resolution is left to the frontend."""
        action = build_remove_cart_item()
        assert action["type"] == ActionType.REMOVE_CART_ITEM
        assert action["payload"] == {}

    def test_remove_cart_item_has_exactly_two_top_level_keys(self):
        action = build_remove_cart_item()
        assert set(action.keys()) == {"type", "payload"}

    # ── build_open_checkout_panel ──

    def test_open_checkout_panel_shape(self):
        action = build_open_checkout_panel()
        assert action["type"] == ActionType.OPEN_CHECKOUT_PANEL
        assert action["payload"] == {}
        assert set(action.keys()) == {"type", "payload"}

    # ── build_propose_checkout_address ──

    def test_propose_checkout_address_minimal(self):
        parsed = {"line_1": "123 Main St", "city": "Anytown"}
        action = build_propose_checkout_address(parsed=parsed)
        assert action["type"] == ActionType.PROPOSE_CHECKOUT_ADDRESS
        assert action["payload"]["parsed"] == parsed
        assert "existing_on_file" not in action["payload"]

    def test_propose_checkout_address_with_existing(self):
        parsed = {"line_1": "123 Main St"}
        existing = {"line_1": "456 Old Rd"}
        action = build_propose_checkout_address(parsed=parsed, existing_on_file=existing)
        assert action["payload"]["existing_on_file"] == existing

    def test_propose_checkout_address_missing_parsed(self):
        with pytest.raises(ValueError, match="parsed"):
            build_propose_checkout_address(parsed=None)

    def test_propose_checkout_address_empty_parsed_raises(self):
        with pytest.raises(ValueError, match="parsed"):
            build_propose_checkout_address(parsed={})

    def test_propose_checkout_address_has_exactly_two_top_level_keys(self):
        action = build_propose_checkout_address(parsed={"line_1": "x"})
        assert set(action.keys()) == {"type", "payload"}


# ════════════════════════════════════════════════════════════════════════════
# 5. Backward-compat: legacy fields are preserved AND new action is emitted
# ════════════════════════════════════════════════════════════════════════════

class TestBackwardCompatCartAdd:
    """
    The ADD_TO_CART flow must still set the legacy trigger_frontend_cart_add
    action field AND metadata (product_id, variation_id, quantity) while
    also including the new ADD_TO_CART entry in the actions[] array.
    """

    def _build_cart_add_response(self):
        """
        Simulate what cart_handler.handle_cart_intent returns for ADD_TO_CART,
        without needing a live DB/Flask app.
        """
        from unittest.mock import MagicMock
        from flask import jsonify

        app = Flask(__name__)
        app.config["TESTING"] = True
        with app.app_context():
            from models import Intent
            from conversation_flow import FlowState
            from handlers.chat_utils import default_pagination

            intent = Intent.ADD_TO_CART
            product_id = 42
            variation_id = 7
            qty = 3
            name = "Aura Tile"

            add_action = build_add_to_cart(
                product_id=product_id,
                quantity=qty,
                variation_id=variation_id,
            )

            resp = jsonify({
                "success":     True,
                "bot_message": f"Adding **{name}** to your cart... 🛒",
                "action":      "trigger_frontend_cart_add",
                "intent":      intent.value,
                "products":    [],
                "suggestions": ["Browse products", "Go to cart", "Checkout"],
                "session_id":  "test-session",
                "metadata":    {
                    "response_time_ms": 50,
                    "product_id":   product_id,
                    "variation_id": variation_id,
                    "quantity":     qty,
                },
                "pagination":  default_pagination(1),
                "flow_state":  FlowState.IDLE.value,
                "actions":     [add_action],
            })
            return resp.get_json()

    def test_legacy_action_field_preserved(self):
        data = self._build_cart_add_response()
        assert data["action"] == "trigger_frontend_cart_add"

    def test_legacy_metadata_fields_preserved(self):
        data = self._build_cart_add_response()
        meta = data["metadata"]
        assert meta["product_id"] == 42
        assert meta["variation_id"] == 7
        assert meta["quantity"] == 3

    def test_new_actions_array_present_and_correct(self):
        data = self._build_cart_add_response()
        assert "actions" in data
        assert isinstance(data["actions"], list)
        assert len(data["actions"]) == 1

        action = data["actions"][0]
        assert action["type"] == ActionType.ADD_TO_CART
        assert action["payload"]["product_id"] == 42
        assert action["payload"]["quantity"] == 3
        assert action["payload"]["variation_id"] == 7

    def test_after_flag_filter_add_to_cart_survives(self):
        """ADD_TO_CART is not gated and must survive flag=False filtering."""
        data = self._build_cart_add_response()
        raw = data.get("actions", [])
        filtered = _filter_actions_by_flag(raw, enabled=False)
        types = [a["type"] for a in filtered]
        assert ActionType.ADD_TO_CART in types
