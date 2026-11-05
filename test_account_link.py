"""
Tests for "change my address / email / name" in chat
(handlers/account_handler.py and its guard in routes/chat.py).

No store, no DB, no network.

Run (either works):
    python test_account_link.py
    pytest --noconftest test_account_link.py
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

import api_builder
from ecommerce.endpoints import EcommerceEndpoints
from ecommerce.shopify_endpoints import ShopifyEndpoints
from ecommerce.woo_endpoints import WooEndpoints
from handlers import account_handler
from handlers.account_handler import account_page_url, handle_customer_update
from models import Intent

SITE = "https://shop.example.com"
CONV = SimpleNamespace(id="6f1c2a9e-0000-0000-0000-000000000001")
ADDRESS = "/my-account/edit-address/"


def _reply(monkeypatch, backend="woocommerce", wp_base_url=SITE):
    monkeypatch.setattr(account_handler, "current_backend", lambda: backend)
    monkeypatch.setattr(account_handler, "get_store_loader",
                        lambda: SimpleNamespace(wp_base_url=wp_base_url))
    with Flask(__name__).test_request_context():
        resp, status = handle_customer_update(Intent.UPDATE_CUSTOMER, CONV, 1, time.time())
        assert status == 200
        return resp.get_json()


# ── the URL ──────────────────────────────────────────────────────────────────

def test_account_page_url_joins_site_and_path():
    assert account_page_url(SITE, ADDRESS) == "https://shop.example.com/my-account/edit-address/"


def test_account_page_url_adds_the_plugin_redirect_parameter():
    assert (account_page_url(SITE, ADDRESS, "edit-address")
            == "https://shop.example.com/my-account/edit-address/?miraq_account=edit-address")


def test_account_page_url_keeps_a_sub_directory_and_ignores_a_trailing_slash():
    assert account_page_url("https://wgc.net.in/hn/", ADDRESS) == "https://wgc.net.in/hn/my-account/edit-address/"


@pytest.mark.parametrize("bad", ["", None, "   ", "shop.example.com", "javascript:alert(1)"])
def test_account_page_url_is_empty_unless_the_site_is_a_real_http_url(bad):
    assert account_page_url(bad, ADDRESS) == ""


# ── WooCommerce reply ────────────────────────────────────────────────────────

def test_woo_reply_links_to_addresses_and_to_account_details(monkeypatch):
    body = _reply(monkeypatch)
    msg = body["bot_message"]

    assert ("[Edit your addresses](https://shop.example.com/my-account/edit-address/"
            "?miraq_account=edit-address)") in msg
    assert ("[Edit your name and email](https://shop.example.com/my-account/edit-account/"
            "?miraq_account=edit-account)") in msg
    assert "can't change your account details" in msg
    assert body["success"] is True
    assert body["intent"] == "update_customer"
    assert body["actions"] == [] and body["products"] == []
    assert body["flow_state"] == "idle"
    assert body["metadata"]["account_update"] == "link"
    assert "updated successfully" not in msg


def test_woo_reply_without_a_known_site_still_says_where_to_go(monkeypatch):
    body = _reply(monkeypatch, wp_base_url="")

    assert "](" not in body["bot_message"]
    assert "My Account" in body["bot_message"]


# ── Shopify keeps its answer ─────────────────────────────────────────────────

def test_shopify_gets_the_existing_not_available_answer(monkeypatch):
    body = _reply(monkeypatch, backend="shopify")

    assert "can't update your account details from chat" in body["bot_message"]
    assert "my-account" not in body["bot_message"]
    assert body["metadata"]["unsupported_on_shopify"] is True


# ── the write is gone ────────────────────────────────────────────────────────

def test_no_backend_can_build_a_customer_update_call():
    for cls in (EcommerceEndpoints, WooEndpoints, ShopifyEndpoints):
        assert not hasattr(cls, "update_customer"), cls.__name__
    assert not hasattr(api_builder, "_build_update_customer")


def test_chat_route_answers_before_building_api_calls():
    src = Path(__file__).with_name("routes").joinpath("chat.py").read_text(encoding="utf-8")
    guard = src.index("if intent == Intent.UPDATE_CUSTOMER:\n            return _ft(handle_customer_update(")
    build = src.index("api_calls = build_api_calls(")
    assert guard < build, "the account reply must come before build_api_calls"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--noconftest", "-q"]))