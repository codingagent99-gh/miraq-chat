"""
Unit tests for the WooCommerce reorder-by-cart change
(handlers/order_handler.handle_reorder -> _reorder_via_cart).

No store, no DB, no network: the stock check, the variation lookup and the
store loader are patched.

Run (either works; --noconftest skips the live-store conftest):
    python test_reorder_cart.py
    pytest --noconftest test_reorder_cart.py

The tests force the WooCommerce branch themselves, so the backend set in
your .env does not matter.
"""

import time
from types import SimpleNamespace

import pytest
from flask import Flask, g

import ecommerce
from handlers import order_handler
from identity import Identity
from models import Intent

CUSTOMER = "42"
SESSION = "6f1c2a9e-0000-0000-0000-000000000001"


@pytest.fixture
def app():
    return Flask(__name__)


def _order(items, customer_id=CUSTOMER):
    return {"id": 900, "number": "900", "customer_id": customer_id, "line_items": items}


def _line(pid, qty=1, name="Tile", vid=0, meta=None):
    line = {"product_id": pid, "quantity": qty, "name": name, "variation_id": vid}
    if meta is not None:
        line["meta_data"] = [{"id": i, "key": k, "value": v} for i, (k, v) in enumerate(meta.items())]
    return line


def _entities():
    return SimpleNamespace(order_id=None, explicit_last_order=True)


@pytest.fixture
def woo(monkeypatch):
    """Patch everything handle_reorder reaches outside itself. Returns a recorder."""
    rec = SimpleNamespace(executed=[], var_payloads={}, oos=set(), required={}, var_lookups=[],
                          stock_batches=[])

    def check_stock(**kw):
        rec.stock_batches.append(list(kw["product_ids"]))
        return SimpleNamespace(kind="check_stock", ids=kw["product_ids"])

    def create_order(**kw):
        return SimpleNamespace(kind="create_order", payload=kw["payload"])

    def execute(call):
        rec.executed.append(call.kind)
        if call.kind == "check_stock":
            return {"success": True, "data": {"products": [
                {"id": pid, "stock_status": "outofstock" if pid in rec.oos else "instock",
                 "variations": []}
                for pid in call.ids]}}
        return {"success": True, "data": {"id": 901, "number": "901"}}

    def payload(*, product_id, variant_id, resolved_attrs, store_loader):
        rec.var_lookups.append(variant_id)
        return rec.var_payloads.get(variant_id, [])

    monkeypatch.setattr(ecommerce.endpoints, "check_stock", check_stock, raising=False)
    monkeypatch.setattr(ecommerce.endpoints, "create_order", create_order, raising=False)
    monkeypatch.setattr(ecommerce.endpoints, "build_cart_variation_payload", payload, raising=False)
    # Parent axes per product; None ("unknown") unless a test sets them, so the
    # too-few-axes guard only fires where a test asks for it.
    monkeypatch.setattr(ecommerce.endpoints, "product_variation_taxonomies",
                        lambda pid, *a, **k: rec.required.get(pid), raising=False)
    monkeypatch.setattr(order_handler.woo_client, "execute", execute)
    monkeypatch.setattr("store_registry.get_store_loader", lambda: None)
    # Force the Woo branch whatever ECOMMERCE_BACKEND / .env says.
    monkeypatch.setattr(order_handler, "current_backend", lambda: "woocommerce")
    monkeypatch.setattr("ecommerce.cart_actions.current_backend", lambda: "woocommerce")
    monkeypatch.setattr("ecommerce.current_backend", lambda: "woocommerce")
    return rec


def _run(app, order, customer_id=CUSTOMER, source=None):
    with app.test_request_context():
        if source:
            g.identity = Identity(customer_id=customer_id, role="customer", source=source)
        out = order_handler.handle_reorder(
            Intent.REORDER, _entities(), [order], customer_id, SESSION, 1, time.time())
        if out is None:
            return None
        resp, status = out
        assert status == 200
        return resp.get_json()


def _types(body):
    return [a["type"] for a in body["actions"]]


def test_simple_lines_become_cart_actions_and_no_order_is_created(app, woo):
    body = _run(app, _order([_line(11, 2, "Blue Tile"), _line(12, 1, "Grout")]))

    assert _types(body) == ["ADD_TO_CART", "ADD_TO_CART", "OPEN_CART_PANEL"]
    first = body["actions"][0]["payload"]
    assert first["product_id"] == 11 and first["quantity"] == 2
    assert first["suppress_result"] is True
    assert "Blue Tile × 2" in body["bot_message"] and "Grout × 1" in body["bot_message"]
    # Only the read-only stock check ran; nothing wrote an order.
    assert woo.executed == ["check_stock"]
    assert "create_order" not in woo.executed


def test_variation_line_carries_its_attributes(app, woo):
    woo.var_payloads[501] = [{"attribute": "pa_color", "value": "red"}]
    body = _run(app, _order([_line(20, 3, "Shirt - Red", vid=501)]))

    p = body["actions"][0]["payload"]
    assert p["variation_id"] == 501
    assert p["variation"] == [{"attribute": "pa_color", "value": "red"}]


def test_saved_options_on_the_old_order_are_used_without_a_lookup(app, woo):
    # Colors is an "Any" attribute on this variation: the variation lookup
    # alone cannot fill it, but the old order recorded what was chosen.
    woo.required[30] = {"pa_finish", "pa_colors"}
    body = _run(app, _order([_line(30, 2, "Tara - Matt, Ash", vid=601,
                                   meta={"pa_finish": "matt", "pa_colors": "tara-ash", "_reduced_stock": "2"})]))

    p = body["actions"][0]["payload"]
    assert p["variation_id"] == 601 and p["quantity"] == 2
    assert p["variation"] == [{"attribute": "pa_colors", "value": "tara-ash"},
                              {"attribute": "pa_finish", "value": "matt"}]
    assert p["suppress_result"] is True
    assert woo.var_lookups == []          # no per-line variation fetch
    assert body["actions"][-1]["type"] == "OPEN_CART_PANEL"


def test_incomplete_saved_options_fall_back_to_the_lookup(app, woo):
    woo.required[31] = {"pa_finish", "pa_colors"}
    woo.var_payloads[602] = [{"attribute": "pa_finish", "value": "gloss"},
                             {"attribute": "pa_colors", "value": "blue"}]
    body = _run(app, _order([_line(31, 1, "Tile", vid=602, meta={"pa_finish": "gloss"})]))

    assert woo.var_lookups == [602]
    assert body["actions"][0]["payload"]["variation"] == woo.var_payloads[602]


def test_saved_options_are_not_used_when_the_parent_axes_are_unknown(app, woo):
    # Never guess: without the parent's axes the saved options can't be
    # confirmed complete, so the existing lookup decides.
    woo.var_payloads[603] = [{"attribute": "pa_size", "value": "m"}]
    _run(app, _order([_line(32, 1, "Tile", vid=603, meta={"pa_size": "m"})]))

    assert woo.var_lookups == [603]


def test_simple_products_ignore_line_meta(app, woo):
    body = _run(app, _order([_line(33, 1, "Grout", meta={"pa_size": "m"})]))

    p = body["actions"][0]["payload"]
    assert "variation" not in p and "variation_id" not in p


def test_variation_that_cannot_be_rebuilt_is_skipped_and_named(app, woo):
    # 502 has no attributes (deleted since the order / fetch failed).
    woo.var_payloads[501] = [{"attribute": "pa_size", "value": "m"}]
    body = _run(app, _order([
        _line(20, 1, "Shirt - M", vid=501),
        _line(21, 1, "Mug - Blue", vid=502),
        _line(22, 1, "Grout"),
    ]))

    added = [a["payload"]["product_id"] for a in body["actions"] if a["type"] == "ADD_TO_CART"]
    assert added == [20, 22]
    assert "couldn't add Mug - Blue" in body["bot_message"]
    assert body["actions"][-1]["type"] == "OPEN_CART_PANEL"


def test_nothing_addable_returns_no_actions_and_does_not_open_the_cart(app, woo):
    body = _run(app, _order([_line(21, 1, "Mug - Blue", vid=502)]))

    assert body["actions"] == []
    assert "couldn't add" in body["bot_message"]
    assert "Mug - Blue" in body["bot_message"]


def test_out_of_stock_still_blocks_the_reorder(app, woo):
    woo.oos = {12}
    body = _run(app, _order([_line(11), _line(12, name="Grout")]))

    assert not body.get("actions")
    assert "out of stock" in body["bot_message"]
    assert "Grout" in body["bot_message"]


def test_large_reorder_stock_check_covers_every_item(app, woo):
    # 150 different products: two requests of at most 100, every item checked.
    woo.oos = {1140}
    lines = [_line(1000 + i, 1, f"Item {i}") for i in range(150)]
    body = _run(app, _order(lines))

    assert [len(b) for b in woo.stock_batches] == [100, 50]
    assert sorted(sum(woo.stock_batches, [])) == [1000 + i for i in range(150)]
    assert "out of stock" in body["bot_message"] and "Item 140" in body["bot_message"]


def test_small_reorder_stock_check_is_one_request_with_unique_ids(app, woo):
    # Two lines of the same product (different variations) -> one id, one request.
    _run(app, _order([_line(11, 1, "A"), _line(11, 2, "B"), _line(12)]))

    assert woo.stock_batches == [[11, 12]]


def test_someone_elses_order_is_refused(app, woo):
    assert _run(app, _order([_line(11)], customer_id="99")) is None


def test_there_is_no_server_side_reorder_any_more():
    assert not hasattr(order_handler, "REORDER_MODE")
    src = order_handler.handle_reorder.__code__.co_names
    assert "create_order" not in src


def test_channel_chat_gets_a_pointer_to_the_website_not_a_phantom_cart(app, woo):
    body = _run(app, _order([_line(11)]), source="channel")

    assert body["actions"] == []
    assert "website" in body["bot_message"]



if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--noconftest", "-q"]))