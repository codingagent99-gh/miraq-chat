"""
Tests for the shopper's orders going through the plugin's /my-orders routes
with the shopper's identity token instead of the WooCommerce key
(ecommerce.woo_endpoints, woo_client.execute / execute_all, identity,
routes.welcome).

No network: the HTTP send function is replaced with a fake store.

Run (either works):
    python test_my_orders.py
    pytest --noconftest test_my_orders.py
"""

import json
import time
from types import SimpleNamespace

import pytest
import requests
from flask import Flask, g

import identity as identity_mod
import woo_client as wc_mod
from ecommerce.woo_endpoints import WooEndpoints


def _resp(status, body, headers=None):
    r = requests.models.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.headers["Content-Type"] = "application/json"
    r.headers.update(headers or {})
    r.url = "https://shop.example.com/x"
    return r


class FakeStore:
    def __init__(self):
        self.calls = []

    def __call__(self, session, method, url, *, state=None, headers=None, auth=None, params=None, **kw):
        self.calls.append({"url": url, "headers": dict(headers or {}), "auth": auth, "params": dict(params or {})})
        if url.rstrip("/").endswith("/my-orders"):
            return _resp(200, [{"id": 11, "customer_id": 7}], {"X-WP-Total": "1", "X-WP-TotalPages": "1"})
        return _resp(200, {"id": 11, "customer_id": 7})


def _loader():
    return SimpleNamespace(base="https://shop.example.com/wp-json/wc/v3",
                           custom_api_base="https://shop.example.com/wp-json/miraq/v1",
                           consumer_key="ck_test", consumer_secret="cs_test",
                           tenant_id="t1", license_id="L1", http=None, ecommerce_backend="woocommerce")


# ── endpoints ────────────────────────────────────────────────────────────────

def test_order_calls_go_to_the_plugin_my_orders_routes():
    ep = WooEndpoints()
    lst = ep.list_customer_orders(customer_id="CURRENT_USER_ID", page=2, per_page=5, after="2026-01-01T00:00:00")
    one = ep.fetch_order(order_id=42)
    assert (lst.endpoint, lst.surface) == ("/my-orders", "custom_plugin")
    assert (one.endpoint, one.surface) == ("/my-orders/42", "custom_plugin")
    assert lst.params == {"per_page": 5, "page": 2, "after": "2026-01-01T00:00:00"}
    for call in (lst, one):
        assert wc_mod.is_shopper_route(call.endpoint) and not wc_mod.is_public_route(call.endpoint)


def test_shopper_route_matching():
    assert wc_mod.is_shopper_route("/my-orders")
    assert wc_mod.is_shopper_route("/my-orders/5")
    assert wc_mod.is_shopper_route("https://s/wp-json/miraq/v1/my-orders?page=1")
    assert not wc_mod.is_shopper_route("/orders")
    assert not wc_mod.is_shopper_route("/catalog/products")


# ── woo_client ───────────────────────────────────────────────────────────────

def test_order_list_sends_the_identity_token_and_never_the_key(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    call = WooEndpoints().list_customer_orders(customer_id=7, page=1, per_page=5)
    out = wc_mod.woo_client.execute(call, loader=_loader(), identity_token="tok123")

    assert out["success"] is True
    sent = store.calls[0]
    assert sent["url"] == "https://shop.example.com/wp-json/miraq/v1/my-orders"
    assert sent["headers"] == {"X-MiraQ-Identity": "tok123"}
    assert sent["auth"] is None
    assert "customer" not in sent["params"]


def test_single_order_sends_the_identity_token(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    wc_mod.woo_client.execute(WooEndpoints().fetch_order(order_id=11), loader=_loader(), identity_token="tok")
    assert store.calls[0]["url"].endswith("/miraq/v1/my-orders/11")
    assert store.calls[0]["headers"] == {"X-MiraQ-Identity": "tok"} and store.calls[0]["auth"] is None


def test_guest_makes_no_order_request(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    out = wc_mod.woo_client.execute(WooEndpoints().fetch_order(order_id=11), loader=_loader(), identity_token="")
    assert store.calls == []
    assert out["success"] is False and out["error"] == "login_required"


def test_token_defaults_to_the_current_request_identity(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    app = Flask(__name__)
    with app.app_context():
        g.identity = identity_mod.Identity(customer_id="7", role="customer", source="wp_token", token="from_g")
        wc_mod.woo_client.execute(WooEndpoints().fetch_order(order_id=11), loader=_loader())
    assert store.calls[0]["headers"] == {"X-MiraQ-Identity": "from_g"}


def test_execute_all_passes_the_token_to_worker_threads(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    monkeypatch.setattr(wc_mod, "get_store_loader", _loader)
    app = Flask(__name__)
    ep = WooEndpoints()
    with app.app_context():
        g.identity = identity_mod.Identity(customer_id="7", role="customer", source="wp_token", token="tok_all")
        wc_mod.woo_client.execute_all([ep.fetch_order(order_id=1), ep.fetch_order(order_id=2),
                                       ep.list_customer_orders(customer_id=7, page=1)])
    assert len(store.calls) == 3
    assert all(c["headers"] == {"X-MiraQ-Identity": "tok_all"} for c in store.calls)


def test_other_plugin_routes_never_get_the_token(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    wc_mod.woo_client.execute(WooEndpoints().fetch_product(product_id=5), loader=_loader(), identity_token="tok")
    assert "X-MiraQ-Identity" not in store.calls[0]["headers"]


# ── identity ─────────────────────────────────────────────────────────────────

def test_verified_identity_keeps_the_token_but_does_not_print_it():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    now = int(time.time())
    sk = Ed25519PrivateKey.generate()
    token = identity_mod.sign_woo_identity_v2(
        {"v": 2, "lid": "L1", "cid": 7, "role": "customer", "email": "a@b.c", "iat": now, "exp": now + 3600},
        sk,
    )
    ident = identity_mod.verify_woo_identity(token, public_key=sk.public_key(), license_id="L1")
    assert ident.customer_id == "7" and ident.token == token
    assert token not in repr(ident)
    assert identity_mod.GUEST.token == ""


# ── welcome screen worker ────────────────────────────────────────────────────

def test_buy_again_worker_sees_the_shoppers_identity(monkeypatch):
    import routes.welcome as welcome
    seen = {}

    def fake_buy_again(loader, customer_id):
        from identity import current_identity
        seen["token"] = current_identity().token
        return []

    monkeypatch.setattr(welcome, "buy_again", fake_buy_again)
    app = Flask(__name__)
    with app.app_context():
        g.identity = identity_mod.Identity(customer_id="7", role="customer", source="wp_token", token="tok_w")
        welcome._start_buy_again(_loader(), "7").result(timeout=5)
    assert seen["token"] == "tok_w"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--noconftest", "-q"]))
