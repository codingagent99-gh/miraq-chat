"""
ecommerce/unsupported.py — Graceful degradation for intents that have no
Shopify implementation.

Why this exists
───────────────
``ShopifyEndpoints`` returns ``WooAPICall(surface="shopify_admin")`` stubs for
~25 methods that were never wired to an executor ("Phase D"). Nothing
dispatches that surface, so those calls used to fall through to
``woo_client``, which resolves the stub's placeholder endpoint against
``WOO_BASE_URL`` — i.e. a Shopify deployment firing HTTP requests at a
WooCommerce store.

Two layers prevent that now:

1. **Hard backstop** — ``woo_client.execute()`` refuses to issue *any* request
   when ``ECOMMERCE_BACKEND == "shopify"``. That is the single true choke
   point: ~25 call sites across handlers, parsers and routes call
   ``woo_client`` directly, bypassing ``chat.py::_execute_api_calls``
   entirely, and ``execute_all()`` delegates to ``execute()``. Guarding there
   makes the "no WooCommerce request from a Shopify deployment" guarantee
   structural rather than a property of every caller remembering to check.

2. **Friendly messaging** — this module. ``chat.py`` checks the built calls
   *before* execution and, when the turn depends on an unsupported surface,
   answers with a clear, human explanation instead of letting the user watch a
   request fail into a generic "no results" reply.

Scope note: the supported set is deliberately narrow (core shopper flows —
search/browse, cart, checkout, order history/tracking/reorder). Everything
else degrades here rather than half-working. Upgrading an intent later means
implementing it and removing its entry — no other change required.
"""

from models import Intent

# Surfaces that have no Shopify executor. Any call carrying one of these on a
# Shopify deployment cannot be fulfilled.
UNSUPPORTED_SURFACES = frozenset({"shopify_admin", "custom_plugin", "admin"})

_DEFAULT_MESSAGE = (
    "Sorry — I can't help with that on this store just yet. "
    "I can search the catalogue, help you with your cart and checkout, "
    "and look up your orders."
)

_DEFAULT_SUGGESTIONS = ["Browse products", "Show my orders", "View cart"]

# Intent → (message, suggestions). Written to be honest about the limitation
# and immediately useful — every message offers a route the user CAN take.
_MESSAGES = {
    Intent.COUPON_INQUIRY: (
        "I can't look up coupon codes on this store. If you have a discount "
        "code, you'll be able to enter it at checkout.",
        ["Browse products", "View cart", "Checkout"],
    ),
    Intent.DISCOUNT_INQUIRY: (
        "I can't pull up the full list of discounted products on this store. "
        "I can still search the catalogue for you, and any active discount "
        "will be applied at checkout.",
        ["Browse products", "Show me the catalogue"],
    ),
    Intent.CLEARANCE_PRODUCTS: (
        "I can't browse clearance items on this store. I can search the "
        "catalogue for something specific though.",
        ["Browse products", "Show me the catalogue"],
    ),
    Intent.PROMOTIONS: (
        "I can't list current promotions on this store. Any active offer will "
        "still apply at checkout.",
        ["Browse products", "View cart"],
    ),
    Intent.SAVE_FOR_LATER: (
        "I can't save items to a wishlist on this store. I can add anything "
        "you like to your cart instead.",
        ["Browse products", "View cart"],
    ),
    Intent.WISHLIST: (
        "I can't open a wishlist on this store. I can add items to your cart "
        "instead.",
        ["Browse products", "View cart"],
    ),
    Intent.UPDATE_CUSTOMER: (
        "I can't update your account details from chat on this store. You can "
        "change them in your account settings, and you'll be able to enter or "
        "edit your delivery address during checkout.",
        ["Browse products", "View cart", "Checkout"],
    ),
    Intent.FETCH_CUSTOMER: (
        "I can't pull up your account details on this store. You'll find them "
        "in your account settings.",
        ["Browse products", "Show my orders"],
    ),
    Intent.PRODUCT_TYPES: (
        "I can't list product types on this store, but I can show you the "
        "categories or search for something specific.",
        ["Show me the catalogue", "Browse products"],
    ),
    Intent.BULK_ORDER: (
        "Bulk ordering isn't available on this store. I can add items to your "
        "cart one at a time.",
        ["Browse products", "View cart"],
    ),
}


def find_unsupported_call(api_calls):
    """Return the first call that cannot be executed on Shopify, else None.

    Only meaningful on a Shopify deployment — callers must check the backend
    first (see ``chat.py``). Kept backend-agnostic here so the policy stays
    testable in isolation.
    """
    for call in api_calls or []:
        if getattr(call, "surface", "") in UNSUPPORTED_SURFACES:
            return call
    return None


def message_for(intent):
    """Return (message, suggestions) for an unsupported intent."""
    return _MESSAGES.get(intent, (_DEFAULT_MESSAGE, list(_DEFAULT_SUGGESTIONS)))