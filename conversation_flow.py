"""
Conversation Flow State Machine for MiraQ Chat.

Manages multi-turn flows like:
  - Intent disambiguation (when classifier is confused)
  - Guided product search → order placement
  - Quantity confirmation → order creation
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List


class FlowState(Enum):
    """Possible conversation states."""
    IDLE = "idle"                          # No active flow
    AWAITING_INTENT_CHOICE = "awaiting_intent_choice"  # MQ asked: product/category/order?
    AWAITING_PRODUCT_OR_CATEGORY = "awaiting_product_or_category"  # User chose product/category
    SHOWING_RESULTS = "showing_results"    # Results displayed, user may order or refine
    AWAITING_QUANTITY = "awaiting_quantity" # MQ asked: how many?
    AWAITING_ORDER_CONFIRM = "awaiting_order_confirm"  # MQ asked: Place order for N items. OK?
    AWAITING_SHIPPING_CONFIRM = "awaiting_shipping_confirm"  # Show address, ask use/change
    AWAITING_NEW_ADDRESS = "awaiting_new_address"            # User typing new address
    AWAITING_ADDRESS_CONFIRM = "awaiting_address_confirm"    # Confirm the newly typed address
    ORDER_COMPLETE = "order_complete"       # Order placed
    AWAITING_ANYTHING_ELSE = "awaiting_anything_else"  # MQ asked: anything else?
    CLOSING = "closing"                     # User said no, chat closing
    AWAITING_VARIANT_SELECTION = "awaiting_variant_selection"  # MQ asked: which variant?


@dataclass
class ConversationContext:
    """Tracks the state of a multi-turn conversation."""
    state: FlowState = FlowState.IDLE
    
    # Product context carried across turns
    last_product_id: Optional[int] = None
    last_product_name: Optional[str] = None
    last_category_name: Optional[str] = None
    last_results: List[dict] = field(default_factory=list)
    
    # Order context
    pending_product_id: Optional[int] = None
    pending_product_name: Optional[str] = None
    pending_quantity: Optional[int] = None
    pending_variation_id: Optional[int] = None
    
    # Disambiguation context
    user_choice: Optional[str] = None  # "product", "category", "order"


# ── Confidence threshold below which we trigger disambiguation ──
LOW_CONFIDENCE_THRESHOLD = 0.60


def should_disambiguate(intent_value: str, confidence: float) -> bool:
    """
    Returns True when MiraQ should ask the user what they meant.
    Triggers on UNKNOWN intent OR very low confidence.
    """
    return intent_value == "unknown" or confidence < LOW_CONFIDENCE_THRESHOLD


def get_disambiguation_message() -> dict:
    """
    Returns the disambiguation prompt and suggested quick-reply buttons.
    """
    return {
        "bot_message": (
            "I'm sorry, I couldn't understand that. Can we start again? 🤔\n\n"
            "What would you like help with?\n"
            "• **Product** — Search or get info about a product\n"
            "• **Category** — Browse a product category\n"
            "• **Order** — Place a new order or check an existing one"
        ),
        "suggestions": [
            "I want information about a product",
            "Show me product categories",
            "I want to place an order",
            "Check my order status",
        ],
        "flow_state": FlowState.AWAITING_INTENT_CHOICE.value,
    }


def handle_flow_state(
    state: FlowState,
    message: str,
    entities: dict,
    confidence: float,
) -> Optional[dict]:
    """
    Process user message within a multi-turn flow.
    Returns a response dict if the flow handles it, or None to fall through
    to normal classifier pipeline.
    """
    text = message.lower().strip()

    # ── State: User is picking intent from menu ──
    if state == FlowState.AWAITING_INTENT_CHOICE:
        if any(kw in text for kw in ["product", "information", "search", "find"]):
            return {
                "bot_message": (
                    "Sure! What product or category are you looking for? "
                    "You can tell me a product name, category, or describe what you need."
                ),
                "suggestions": [
                    "Show me marble look tiles",
                    "What categories do you have?",
                    "I'm looking for floor tiles",
                ],
                "flow_state": FlowState.AWAITING_PRODUCT_OR_CATEGORY.value,
                "pass_through": False,
            }
        elif any(kw in text for kw in ["category", "categories", "browse"]):
            return {
                "bot_message": "Let me show you our categories!",
                "flow_state": FlowState.IDLE.value,
                "pass_through": True,  # Let classifier handle "show categories"
                "override_message": "show me all categories",
            }
        elif any(kw in text for kw in ["order", "place", "buy", "purchase"]):
            return {
                "bot_message": (
                    "I can help you place an order! 🛒\n\n"
                    "Which product would you like to order? "
                    "You can tell me the product name and quantity."
                ),
                "suggestions": [
                    "Order 5 Affogato tiles",
                    "Show me my last order",
                    "Reorder my previous order",
                ],
                "flow_state": FlowState.AWAITING_PRODUCT_OR_CATEGORY.value,
                "pass_through": False,
            }
        elif any(kw in text for kw in ["yes", "yeah", "ok", "sure", "start again"]):
            return get_disambiguation_message()
        else:
            # No keyword matched — let the classifier pipeline handle it
            return None

    # ── State: Awaiting quantity for an order ──
    if state == FlowState.AWAITING_QUANTITY:
        # Try to parse a number from the message
        import re
        qty_match = re.search(r"\b(\d+)\b", text)
        if qty_match:
            quantity = int(qty_match.group(1))
            product_name = entities.get("pending_product_name", "the product")
            return {
                "bot_message": (
                    f"Placing an order for **{quantity}** × **{product_name}**. Is that OK? ✅"
                ),
                "suggestions": ["Yes, place the order", "No, cancel"],
                "flow_state": FlowState.AWAITING_ORDER_CONFIRM.value,
                "pending_quantity": quantity,
                "pass_through": False,
            }
        else:
            return {
                "bot_message": "How many would you like to order? Please enter a number.",
                "suggestions": ["1", "5", "10", "25"],
                "flow_state": FlowState.AWAITING_QUANTITY.value,
                "pass_through": False,
            }

    # ── State: Awaiting order confirmation ──
    if state == FlowState.AWAITING_ORDER_CONFIRM:
        if any(kw in text for kw in ["yes", "ok", "confirm", "sure", "go ahead", "place"]):
            return {
                "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value,
                "fetch_customer_address": True,
                "pass_through": True,
            }
        elif any(kw in text for kw in ["no", "cancel", "stop", "don't"]):
            return {
                "bot_message": "No problem! Order cancelled. Is there anything else I can help with?",
                "suggestions": [
                    "Show me products",
                    "Browse categories",
                    "No, thank you",
                ],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }

    # ── State: Awaiting shipping address confirmation ──
    if state == FlowState.AWAITING_SHIPPING_CONFIRM:
        if any(kw in text for kw in ["yes", "use this", "ship here", "ok", "confirm", "correct", "sure"]):
            return {
                "flow_state": FlowState.ORDER_COMPLETE.value,
                "create_order": True,
                "pass_through": True,
                "use_existing_address": True,
            }
        elif any(kw in text for kw in ["change", "new address", "different", "update"]):
            return {
                "bot_message": "Please type your new shipping address (street, city, state, zip code):",
                "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
                "pass_through": False,
            }
        elif any(kw in text for kw in ["cancel", "no", "stop"]):
            return {
                "bot_message": "No problem! Order cancelled. Is there anything else I can help with?",
                "suggestions": ["Show me products", "Browse categories", "No, thank you"],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }

    # ── State: Awaiting new shipping address input ──
    if state == FlowState.AWAITING_NEW_ADDRESS:
        if any(kw in text for kw in ["cancel", "stop"]):
            return {
                "bot_message": "No problem! Order cancelled. Is there anything else I can help with?",
                "suggestions": ["Show me products", "Browse categories", "No, thank you"],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }
        # Accept any other text as the new address
        return {
            "flow_state": FlowState.AWAITING_ADDRESS_CONFIRM.value,
            "pending_shipping_address": message.strip(),
            "bot_message": f"Ship to: **{message.strip()}**\n\nIs that correct?",
            "suggestions": ["Yes, correct", "Re-enter address", "Cancel"],
            "pass_through": False,
        }

    # ── State: Awaiting confirmation of new address ──
    if state == FlowState.AWAITING_ADDRESS_CONFIRM:
        if any(kw in text for kw in ["yes", "confirm", "correct", "ok", "sure"]):
            return {
                "flow_state": FlowState.ORDER_COMPLETE.value,
                "create_order": True,
                "pass_through": True,
                "use_new_address": True,
            }
        elif any(kw in text for kw in ["re-enter", "change", "wrong", "different", "no"]):
            return {
                "bot_message": "Please type your new shipping address (street, city, state, zip code):",
                "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
                "pass_through": False,
            }
        elif any(kw in text for kw in ["cancel", "stop"]):
            return {
                "bot_message": "No problem! Order cancelled. Is there anything else I can help with?",
                "suggestions": ["Show me products", "Browse categories", "No, thank you"],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }

    # ── State: Anything else? ──
    if state == FlowState.AWAITING_ANYTHING_ELSE:
        if any(kw in text for kw in ["no", "nothing", "bye", "that's all", "done", "nope"]):
            return {
                "bot_message": "Thank you for chatting with MiraQ! 👋 Have a great day! I'll close this chat now. Bye!",
                "suggestions": [],
                "flow_state": FlowState.CLOSING.value,
                "pass_through": False,
            }
        elif any(kw in text for kw in ["yes", "yeah", "sure"]):
            return get_disambiguation_message()
        else:
            # Treat as a new query — fall through to classifier
            return None

    # ── State: Order complete ──
    if state == FlowState.ORDER_COMPLETE:
        if any(kw in text for kw in ["thank", "thanks"]):
            return {
                "bot_message": "You're welcome! Is there anything else I can help you with? 😊",
                "suggestions": [
                    "Show me more products",
                    "Check my orders",
                    "No, that's all",
                ],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }

    # ── State: Awaiting variant selection for a variable product ──
    if state == FlowState.AWAITING_VARIANT_SELECTION:
        if any(kw in text for kw in ["cancel", "stop", "nevermind", "never mind"]):
            return {
                "bot_message": "No problem! Order cancelled. Is there anything else I can help with?",
                "suggestions": ["Show me products", "Browse categories", "No, thank you"],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }
        # Pass through so Step 3.6 can resolve the variant from the user's response
        return {
            "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
            "pass_through": True,
        }

    return None  # Fall through to normal pipeline