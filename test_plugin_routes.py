"""
Tests for calling the public plugin routes without the WooCommerce key
(woo_client.is_public_route / execute, store_loader.fetcher, http_profiles).
The key is never sent to these routes and there is no retry with it.

No network: the HTTP send functions are replaced with a fake store.

Run (either works):
    python test_plugin_routes.py
    pytest --noconftest test_plugin_routes.py
"""

import json
from types import SimpleNamespace

import pytest
import requests

import woo_client as wc_mod
from models import WooAPICall
from store_loader import fetcher

MISSING_CREDS = {"code": "unauthorized", "message": "Missing API credentials.", "data": {"status": 401}}


def _resp(status, body, headers=None):
    r = requests.models.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.headers["Content-Type"] = "application/json"
    r.headers.update(headers or {})
    r.url = "https://shop.example.com/x"
    return r


class FakeStore:
    """Answers like a store on plugin 1.0.7: public routes open, others need the key."""

    def __init__(self, reject_all=False):
        self.reject_all, self.calls = reject_all, []

    def __call__(self, session, method, url, *, state=None, headers=None, auth=None, **kw):
        sent_key = bool(headers and headers.get("X-Consumer-Key")) or auth is not None
        self.calls.append({"method": method, "url": url, "key": sent_key})
        public = wc_mod.is_public_route(url)
        if self.reject_all or (not public and not sent_key):
            return _resp(401, MISSING_CREDS)
        if url.endswith("/catalog/currency"):
            return _resp(200, {"code": "INR", "name": "Indian rupee", "symbol": "\u20b9"})
        if "/catalog/" in url:
            items = {"categories": [{"id": 1, "slug": "tiles"}], "tags": [{"id": 2, "slug": "new"}],
                     "products": [{"id": 7, "name": "Tara", "status": "publish"}]}[url.rsplit("/", 1)[-1]]
            return _resp(200, items, {"X-WP-Total": str(len(items)), "X-WP-TotalPages": "1"})
        if url.endswith("/catalog-version"):
            return _resp(200, {"version": "abc123", "product_count": 5, "term_count": 9, "last_modified": ""})
        if url.endswith("/branding") or url.endswith("/data"):
            return _resp(200, {"image_url": "https://s/logo.png", "text": "Hi"})
        if url.endswith("/all-attributes"):
            return _resp(200, [{"attribute_id": 3, "terms": [{"id": 1, "slug": "ash"}]}])
        return _resp(200, {"products": [{"id": 7, "name": "Tara"}], "total": 1, "pages": 1})


def _loader():
    return SimpleNamespace(base="https://shop.example.com/wp-json/wc/v3",
                           custom_api_base="https://shop.example.com/wp-json/miraq/v1",
                           consumer_key="ck_test", consumer_secret="cs_test",
                           tenant_id="t1", http=None, ecommerce_backend="woocommerce")


def _search_call():
    return WooAPICall(method="POST", endpoint="/products-advanced-new", params={},
                      body={"page": 1, "per_page": 4}, surface="custom_plugin",
                      description="search")


# ── is_public_route ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("ep, public", [
    ("/products-advanced-new", True),
    ("https://s.example.com/wp-json/miraq/v1/all-attributes", True),
    ("https://s.example.com/wp-json/custom-api/v1/all-attributes/", True),
    ("/catalog-version", True), ("/branding", True),
    ("/catalog/products", True), ("/catalog/products/123", True),
    ("/catalog/products/123/variations/9", True), ("/catalog/categories", True),
    ("/catalog/currency", True), ("https://s.example.com/wp-json/miraq/v1/catalog/tags?page=2", True),
    ("/customers/12", False), ("/coupons", False),
    ("/orders", False), ("/saved-addresses", False),
    ("/products-advanced-new-old", False),
])
def test_is_public_route(ep, public):
    assert wc_mod.is_public_route(ep) is public


# ── woo_client ───────────────────────────────────────────────────────────────

def test_search_goes_out_without_the_key_on_an_updated_plugin(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    out = wc_mod.woo_client.execute(_search_call(), loader=_loader())

    assert out["success"] is True and out["data"][0]["id"] == 7
    assert [c["key"] for c in store.calls] == [False]


def test_a_rejected_public_request_is_not_retried_with_the_key(monkeypatch):
    store = FakeStore(reject_all=True)
    monkeypatch.setattr(wc_mod, "http_send", store)
    out = wc_mod.woo_client.execute(_search_call(), loader=_loader())

    assert [c["key"] for c in store.calls] == [False]
    assert out.get("success") is not True


def test_no_call_ever_carries_the_key(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    call = WooAPICall(method="GET", endpoint="/not-a-public-route", params={}, body={},
                      surface="custom_plugin", description="private route")
    wc_mod.woo_client.execute(call, loader=_loader())

    assert [c["key"] for c in store.calls] == [False]


def test_a_wc_v3_call_is_refused_without_reaching_the_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    call = WooAPICall(method="GET", endpoint="/orders", params={}, surface="admin", description="old")
    out = wc_mod.woo_client.execute(call, loader=_loader())

    assert store.calls == []
    assert out["success"] is False and out["error"] == "wc_v3_not_available"


# ── store loader ─────────────────────────────────────────────────────────────

def test_catalogue_attributes_fetch_is_keyless(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(fetcher, "send", store)
    monkeypatch.setattr(fetcher, "fetch_currency_symbol", lambda *a, **k: "$")
    monkeypatch.setattr(fetcher, "fetch_all_pages", lambda *a, **k: [])
    monkeypatch.setattr(fetcher, "fetch_all_pages_with_total", lambda *a, **k: ([], 0))
    out = fetcher.load_from_live_api(None, "https://shop.example.com/wp-json/wc/v3",
                                     "https://shop.example.com/wp-json/miraq/v1")

    attr_calls = [c["key"] for c in store.calls if c["url"].endswith("/all-attributes")]
    assert attr_calls == [False]
    assert out["all_attributes_raw"][0]["attribute_id"] == 3
    assert out["attribute_terms"] == {3: [{"id": 1, "slug": "ash"}]}


def test_catalogue_version_poll_is_keyless(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(fetcher, "send", store)
    version = fetcher.fetch_catalog_version(None, "https://shop.example.com/wp-json/miraq/v1")

    assert version == "abc123"
    assert [c["key"] for c in store.calls] == [False]


# ── catalogue reads: every call goes to the public /catalog routes ──────────

def test_catalogue_endpoints_point_at_the_public_plugin_routes():
    from ecommerce.woo_endpoints import WooEndpoints
    ep = WooEndpoints()
    calls = {
        "categories": ep.list_categories(page=1),
        "tags": ep.list_tags(page=1),
        "published": ep.list_published_products(page=1),
        "product": ep.fetch_product(product_id=5),
        "variant": ep.fetch_variant(product_id=5, variant_id=9),
        "variants": ep.list_variants(product_id=5, status="publish"),
        "on_sale": ep.list_products_on_sale(page=1, per_page=4),
        "search": ep.search_products(search_term="tara", page=1, per_page=4),
        "terms": ep.list_attribute_terms(attribute_id=3),
        "currency": ep.fetch_currency(),
    }
    expected = {
        "categories": "/catalog/categories", "tags": "/catalog/tags", "published": "/catalog/products",
        "product": "/catalog/products/5", "variant": "/catalog/products/5/variations/9",
        "variants": "/catalog/products/5/variations", "on_sale": "/catalog/products",
        "search": "/catalog/products", "terms": "/catalog/attributes/3/terms", "currency": "/catalog/currency",
    }
    for name, call in calls.items():
        assert call.endpoint == expected[name], name
        assert call.surface == "custom_plugin", name
        assert wc_mod.is_public_route(call.endpoint), name
    # Same query parameters as before.
    assert calls["categories"].params["hide_empty"] is True
    assert calls["variants"].params["status"] == "publish"
    assert calls["search"].params["search"] == "tara" and calls["on_sale"].params["on_sale"] == "true"


def test_catalogue_variation_fetch_goes_out_without_the_key(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(wc_mod, "http_send", store)
    from ecommerce.woo_endpoints import WooEndpoints
    wc_mod.woo_client.execute(WooEndpoints().fetch_variant(product_id=5, variant_id=9), loader=_loader())

    assert store.calls[0]["url"].endswith("/wp-json/miraq/v1/catalog/products/5/variations/9")
    assert store.calls[0]["key"] is False


def test_full_catalogue_load_is_keyless_and_reads_the_catalog_routes(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(fetcher, "send", store)
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)
    out = fetcher.load_from_live_api(None, "https://shop.example.com/wp-json/wc/v3",
                                     "https://shop.example.com/wp-json/miraq/v1")

    urls = [c["url"] for c in store.calls]
    for path in ("/catalog/currency", "/all-attributes", "/catalog/categories", "/catalog/tags", "/catalog/products"):
        assert any(u.endswith(path) for u in urls), path
    assert not any("/wc/v3/" in u for u in urls)             # nothing reads /wc/v3 any more
    assert all(c["key"] is False for c in store.calls)       # and nothing sends the key
    assert out["products"][0]["name"] == "Tara"
    assert out["categories"][0]["slug"] == "tiles" and out["tags"][0]["slug"] == "new"
    assert out["currency_symbol"] == "\u20b9"


def test_similar_products_route_reads_the_catalog_routes(monkeypatch):
    from flask import Flask
    from routes import products as products_route
    seen = []

    def execute(call, loader=None):
        seen.append((call.endpoint, call.surface))
        if call.endpoint == "/catalog/products/21":
            return {"success": True, "data": {"id": 21, "type": "variation", "parent_id": 20}}
        if call.endpoint == "/catalog/products/20":
            return {"success": True, "data": {"id": 20, "type": "variable", "related_ids": [5, 6],
                                               "meta_data": [{"key": "_recommended_products", "value": ["7"]}]}}
        return {"success": True, "data": []}

    monkeypatch.setattr(products_route.woo_client, "execute", execute)
    with Flask(__name__).test_request_context():
        products_route.get_similar_products.__wrapped__(21) if hasattr(products_route.get_similar_products, "__wrapped__") \
            else products_route.get_similar_products(21)

    assert seen[:3] == [("/catalog/products/21", "custom_plugin"), ("/catalog/products/20", "custom_plugin"),
                        ("/catalog/products", "custom_plugin")]


# ── widget branding ──────────────────────────────────────────────────────────

def test_branding_is_fetched_without_any_key(monkeypatch):
    import widget_branding as wb
    store, saved = FakeStore(), {}
    monkeypatch.setattr(wb, "http_send", store)
    monkeypatch.setattr(wb, "_http_state_for", lambda t: None)
    monkeypatch.setattr(wb, "detect_plugin_namespace", lambda *a, **k: "miraq/v1")
    monkeypatch.setattr(wb, "_sync_loader_namespace", lambda *a, **k: None)
    monkeypatch.setattr(wb, "store_widget_branding", lambda t, **kw: saved.update(kw))
    tenant = SimpleNamespace(ecommerce_backend="woocommerce", wp_base_url="https://shop.example.com",
                             license_id="L1")

    assert wb.fetch_and_store_widget_branding(tenant) is True
    assert saved == {"image_url": "https://s/logo.png", "text": "Hi"}
    assert [c["key"] for c in store.calls] == [False]


# ── firewall probe ───────────────────────────────────────────────────────────

def test_probe_sends_nothing_with_a_key_and_never_calls_wc_v3():
    import http_profiles
    sent = []

    class Session:
        def request(self, method, url, headers=None, auth=None, **kw):
            sent.append((url, bool(headers and headers.get("X-Consumer-Key")) or auth is not None))
            return _resp(200, {})

    result = http_profiles.probe_profiles(state=http_profiles.HttpProfileState(tenant_id="t1"),
                                          custom_api_base="https://s/wp-json/miraq/v1", session=Session())
    assert result.chosen
    assert [u.rsplit("/", 1)[-1] for u, _ in sent] == ["catalog-version", "products-advanced-new"]
    assert not any(key for _, key in sent)
    assert not any("/wc/v3/" in u for u, _ in sent)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--noconftest", "-q"]))
