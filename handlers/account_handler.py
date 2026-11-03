"""
handlers/account_handler.py - "change my address / email / name" in chat.

The chat never edits a customer account. It used to send
PUT /wc/v3/customers/{id} with the store's REST key; that call no longer
exists anywhere in the backend.

WooCommerce: the shopper gets links to My Account -> Addresses and
My Account -> Account details, where WooCommerce validates and saves the
change under their own login. (The Store API's customer update is not a
substitute: it only changes the billing/shipping details on the CART for
checkout, not the account.)

Shopify: the existing "not available here" answer from ecommerce/unsupported.py.
"""

import time

from flask import jsonify

from app_config import WOO_EDIT_ADDRESS_PATH, WOO_EDIT_ACCOUNT_PATH
from chat_logger import get_logger
from conversation_flow import FlowState
from ecommerce.unsupported import message_for as unsupported_message_for
from handlers.chat_utils import default_pagination
from platform_config import current_backend
from store_registry import get_store_loader

logger = get_logger("miraq_chat")


def account_page_url(wp_base_url, path):
    """A My Account page on this store, or "" when the site URL is unusable.

    ``wp_base_url`` is the WordPress root and may include a sub-directory
    (https://example.com/shop).
    """
    base = str(wp_base_url or "").strip().rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        return ""
    tail = str(path or "").strip().strip("/")
    return f"{base}/{tail}/" if tail else f"{base}/"


def _woo_message(wp_base_url):
    address_url = account_page_url(wp_base_url, WOO_EDIT_ADDRESS_PATH)
    account_url = account_page_url(wp_base_url, WOO_EDIT_ACCOUNT_PATH)
    if not (address_url and account_url):
        return (
            "I can't change your account details from chat. You can update them "
            "yourself on the store under My Account → Addresses (billing and "
            "delivery) or My Account → Account details (name and email)."
        ), False
    return (
        "I can't change your account details from chat, but you can update them "
        "yourself in your account:\n\n"
        f"- [Edit your addresses]({address_url}) — billing and delivery\n"
        f"- [Edit your name and email]({account_url})"
    ), True


def handle_customer_update(intent, conversation, page, start_time):
    """Answer an UPDATE_CUSTOMER request without touching the account."""
    metadata = {"response_time_ms": round((time.time() - (start_time or time.time())) * 1000)}

    if current_backend() == "shopify":
        bot_message, suggestions = unsupported_message_for(intent)
        metadata["unsupported_on_shopify"] = True
    else:
        loader = get_store_loader()
        bot_message, linked = _woo_message(getattr(loader, "wp_base_url", "") if loader else "")
        suggestions = ["Show my orders", "Browse products"]
        metadata["account_update"] = "link"
        # Nothing the shopper typed (an address, an email) is logged or echoed back.
        logger.info(f"Account update requested in chat - sent to My Account | link={'yes' if linked else 'no'}")

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": intent.value,
        "products": [],
        "suggestions": suggestions,
        "session_id": str(conversation.id),
        "metadata": metadata,
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
        "actions": [],
    }), 200