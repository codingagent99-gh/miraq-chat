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
from app_config import BOT_NAME

class FlowState(Enum):
    """Possible conversation states."""
    IDLE = "idle"                          # No active flow
    AWAITING_INTENT_CHOICE = "awaiting_intent_choice"  # MQ asked: product/category/order?
    AWAITING_PRODUCT_OR_CATEGORY = "awaiting_product_or_category"  # User chose product/category
    SHOWING_RESULTS = "showing_results"    # Results displayed, user may order or refine
    AWAITING_QUANTITY = "awaiting_quantity" # MQ asked: how many?
    AWAITING_ANYTHING_ELSE = "awaiting_anything_else"  # MQ asked: anything else?
    CLOSING = "closing"                     # User said no, chat closing
    AWAITING_VARIANT_SELECTION = "awaiting_variant_selection"  # MQ asked: which variant?
    AWAITING_ORDER_DETAIL = "awaiting_order_detail"            # User asked for order detail / clicked an order
    AWAITING_REORDER_ID = "awaiting_reorder_id"
    AWAITING_FILTER_CLARIFICATION = "awaiting_filter_clarification"
    AWAITING_CART_CONFIRMATION = "awaiting_cart_confirmation"
    
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
    import re
    
    text = message.lower().strip()

    # ══════════════════════════════════════════════════════════
    # ── GLOBAL ESCAPE HATCH: Allow users to exit ANY flow ──
    # ══════════════════════════════════════════════════════════
    
    if state == FlowState.AWAITING_CART_CONFIRMATION:
        text = message.lower().strip()

        _yes = re.search(
            r'\b(yes|yeah|yep|sure|okay|ok|go\s+ahead|add\s+it|add\s+to\s+cart)\b',
            text
        )
        _no = re.search(
            r'\b(no|nope|cancel|skip|not\s+now|continue\s+shopping)\b',
            text
        )

        if _yes:
            return {
                "action": "confirm_add_to_cart",   # explicit sentinel
                "flow_state": FlowState.IDLE.value,
                "pass_through": False,             # do NOT fall through to classify
            }
        elif _no:
            return {
                "action": "decline_add_to_cart",
                "flow_state": FlowState.IDLE.value,
                "pass_through": False,
            }
        else:
            # Ambiguous — reset state and let classify handle it normally
            return {
                "action": None,
                "flow_state": FlowState.IDLE.value,
                "pass_through": True,
            }
            
    if state not in (FlowState.IDLE, FlowState.AWAITING_ANYTHING_ELSE):
        # Exact matches or starts-with to prevent accidental triggers 
        # (e.g., we want to catch "cancel order" but not "I want to order cancela tiles")
        exit_phrases = ["cancel", "exit", "stop", "quit", "nevermind", "never mind", "abort", "start over"]
        if text in exit_phrases or any(text.startswith(p + " ") for p in exit_phrases):
            return {
                "bot_message": "No problem! I've cancelled that. Is there anything else I can help with? 😊",
                "suggestions": ["Show me products", "Browse categories", "No, thank you"],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }
            
    # ── State: Awaiting Filter Clarification ──
    if state == FlowState.AWAITING_FILTER_CLARIFICATION:
        # Check if they accepted the suggested filter
        if any(kw in text for kw in ["yes", "yeah", "yep", "sure", "ok", "correct", "use", "mean"]):
            return {
                "flow_state": FlowState.IDLE.value,
                "pass_through": True,
                "apply_semantic_match": True
            }
        # Check if they explicitly rejected it
        elif any(kw in text for kw in ["no", "nope", "don't", "original", "search for"]):
            return {
                "flow_state": FlowState.IDLE.value,
                "pass_through": True,
                "reject_semantic_match": True
            }
        else:
            # Context Switch! The user ignored the question and typed a completely new search.
            # silently clear the semantic state and let the new query process normally.
            return {
                "flow_state": FlowState.IDLE.value,
                "pass_through": True,
                "clear_semantic_match": True
            }

    # ── State: User is picking intent from menu ──
    if state == FlowState.AWAITING_INTENT_CHOICE:
        if any(kw in text for kw in ["product", "information", "search", "find"]):
            return {
                "bot_message": (
                    "Sure! What product or category are you looking for? "
                    "You can tell me a product name, category, or describe what you need."
                ),
                "suggestions": [
                    "Show me products",
                    "What categories do you have?",
                    "I'm looking for floor products",
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
                    "Order 5 Affogato",
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

    # ── State: Awaiting specific order ID to reorder ──
    if state == FlowState.AWAITING_REORDER_ID:
        if any(kw in text for kw in ["cancel", "stop", "nevermind", "never mind", "quit", "exit"]):
            return {
                "bot_message": "No problem! Let me know if you need anything else.",
                "suggestions": ["Show me products", "View my orders", "No, thank you"],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }

        import re
        # If they reply with "my last one", route it as an explicit last order
        if re.search(r"\b(last|recent|previous)\b", text):
            return {
                "override_message": "reorder my last order",
                "flow_state": FlowState.IDLE.value,
                "pass_through": True
            }

        # Check if they provided a number (e.g. "12345" or "#12345")
        match = re.search(r'#?\s*(\d+)', text)
        if match:
            order_num = match.group(1)
            # Override their message to be perfectly parsable by the standard pipeline!
            return {
                "override_message": f"reorder order #{order_num}",
                "flow_state": FlowState.IDLE.value,
                "pass_through": True
            }

        # They typed something unrelated
        return {
            "bot_message": "I didn't catch an order number. Please provide the order number (e.g., #12345), or say 'my last order' to reorder your most recent purchase.",
            "suggestions": ["My last order", "Cancel"],
            "flow_state": FlowState.AWAITING_REORDER_ID.value,
            "pass_through": False,
        }
    # ── State: Awaiting quantity for an order ──
    # After user provides quantity → go to cart confirmation
    if state == FlowState.AWAITING_QUANTITY:
        import re
        qty_match = re.search(r"\b(\d+)\b", text)
        if qty_match:
            quantity = int(qty_match.group(1))
            return {
                "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
                "pending_quantity": quantity,
                "pass_through": True,
            }
        else:
            return {
                "bot_message": "How many would you like to order? Please enter a number.",
                "suggestions": ["1", "5", "10", "25", "Cancel Order"],
                "flow_state": FlowState.AWAITING_QUANTITY.value,
                "pass_through": False,
            }

    # ── State: Anything else? ──
    if state == FlowState.AWAITING_ANYTHING_ELSE:
        if any(kw in text for kw in ["no", "nothing", "bye", "that's all", "done", "nope"]):
            return {
                "bot_message": f"Thank you for chatting with {BOT_NAME}! 👋 Have a great day! I'll close this chat now. Bye!",
                "suggestions": [],
                "flow_state": FlowState.CLOSING.value,
                "pass_through": False,
            }
        elif any(kw in text for kw in ["yes", "yeah", "sure"]):
            return get_disambiguation_message()
        else:
            # Treat as a new query — fall through to classifier
            return None

    # ── State: Awaiting variant selection for a variable product ──
    if state == FlowState.AWAITING_VARIANT_SELECTION:
        if any(kw in text for kw in [
            "cancel", "stop", "nevermind", "never mind",
            "exit", "quit", "back", "done", "go back",
        ]):
            return {
                "bot_message": "No problem! Order cancelled. Is there anything else I can help with?",
                "suggestions": ["Show me products", "Browse categories", "No, thank you"],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }
        # Topic-change detection: user clearly wants to do something else — reset to IDLE
        _topic_change_phrases = [
            "show me products", "show products", "browse categories",
            "what categories", "check my orders", "check orders",
        ]
        if any(ph in text for ph in _topic_change_phrases) or text.strip() in ("hello", "hi"):
            return None