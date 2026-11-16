"""Route tests for routes/shopify_admin.py with the database layer mocked.
Run from the backend root (the root conftest.py needs a live store, so skip it):
    python -m pytest --noconftest tests_shopify_admin
"""
import base64, hashlib, hmac, json, os, sys, time, types
from contextlib import contextmanager
from unittest import mock
import pytest

os.environ.setdefault("ECOMMERCE_BACKEND", "shopify")
os.environ["SHOPIFY_CLIENT_ID"] = "pub_id"; os.environ["SHOPIFY_CLIENT_SECRET"] = "pub_secret"
from flask import Flask
import routes.shopify_admin as m
import shopify_apps
shopify_apps.public_app = lambda: shopify_apps.AppCredentials("pub_id", "pub_secret")
m.public_app = shopify_apps.public_app
import shopify_session; shopify_session.public_app = shopify_apps.public_app

def b64(d): return base64.urlsafe_b64encode(d).decode().rstrip("=")
def tok(shop="demo.myshopify.com"):
    now = time.time()
    c = {"iss": f"https://{shop}/admin", "dest": f"https://{shop}", "aud": "pub_id", "exp": now + 60, "nbf": now - 1}
    h = b64(b'{"alg":"HS256","typ":"JWT"}'); p = b64(json.dumps(c).encode())
    return f"{h}.{p}.{b64(hmac.new(b'pub_secret', f'{h}.{p}'.encode(), hashlib.sha256).digest())}"
AUTH = lambda: {"Authorization": f"Bearer {tok()}"}

@pytest.fixture
def client(tmp_path):
    (tmp_path / "app-assets").mkdir()
    (tmp_path / "index.html").write_text('<meta name="shopify-api-key" content="%SHOPIFY_API_KEY%" /><script src="./app-assets/a.js"></script>')
    (tmp_path / "app-assets" / "a.js").write_text("console.log(1)")
    app = Flask(__name__); app.register_blueprint(m.shopify_admin_bp)
    with mock.patch.object(m, "ADMIN_DIST", str(tmp_path)):
        yield app.test_client()

@contextmanager
def lock(v=True): yield v

def test_page_injects_key_and_csp(client):
    r = client.get("/shopify/app?shop=demo.myshopify.com&host=x&embedded=1")
    assert r.status_code == 200
    assert 'content="pub_id"' in r.text and "%SHOPIFY_API_KEY%" not in r.text
    assert r.headers["Content-Security-Policy"] == "frame-ancestors https://demo.myshopify.com https://admin.shopify.com;"
    assert r.headers["Cache-Control"] == "no-store"

def test_page_ignores_bad_shop_in_csp(client):
    r = client.get("/shopify/app?shop=evil.com%20https://x.com")
    assert r.headers["Content-Security-Policy"] == "frame-ancestors https://admin.shopify.com;"

def test_assets(client):
    r = client.get("/shopify/app-assets/a.js")
    assert r.status_code == 200 and "immutable" in r.headers["Cache-Control"]
    assert client.get("/shopify/app-assets/../index.html").status_code == 404

def test_status_requires_session(client):
    r = client.get("/shopify/admin/status")
    assert r.status_code == 401 and r.headers["X-Shopify-Retry-Invalid-Session-Request"] == "1"

def test_status_busy(client):
    with mock.patch.object(m, "_install_lock", lambda shop: lock(False)):
        assert client.get("/shopify/admin/status", headers=AUTH()).status_code == 202

def test_status_healthy_store_does_no_exchange(client):
    with mock.patch.object(m, "_install_lock", lambda shop: lock()), \
         mock.patch.object(m, "_tenant_problem", return_value=None), \
         mock.patch.object(m, "_token_problem", return_value=None), \
         mock.patch.object(m, "exchange_for_offline_token") as ex, \
         mock.patch.object(m, "_status_payload", return_value={"setup": "ready"}):
        r = client.get("/shopify/admin/status", headers=AUTH())
    assert r.status_code == 200 and r.json == {"setup": "ready"} and not ex.called

def test_first_load_installs(client):
    calls = []
    payload = {"access_token": "shpat", "refresh_token": "r", "expires_in": 3600, "scope": "read_products"}
    with mock.patch.object(m, "_install_lock", lambda shop: lock()), \
         mock.patch.object(m, "_tenant_problem", return_value="no tenant"), \
         mock.patch.object(m, "_token_problem", return_value="no token"), \
         mock.patch.object(m, "exchange_for_offline_token", side_effect=lambda s, a, t: calls.append(("exchange", s, a.client_id)) or payload), \
         mock.patch.object(m, "store_token", side_effect=lambda s, p: calls.append(("store", s))), \
         mock.patch.object(m, "record_install", side_effect=lambda s, a: calls.append(("record", s))), \
         mock.patch.object(m, "provision_tenant", side_effect=lambda s: calls.append(("provision", s))), \
         mock.patch.object(m, "_status_payload", return_value={"setup": "indexing"}):
        r = client.get("/shopify/admin/status", headers=AUTH())
    assert r.status_code == 200
    assert [c[0] for c in calls] == ["exchange", "store", "record", "provision"]
    assert calls[0][1:] == ("demo.myshopify.com", "pub_id")

def test_token_only_problem_skips_provision(client):
    payload = {"access_token": "shpat", "refresh_token": "r", "expires_in": 3600}
    with mock.patch.object(m, "_install_lock", lambda shop: lock()), \
         mock.patch.object(m, "_tenant_problem", return_value=None), \
         mock.patch.object(m, "_token_problem", return_value="last refresh failed"), \
         mock.patch.object(m, "exchange_for_offline_token", return_value=payload), \
         mock.patch.object(m, "store_token") as st, mock.patch.object(m, "record_install") as rec, \
         mock.patch.object(m, "provision_tenant") as prov, \
         mock.patch.object(m, "_status_payload", return_value={}):
        assert client.get("/shopify/admin/status", headers=AUTH()).status_code == 200
    assert st.called and not rec.called and not prov.called

def test_non_expiring_token_refused(client):
    with mock.patch.object(m, "_install_lock", lambda shop: lock()), \
         mock.patch.object(m, "_tenant_problem", return_value="no tenant"), \
         mock.patch.object(m, "_token_problem", return_value="no token"), \
         mock.patch.object(m, "exchange_for_offline_token", return_value={"access_token": "shpat"}), \
         mock.patch.object(m, "store_token") as st:
        assert client.get("/shopify/admin/status", headers=AUTH()).status_code == 502
    assert not st.called

def test_exchange_retryable_gives_401(client):
    with mock.patch.object(m, "_install_lock", lambda shop: lock()), \
         mock.patch.object(m, "_tenant_problem", return_value="no tenant"), \
         mock.patch.object(m, "_token_problem", return_value=None), \
         mock.patch.object(m, "exchange_for_offline_token", side_effect=shopify_session.TokenExchangeError("x", retryable=True)):
        r = client.get("/shopify/admin/status", headers=AUTH())
    assert r.status_code == 401

def test_setup_state_and_payload():
    T = lambda s: types.SimpleNamespace(status=s, tenant_id="t")
    assert m._setup_state(T("active")) == "ready"
    assert m._setup_state(T("warming")) == "indexing"
    assert m._setup_state(T("provision_failed")) == "failed"
    assert m._setup_state(None) == "failed"
    app = shopify_apps.AppCredentials("pub_id", "pub_secret")
    fake_q = mock.MagicMock(); fake_q.filter_by.return_value.first.return_value = T("active")
    with mock.patch.object(m, "Tenant", types.SimpleNamespace(query=fake_q)), \
         mock.patch.object(m, "_instagram", return_value={"available": False}), \
         mock.patch.dict(m._SUPPORT, {"email": "help@x.com", "url": "", "privacy_url": ""}):
        p = m._status_payload("demo.myshopify.com", app)
    assert p["embed_url"] == "https://demo.myshopify.com/admin/themes/current/editor?context=apps&activateAppId=pub_id/miraq_widget"
    assert p["support"] == {"email": "help@x.com"} and p["setup"] == "ready"

# ── conversations (read-only, App Store requirement 5.1.5) ──────────────────

def test_conversations_require_session(client):
    for path in ("/shopify/admin/conversations", f"/shopify/admin/conversations/{'0' * 8}-0000-0000-0000-{'0' * 12}"):
        r = client.get(path)
        assert r.status_code == 401 and r.headers["X-Shopify-Retry-Invalid-Session-Request"] == "1"

def test_conversations_unknown_store_404(client):
    fake_q = mock.MagicMock(); fake_q.filter_by.return_value.first.return_value = None
    with mock.patch.object(m, "Tenant", types.SimpleNamespace(query=fake_q)):
        assert client.get("/shopify/admin/conversations", headers=AUTH()).status_code == 404

def test_conversations_archived_store_404_no_db_name_empty():
    T = lambda status, db="tenant_x": types.SimpleNamespace(status=status, db_name=db)
    for tenant, expect_error in ((T("archived"), True), (T("active", None), False)):
        fake_q = mock.MagicMock(); fake_q.filter_by.return_value.first.return_value = tenant
        app = Flask(__name__)
        with app.test_request_context(), mock.patch.object(m, "Tenant", types.SimpleNamespace(query=fake_q)):
            got, error = m._bind_store_db("demo.myshopify.com")
        assert got is None and (error is not None) == expect_error

def test_conversations_store_without_db_is_empty(client):
    with mock.patch.object(m, "_bind_store_db", return_value=(None, None)), \
         mock.patch.object(m, "_list_conversations") as lst:
        r = client.get("/shopify/admin/conversations", headers=AUTH())
    assert r.status_code == 200 and r.json == {"conversations": [], "has_more": False} and not lst.called

def test_conversations_bind_the_session_store_and_page(client):
    seen = {}
    def bind(shop):
        seen["shop"] = shop
        return types.SimpleNamespace(tenant_id="t"), None
    def lst(cursor, limit):
        seen["cursor"], seen["limit"] = cursor, limit
        return [{"id": "c1"}], True
    cursor = "1794830400000000|11111111-2222-3333-4444-555555555555"
    with mock.patch.object(m, "_bind_store_db", side_effect=bind), \
         mock.patch.object(m, "_list_conversations", side_effect=lst):
        r = client.get(f"/shopify/admin/conversations?limit=500&before={cursor}", headers=AUTH())
    assert r.status_code == 200 and r.json == {"conversations": [{"id": "c1"}], "has_more": True}
    assert r.headers["Cache-Control"] == "no-store"
    assert seen["shop"] == "demo.myshopify.com" and seen["limit"] == m._CONV_PAGE_MAX
    assert str(seen["cursor"][1]) == "11111111-2222-3333-4444-555555555555"

def test_conversations_bad_cursor_400(client):
    with mock.patch.object(m, "_bind_store_db") as bind:
        r = client.get("/shopify/admin/conversations?before=nonsense", headers=AUTH())
    assert r.status_code == 400 and not bind.called

def test_conversations_db_error_is_503_not_empty(client):
    from sqlalchemy.exc import OperationalError
    boom = OperationalError("select", {}, Exception("db gone"))
    with mock.patch.object(m, "_bind_store_db", return_value=(types.SimpleNamespace(status="active"), None)), \
         mock.patch.object(m, "_list_conversations", side_effect=boom), mock.patch.object(m.db, "session"):
        r = client.get("/shopify/admin/conversations", headers=AUTH())
    assert r.status_code == 503 and r.json == {"error": "conversations_unavailable"}

def test_conversations_failed_setup_without_db_is_empty(client):
    # Setup failed before the store's database existed: no chats, not an outage.
    from sqlalchemy.exc import OperationalError
    boom = OperationalError("select", {}, Exception('database "tenant_x" does not exist'))
    with mock.patch.object(m, "_bind_store_db", return_value=(types.SimpleNamespace(status="provision_failed"), None)), \
         mock.patch.object(m, "_list_conversations", side_effect=boom), mock.patch.object(m.db, "session"):
        r = client.get("/shopify/admin/conversations", headers=AUTH())
    assert r.status_code == 200 and r.json == {"conversations": [], "has_more": False}

def test_transcript_not_in_this_store_404(client):
    # Another store's conversation id isn't in this store's database.
    with mock.patch.object(m, "_bind_store_db", return_value=(types.SimpleNamespace(status="active"), None)), \
         mock.patch.object(m, "_transcript", return_value=None):
        r = client.get("/shopify/admin/conversations/11111111-2222-3333-4444-555555555555", headers=AUTH())
    assert r.status_code == 404

def test_transcript_found(client):
    body = {"id": "c1", "messages": [{"from": "shopper", "text": "hi"}]}
    with mock.patch.object(m, "_bind_store_db", return_value=(types.SimpleNamespace(), None)), \
         mock.patch.object(m, "_transcript", return_value=body) as tr:
        r = client.get("/shopify/admin/conversations/11111111-2222-3333-4444-555555555555", headers=AUTH())
    assert r.status_code == 200 and r.json == body and r.headers["Cache-Control"] == "no-store"
    assert str(tr.call_args[0][0]) == "11111111-2222-3333-4444-555555555555"

def test_conversation_helpers():
    assert m._preview("  where   is\nmy order? ") == "where is my order?"
    long = m._preview("x" * 500)
    assert len(long) == m._PREVIEW_CHARS and long.endswith("…")
    assert m._page_size(None) == m._CONV_PAGE_DEFAULT and m._page_size("0") == 1 and m._page_size("abc") == m._CONV_PAGE_DEFAULT
    conv = types.SimpleNamespace(updated_at=__import__("datetime").datetime(2026, 11, 16, 10, 0, 0, 123456, tzinfo=__import__("datetime").timezone.utc),
                                 id=__import__("uuid").UUID("11111111-2222-3333-4444-555555555555"))
    assert m._decode_cursor(m._encode_cursor(conv)) == (conv.updated_at, conv.id)
    assert m._decode_cursor("no-separator") is None and m._decode_cursor("123|not-a-uuid") is None
    assert m._decode_cursor("2026-11-16T10:00:00+00:00|11111111-2222-3333-4444-555555555555") is None
