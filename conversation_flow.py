"""
Conversation Flow State Machine for MiraQ Chat.

Manages multi-turn flows like:
  - Intent disambiguation (when classifier is confused)
  - Guided product search → order placement
  - Quantity confirmation → order creation
"""

import re
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
    AWAITING_REORDER_ID = "awaiting_reorder_id"
    AWAITING_FILTER_CLARIFICATION = "awaiting_filter_clarification"
    AWAITING_REFINEMENT_CHOICE    = "awaiting_refinement_choice"
    AWAITING_NO_RESULTS_CHOICE    = "awaiting_no_results_choice"
    AWAITING_CART_CONFIRMATION = "awaiting_cart_confirmation"
    
    # ── Bulk order (shopper's own, into the cart) ──────────────────────
    # A stored state that no longer exists (the removed company/recipient/
    # address/confirmation steps) falls back to IDLE in routes/chat.py.
    AWAITING_BULK_ORDER_INPUT         = "awaiting_bulk_order_input"
    AWAITING_BULK_VARIANT_SELECTION   = "awaiting_bulk_variant_selection"
    AWAITING_BULK_PRODUCT             = "awaiting_bulk_product"
    AWAITING_BULK_QUANTITY = "awaiting_bulk_quantity"

_ORDER_FLOW_STATES = {
    FlowState.AWAITING_QUANTITY,
    FlowState.AWAITING_CART_CONFIRMATION,
    FlowState.AWAITING_REORDER_ID,
    FlowState.AWAITING_BULK_ORDER_INPUT,
    FlowState.AWAITING_BULK_VARIANT_SELECTION,
    FlowState.AWAITING_BULK_PRODUCT,
    FlowState.AWAITING_BULK_QUANTITY,
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


# "order 4 Zyvora …", "buy 2 …", "add 3 … to cart": a verb, a quantity, then a
# product. Requires all three, so "ok", "yes add it" or a bare "order" never match.
_NEW_ORDER_COMMAND_RE = re.compile(r"^(?:order|buy|add)\s+\d+\s*(?:x\s+)?\S")


# A new browse/search request typed while the bot waits for Yes/No on the cart
# ("Exterior category", "show me matte tiles", "browse categories"). Word
# boundaries, so "showroom" or "listing" don't count.
_NEW_SEARCH_REQUEST_RE = re.compile(
    r"\b(?:show|search|find|browse|display|list|categor(?:y|ies)|collections?|"
    r"what\s+do\s+you\s+have)\b"
)


def _is_new_search_request(text: str) -> bool:
    """True when a cart-confirmation reply is really a new search: a search
    word, or the whole message naming a store category/tag ("Exterior")."""
    if _NEW_SEARCH_REQUEST_RE.search(text):
        return True
    try:
        from classifier.utils import message_is_store_name
        from store_registry import get_store_loader
        return message_is_store_name(text, get_store_loader())
    except Exception:
        return False


def _is_topic_change(text: str) -> bool:
    """True if the message looks like the user wants to do something different."""
    return any(kw in text for kw in _TOPIC_CHANGE_KEYWORDS)


def _flow_context_message(state: FlowState) -> dict:
    """Return a state-specific nudge message with cancel instruction."""
    _hints = {
        FlowState.AWAITING_QUANTITY:                "Please enter a quantity (e.g. **5**)",
        FlowState.AWAITING_VARIANT_SELECTION:       "Please select a variant from the options above",
        FlowState.AWAITING_CART_CONFIRMATION:       "Please reply **Yes** to add to cart or **No** to skip",
        FlowState.AWAITING_BULK_PRODUCT:            "Please enter a product name",
        FlowState.AWAITING_BULK_QUANTITY:           "Please enter a quantity",
        FlowState.AWAITING_BULK_ORDER_INPUT:        "Please enter your order lines",
        FlowState.AWAITING_BULK_VARIANT_SELECTION:  "Please select a variant from the options above",
    }
    hint = _hints.get(state, "Please complete the current step")
    return {
        "bot_message": (
            f"{hint}.\n\n"
            "Say **Cancel** to exit and start a new search."
        ),
        "suggestions": _REPROMPT_SUGGESTIONS.get(state, ["Cancel"]),
        "flow_state": state.value,
        "pass_through": False,
        # Marks this as "the reply didn't answer the question", so routes/chat.py
        # can count consecutive misses and switch to flow_recap_message().
        "reprompt": True,
    }


# Buttons shown with a re-prompt. Yes/no questions get their answer buttons
# back — the old re-prompt offered only "Cancel", so a shopper who had
# scrolled past the original buttons had nothing to tap but an exit.
_REPROMPT_SUGGESTIONS = {
    FlowState.AWAITING_CART_CONFIRMATION:       ["Yes, add it", "No thanks"],
}

# How many consecutive unrecognised replies before the recap replaces the nudge.
FLOW_RECAP_AFTER_MISSES = 2


def flow_recap_message(state: FlowState, context: Optional[dict] = None) -> dict:
    """
    Shown when the shopper has answered the current question with something
    the flow can't use twice in a row. Instead of repeating the same one-line
    nudge, it recaps what the bot is waiting for, how to answer, and how to
    leave the step to do something else.
    """
    ctx = context or {}
    name = ctx.get("pending_product_name") or "this item"
    qty = ctx.get("pending_quantity") or 1
    resolved = ctx.get("resolved_attributes") or {}
    variant = " / ".join(str(v) for v in resolved.values() if v) if isinstance(resolved, dict) else ""
    item = f"**{name}**" + (f" ({variant})" if variant else "")

    if state == FlowState.AWAITING_CART_CONFIRMATION:
        lines = [
            "Just to recap, I'm still waiting for one answer before we move on:",
            "",
            f"**Add {item.replace('**', '')} ×{qty} to your cart?**",
            "",
            "• Tap **Yes, add it** to add it to your cart",
            "• Tap **No thanks** to skip it",
            "",
            "Looking for something else? Tap **No thanks** first, then type your search.",
        ]
        suggestions = ["Yes, add it", "No thanks"]
    else:
        questions = {
            FlowState.AWAITING_QUANTITY:           f"how many of {item} you'd like",
            FlowState.AWAITING_VARIANT_SELECTION:  f"which option of {item} you'd like",
            FlowState.AWAITING_REORDER_ID:         "which order you'd like to reorder",
        }
        hint = _flow_context_message(state)["bot_message"].split("\n\n")[0] \
            if state != FlowState.AWAITING_REORDER_ID else \
            "Please give the order number (e.g. **#12345**), or say **my last order**."
        lines = ["Just to recap, I'm still waiting for one answer before we move on."]
        if state in questions:
            lines += ["", f"I asked {questions[state]}."]
        lines += [
            "",
            hint,
            "",
            "Want to do something else instead? Tap **Cancel** to stop this step, "
            "then type your new request.",
        ]
        suggestions = list(_REPROMPT_SUGGESTIONS.get(state, []))
        if state == FlowState.AWAITING_REORDER_ID:
            suggestions = ["My last order"]
        if "Cancel" not in suggestions:
            suggestions.append("Cancel")

    return {
        "bot_message": "\n".join(lines),
        "suggestions": suggestions,
        "flow_state": state.value,
        "pass_through": False,
        "reprompt": True,
        "recap": True,
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
            "• **Product** - Search or get info about a product\n"
            "• **Category** - Browse a product category\n"
            "• **Order** - Place a new order or check an existing one"
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

        # A fresh "order <qty> <product>" is a new request, not an answer to
        # the pending yes/no — it is exactly what every product card's Order
        # button sends, e.g. "order 4 Zyvora - Zyvora Havana Dune / Matte /
        # 12"x12"" right after the bot offered that variant ×1. It was being
        # re-prompted ("Please reply Yes…") and the order never ran. Checked
        # BEFORE yes/no so a product name containing "no" or "ok" can't be
        # read as an answer. routes/chat.py drops the old pending item and
        # sends the message through normal classification.
        if _NEW_ORDER_COMMAND_RE.match(text):
            return {
                "flow_state": FlowState.IDLE.value,
                "pass_through": True,
                "replace_pending_order": True,
            }

        _yes = re.search(
            r'\b(yes|yeah|yep|sure|okay|ok|go\s+ahead|add\s+it|add\s+to\s+cart)\b',
            text
        )
        _no = re.search(
            r'\b(no|nope|cancel|skip|not\s+now|continue\s+shopping)\b',
            text
        )

        # A new search ("Exterior category") isn't an answer either: it was
        # re-prompted ("Please reply Yes…"), and after "No thanks" the search
        # was gone and had to be typed again. Treated as "no, show me this
        # instead": the pending item is dropped and the message classified
        # fresh. A "yes" still wins ("yes add it and show me more"), and this
        # runs before the "no" check so "show me no-slip tiles" isn't a decline.
        if not _yes and _is_new_search_request(text):
            return {
                "flow_state": FlowState.IDLE.value,
                "pass_through": True,
                "replace_pending_order": True,
            }

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
            "reprompt": True,
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
        
    # ── State: Awaiting bulk variant selection ──
    if state == FlowState.AWAITING_BULK_VARIANT_SELECTION:
        if _is_topic_change(text):
            return _flow_context_message(FlowState.AWAITING_BULK_VARIANT_SELECTION)
        return {
            "action": "process_bulk_variant_selection",
            "flow_state": FlowState.AWAITING_BULK_VARIANT_SELECTION.value,
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