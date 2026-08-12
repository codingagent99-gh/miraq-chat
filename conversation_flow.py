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
    AWAITING_REFINEMENT_CHOICE    = "awaiting_refinement_choice"
    AWAITING_NO_RESULTS_CHOICE    = "awaiting_no_results_choice"
    AWAITING_CART_CONFIRMATION = "awaiting_cart_confirmation"
    
    # ── Sales rep flows ──────────────────────────────────────────────
    AWAITING_ORDER_FOR_EMAIL          = "awaiting_order_for_email"   # renamed
    AWAITING_BULK_ORDER_INPUT         = "awaiting_bulk_order_input"
    AWAITING_BULK_ORDER_CONFIRMATION  = "awaiting_bulk_order_confirmation"
    AWAITING_BULK_ADDRESS_CONFIRMATION = "awaiting_bulk_address_confirmation"
    AWAITING_BULK_VARIANT_SELECTION   = "awaiting_bulk_variant_selection"
    AWAITING_BULK_EMAIL               = "awaiting_bulk_email"
    AWAITING_BULK_COMPANY             = "awaiting_bulk_company"
    AWAITING_BULK_RECIPIENT           = "awaiting_bulk_recipient"
    AWAITING_BULK_RECIPIENT_MODE      = "awaiting_bulk_recipient_mode"
    AWAITING_BULK_ADDRESS_CHOICE      = "awaiting_bulk_address_choice"
    AWAITING_BULK_PRODUCT             = "awaiting_bulk_product"
    AWAITING_BULK_QUANTITY = "awaiting_bulk_quantity"

    # Admin picking between several reps who match a name in an order report
    AWAITING_REP_CHOICE = "awaiting_rep_choice"

    # Rep picking which COMPANY a bulk order is for, when a fuzzy company
    # lookup matched more than one business. Always resolved BEFORE the
    # recipient question, so the person list never mixes two companies.
    AWAITING_BULK_COMPANY_CHOICE = "awaiting_bulk_company_choice"

_ORDER_FLOW_STATES = {
    FlowState.AWAITING_QUANTITY,
    FlowState.AWAITING_CART_CONFIRMATION,
    FlowState.AWAITING_ORDER_DETAIL,
    FlowState.AWAITING_REORDER_ID,
    FlowState.AWAITING_ORDER_FOR_EMAIL,
    FlowState.AWAITING_BULK_ORDER_INPUT,
    FlowState.AWAITING_BULK_ORDER_CONFIRMATION,
    FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION,
    FlowState.AWAITING_BULK_VARIANT_SELECTION,
    FlowState.AWAITING_BULK_EMAIL,
    FlowState.AWAITING_BULK_COMPANY,
    FlowState.AWAITING_BULK_RECIPIENT,
    FlowState.AWAITING_BULK_RECIPIENT_MODE,
    FlowState.AWAITING_BULK_ADDRESS_CHOICE,
    FlowState.AWAITING_BULK_PRODUCT,
    FlowState.AWAITING_BULK_QUANTITY,
    FlowState.AWAITING_BULK_COMPANY_CHOICE,
    FlowState.AWAITING_REP_CHOICE,
}

# Bare exit vocabulary. Shared by the in-flow escape hatch below and by the
# IDLE/ANYTHING_ELSE interceptor in routes/chat.py, so "cancel" means the
# same thing everywhere instead of being redefined in four places.
EXIT_PHRASES = frozenset({
    "cancel", "exit", "stop", "quit", "nevermind", "never mind",
    "abort", "start over",
})


def is_bare_exit(text: str) -> bool:
    """
    True only for a STANDALONE exit word.

    Deliberately stricter than the in-flow check, which also accepts
    "<exit word> ..." prefixes. In IDLE there is no flow to escape, so a
    longer phrase is far more likely to be a real request that merely
    starts with one of these words — "cancel my order #4405" is an order
    cancellation, not a request to reset the chat. Those must reach the
    classifier untouched.
    """
    return text.strip().lower().rstrip("!.?") in EXIT_PHRASES


# Keywords that signal a topic change away from the current flow
_TOPIC_CHANGE_KEYWORDS = {
    "show", "search", "find", "browse", "display", "list",
    "order history", "my orders", "check order", "track",
    "categories", "what do you have",
}

def is_order_flow(state: FlowState) -> bool:
    """True when state is an active order/transaction flow (not search refinement)."""
    return state in _ORDER_FLOW_STATES


def _is_topic_change(text: str) -> bool:
    """True if the message looks like the user wants to do something different."""
    return any(kw in text for kw in _TOPIC_CHANGE_KEYWORDS)


def _flow_context_message(state: FlowState) -> dict:
    """Return a state-specific nudge message with cancel instruction."""
    _hints = {
        FlowState.AWAITING_QUANTITY:                "Please enter a quantity (e.g. **5**)",
        FlowState.AWAITING_VARIANT_SELECTION:       "Please select a variant from the options above",
        FlowState.AWAITING_CART_CONFIRMATION:       "Please reply **Yes** to add to cart or **No** to skip",
        FlowState.AWAITING_BULK_EMAIL:              "Please provide a valid customer email address",
        FlowState.AWAITING_BULK_COMPANY:           "Please provide the company name for this order",
        FlowState.AWAITING_BULK_RECIPIENT:         "Please tell me who this order is for",
        FlowState.AWAITING_REP_CHOICE:             "Please pick which rep you meant",
        FlowState.AWAITING_BULK_COMPANY_CHOICE:    "Please pick which company this order is for",
        FlowState.AWAITING_BULK_RECIPIENT_MODE:    "Please say whether these are for the same person or different people",
        FlowState.AWAITING_BULK_ADDRESS_CHOICE:    "Please pick which address to ship to",
        FlowState.AWAITING_BULK_PRODUCT:            "Please enter a product name",
        FlowState.AWAITING_BULK_QUANTITY:           "Please enter a quantity",
        FlowState.AWAITING_BULK_ORDER_INPUT:        "Please enter your order lines",
        FlowState.AWAITING_BULK_ORDER_CONFIRMATION: "Please reply **Yes** to confirm or **No** to cancel",
        FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION:"Please reply **Yes**, **Change**, or **Skip**",
        FlowState.AWAITING_BULK_VARIANT_SELECTION:  "Please select a variant from the options above",
        FlowState.AWAITING_ORDER_FOR_EMAIL:         "Please provide the customer email address",
    }
    hint = _hints.get(state, "Please complete the current step")
    return {
        "bot_message": (
            f"{hint}.\n\n"
            "Say **Cancel** to exit and start a new search."
        ),
        "suggestions": ["Cancel"],
        "flow_state": state.value,
        "pass_through": False,
    }
       
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
           return _flow_context_message(FlowState.AWAITING_CART_CONFIRMATION)
            
    if state not in (FlowState.IDLE, FlowState.AWAITING_ANYTHING_ELSE):
        # Exact matches or starts-with to prevent accidental triggers 
        # (e.g., we want to catch "cancel" but not "I want to order cancela tiles")
        exit_phrases = sorted(EXIT_PHRASES)
        if text in exit_phrases or any(text.startswith(p + " ") for p in exit_phrases):
            return {
                "bot_message": "No problem! I've cancelled that. Is there anything else I can help with? 😊",
                "suggestions": ["Browse Products", "Browse categories"],
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
        if re.search(r'\b(product|information|search|find)\b', text):
            return {
                "bot_message": (
                    "Sure! What product or category are you looking for? "
                    "You can tell me a product name, category, or describe what you need."
                ),
                "suggestions": [
                    "Browse Products",
                    "What categories do you have?",
                    "I'm looking for floor products",
                ],
                "flow_state": FlowState.AWAITING_PRODUCT_OR_CATEGORY.value,
                "pass_through": False,
            }
        elif re.search(r'\b(category|categories|browse)\b', text):
            return {
                "bot_message": "Let me show you our categories!",
                "flow_state": FlowState.IDLE.value,
                "pass_through": True,
                "override_message": "show me all categories",
            }
        elif re.search(r'\b(order|place|buy|purchase)\b', text):
            return {
                "bot_message": (
                    "I can help you place an order! 🛒\n\n"
                    "Which product would you like to order? "
                    "You can tell me the product name and quantity."
                ),
                "suggestions": [
                    "I want to place an order. ",
                    "Show me my last order",
                    "Reorder my previous order",
                ],
                "flow_state": FlowState.AWAITING_PRODUCT_OR_CATEGORY.value,
                "pass_through": False,
            }
        elif re.search(r'\b(yes|yeah|ok|sure)\b|start\s+again', text):
            return get_disambiguation_message()
        else:
            return None
        
    # ── State: Awaiting specific order ID to reorder ──
    if state == FlowState.AWAITING_REORDER_ID:
        if any(kw in text for kw in ["cancel", "stop", "nevermind", "never mind", "quit", "exit"]):
            return {
                "bot_message": "No problem! Let me know if you need anything else.",
                "suggestions": ["Browse Products", "View my orders"],
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
    # After user provides quantity → emit cart-confirmation prompt
    if state == FlowState.AWAITING_QUANTITY:
        import re
        qty_match = re.search(r"\b(\d+)\b", text)
        if qty_match:
            quantity = int(qty_match.group(1))
            return {
                "action": "prompt_cart_confirmation",   # routes/chat.py builds the prompt
                "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
                "pending_quantity": quantity,
                "pass_through": False,
            }
        else:
            return _flow_context_message(FlowState.AWAITING_QUANTITY)

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
                "suggestions": ["Browse Products", "Browse categories"],
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "pass_through": False,
            }
        # Topic-change detection: user clearly wants to do something else — reset to IDLE
        _topic_change_phrases = [
            "Browse Products", "show products", "browse categories",
            "what categories", "check my orders", "check orders",
        ]
        if any(ph in text for ph in _topic_change_phrases) or _is_topic_change(text) or text.strip() in ("hello", "hi"):
            return _flow_context_message(FlowState.AWAITING_VARIANT_SELECTION)
        
    # ── State: Awaiting bulk order confirmation (Yes/No) ──
    if state == FlowState.AWAITING_BULK_ORDER_CONFIRMATION:
        _yes = re.search(r'\b(yes|yeah|yep|sure|confirm|ok|okay|go\s+ahead|proceed)\b', text)
        _no  = re.search(r'\b(no|nope|abort|never\s+mind)\b', text)
        if _yes:
            return {
                "action": "confirm_bulk_order",
                "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
                "pass_through": False,
            }
        elif _no:
            return {
                "action": "cancel_bulk_order",
                "flow_state": FlowState.IDLE.value,
                "pass_through": False,
            }
        else:
            return {
                "action": "bulk_confirmation_unclear",
                "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
                "pass_through": False,
            }

    # ── State: Awaiting bulk address confirmation ──
    if state == FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION:
        # Structured save from the inline edit panel: message is
        # "__BULK_ADDR__<json>" carrying edited billing + shipping blocks.
        # Check the RAW message (not lowercased text) so the prefix and the
        # JSON payload are preserved exactly.
        if message.strip().startswith("__BULK_ADDR__"):
            return {
                "action": "bulk_address_override_structured",
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
                "pass_through": False,
            }
        # Sub-state: user was prompted to type a new address (legacy text path)
        if entities.get("bulk_awaiting_address_text"):
            return {
                "action": "bulk_address_override_text",
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
                "pass_through": False,
            }
        _yes    = re.search(r'\b(yes|yeah|yep|sure|confirm|ok|okay|use|correct)\b', text)
        _change = re.search(r'\b(change|update|different|new|edit|modify|type)\b', text)
        _skip   = re.search(r'\b(skip|next|later)\b', text)
        if _yes:
            return {
                "action": "bulk_address_confirmed",
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
                "pass_through": False,
            }
        elif _change:
            return {
                "action": "bulk_address_change",
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
                "pass_through": False,
            }
        elif _skip:
            return {
                "action": "bulk_address_skip",
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
                "pass_through": False,
            }
        else:
            return {
                "bot_message": "Please reply **Yes** to use the address on file, **Change** to type a new one, or **Skip** to proceed without an override.",
                "suggestions": ["Yes, use it", "Change address", "Skip"],
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
                "pass_through": False,
            }

    # ── State: Awaiting bulk variant selection ──
    if state == FlowState.AWAITING_BULK_VARIANT_SELECTION:
        if _is_topic_change(text):
            return _flow_context_message(FlowState.AWAITING_BULK_VARIANT_SELECTION)
        return {
            "action": "process_bulk_variant_selection",
            "flow_state": FlowState.AWAITING_BULK_VARIANT_SELECTION.value,
            "pass_through": False,
        }
        
    # ── State: Awaiting customer email for order-for flow (rep only) ──
    if state == FlowState.AWAITING_ORDER_FOR_EMAIL:
        return {
            "action":     "resolve_order_for_email",
            "flow_state": FlowState.AWAITING_ORDER_FOR_EMAIL.value,
            "pass_through": False,
        }
    
    # ── State: Awaiting which of a person's addresses to ship to ──
    if state == FlowState.AWAITING_BULK_ADDRESS_CHOICE:
        if not message.strip():
            return _flow_context_message(FlowState.AWAITING_BULK_ADDRESS_CHOICE)
        return {
            "action": "process_bulk_address_choice_reply",
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
            "pass_through": False,
        }

    # ── State: Awaiting "same person" vs "different people" for unnamed lines ──
    if state == FlowState.AWAITING_BULK_RECIPIENT_MODE:
        if not message.strip():
            return _flow_context_message(FlowState.AWAITING_BULK_RECIPIENT_MODE)
        return {
            "action": "process_bulk_recipient_mode_reply",
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT_MODE.value,
            "pass_through": False,
        }

    # ── State: Awaiting the person this bulk order ships to ──
    if state == FlowState.AWAITING_BULK_RECIPIENT:
        if not message.strip():
            return _flow_context_message(FlowState.AWAITING_BULK_RECIPIENT)
        return {
            "action": "process_bulk_recipient_reply",
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
            "pass_through": False,
        }

    # ── State: Rep choosing which matching company the bulk order is for ──
    if state == FlowState.AWAITING_BULK_COMPANY_CHOICE:
        if not message.strip():
            return _flow_context_message(FlowState.AWAITING_BULK_COMPANY_CHOICE)
        return {
            "action": "process_bulk_company_choice_reply",
            "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
            "pass_through": False,
        }

    # ── State: Admin choosing which of several matching reps to report on ──
    if state == FlowState.AWAITING_REP_CHOICE:
        if not message.strip():
            return _flow_context_message(FlowState.AWAITING_REP_CHOICE)
        return {
            "action": "process_rep_choice_reply",
            "flow_state": FlowState.AWAITING_REP_CHOICE.value,
            "pass_through": False,
        }

    # ── State: Awaiting company name for bulk order scope resolution ──
    if state == FlowState.AWAITING_BULK_COMPANY:
        if not message.strip():
            return _flow_context_message(FlowState.AWAITING_BULK_COMPANY)
        return {
            "action": "process_bulk_company_reply",
            "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
            "pass_through": False,
        }

    # ── State: Awaiting email address for bulk order customer resolution ──
    if state == FlowState.AWAITING_BULK_EMAIL:
        import re as _re
        _EMAIL_RE = _re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', _re.I)
        emails = _EMAIL_RE.findall(message)
        if emails:
            return {
                "action": "process_bulk_email_reply",
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                "pass_through": False,
            }
        else:
            return {
                "bot_message": "Please provide a valid email address to look up the customer.",
                "suggestions": [],
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                "pass_through": False,
            }
    # ── State: Awaiting product name for bulk order line ──
    if state == FlowState.AWAITING_BULK_PRODUCT:
        # Any non-empty reply is treated as a product + optional quantity description.
        # The global escape hatch above already handles cancel/exit.
        if _is_topic_change(text):
            return _flow_context_message(FlowState.AWAITING_BULK_PRODUCT)
        return {
            "action": "process_bulk_product_reply",
            "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
            "pass_through": False,
        }
        
    # ── State: Awaiting bulk order lines (after trigger prompt) ──
    if state == FlowState.AWAITING_BULK_ORDER_INPUT:
        if _is_topic_change(text):
            return _flow_context_message(FlowState.AWAITING_BULK_ORDER_INPUT)
        return {
            "action":     "process_bulk_input",
            "flow_state": FlowState.AWAITING_BULK_ORDER_INPUT.value,
            "pass_through": False,
        }
        
    if state == FlowState.AWAITING_BULK_QUANTITY:
        if _is_topic_change(text):
            return _flow_context_message(FlowState.AWAITING_BULK_QUANTITY)
        return {
            "action": "process_bulk_quantity_reply",
            "flow_state": FlowState.AWAITING_BULK_QUANTITY.value,
            "pass_through": False,
        }