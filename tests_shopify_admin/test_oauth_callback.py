"""The refactored OAuth callback still runs: validate -> store token -> bind app -> provision -> redirect.
Run from the backend root (the root conftest.py needs a live store, so skip it):
    python -m pytest --noconftest tests_shopify_admin
"""
import hashlib, hmac, os, time, urllib.parse
from unittest import mock
os.environ.setdefault("ECOMMERCE_BACKEND", "shopify")
os.environ["SHOPIFY_CLIENT_ID"] = "pub_id"; os.environ["SHOPIFY_CLIENT_SECRET"] = "pub_secret"
from flask import Flask
import routes.shopify_oauth as o
import shopify_apps

PUB = shopify_apps.AppCredentials("pub_id", "pub_secret")

def signed(params):
    msg = "&".join(sorted(f"{k}={v}" for k, v in params.items()))
    return dict(params, hmac=hmac.new(b"pub_secret", msg.encode(), hashlib.sha256).hexdigest())

class R:
    status_code = 200; text = ""
    def __init__(s, b): s.b = b
    def raise_for_status(s): pass
    def json(s): return s.b

def run(payload):
    app = Flask(__name__); app.register_blueprint(o.shopify_oauth_bp)
    shop = "demo.myshopify.com"
    q = signed({"shop": shop, "code": "c", "state": o._make_state(shop, "pub_secret"), "timestamp": str(int(time.time()))})
    calls = []
    with mock.patch.object(o, "app_for_install", return_value=PUB), \
         mock.patch.object(o.http_requests, "post", return_value=R(payload)), \
         mock.patch.object(o, "store_token", side_effect=lambda s, p: calls.append("store")), \
         mock.patch.object(o, "record_install", side_effect=lambda s, a: calls.append("record")), \
         mock.patch.object(o, "provision_tenant", side_effect=lambda s: calls.append("provision")), \
         mock.patch.object(o, "_app_page_redirect", return_value=("redirected", 302)):
        r = app.test_client().get("/shopify/auth/callback?" + urllib.parse.urlencode(q))
    return r, calls

def test_callback_happy_path():
    r, calls = run({"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    assert r.status_code == 302 and calls == ["store", "record", "provision"]

def test_callback_rejects_non_expiring():
    r, calls = run({"access_token": "a"})
    assert r.status_code == 502 and calls == []
