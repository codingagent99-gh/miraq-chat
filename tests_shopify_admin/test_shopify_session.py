"""Unit tests for shopify_session (session-token verification + token exchange).
Run from the backend root (the root conftest.py needs a live store, so skip it):
    python -m pytest --noconftest tests_shopify_admin
"""
import base64, hashlib, hmac, json, sys, time, types
from unittest import mock

# shopify_apps imports app_config lazily; give it a stub with known creds.
sys.modules["app_config"] = types.SimpleNamespace(SHOPIFY_CLIENT_ID="pub_id", SHOPIFY_CLIENT_SECRET="pub_secret")

import shopify_apps, shopify_session as ss
from shopify_apps import AppCredentials

CUSTOM = AppCredentials("custom_id", "custom_secret", custom=True)
shopify_apps.app_for_install = lambda cid: CUSTOM if cid == "custom_id" else (shopify_apps.public_app() if not cid else None)
ss.app_for_install = shopify_apps.app_for_install

def b64(d): return base64.urlsafe_b64encode(d).decode().rstrip("=")
def make(claims=None, secret="pub_secret", alg="HS256", **over):
    now = time.time()
    c = {"iss": "https://demo.myshopify.com/admin", "dest": "https://demo.myshopify.com",
         "aud": "pub_id", "sub": "1", "exp": now + 60, "nbf": now - 1, "iat": now - 1, "jti": "x", "sid": "s"}
    c.update(claims or {}); c.update(over)
    h = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode()); p = b64(json.dumps(c).encode())
    sig = b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"

def rejects(tok, why=""):
    try: ss.verify_session_token(tok)
    except ss.SessionTokenError as e: return str(e)
    raise AssertionError(f"accepted a bad token: {why}")

def test_valid_public():
    shop, app = ss.verify_session_token(make())
    assert shop == "demo.myshopify.com" and app.client_id == "pub_id" and not app.custom

def test_custom_app_token_refused():
    # Custom apps are not embedded, so they never mint session tokens.
    rejects(make(aud="custom_id", secret="custom_secret"), "custom (non-embedded) app")

def test_aud_list():
    assert ss.verify_session_token(make(aud=["pub_id"]))[1].client_id == "pub_id"

def test_rejections():
    rejects(make(secret="wrong"), "bad sig")
    rejects(make(aud="custom_id", secret="pub_secret"), "custom aud signed with public secret")
    rejects(make(aud="nope"), "unknown aud")
    rejects(make(alg="none"), "alg none")
    rejects(make(alg="HS512"), "other alg")
    rejects(make(exp=time.time() - 30), "expired")
    rejects(make(nbf=time.time() + 60), "not yet valid")
    rejects(make(dest="https://evil.com"), "dest not myshopify")
    rejects(make(iss="https://other.myshopify.com/admin"), "iss mismatch")
    rejects(make(dest="https://demo.myshopify.com.evil.com"), "dest suffix trick")
    rejects("abc", "malformed"); rejects("a.b.c", "garbage")
    t = make(); h, p, s = t.split("."); 
    forged = json.loads(base64.urlsafe_b64decode(p + "==")); forged["dest"] = "https://victim.myshopify.com"
    rejects(f"{h}.{b64(json.dumps(forged).encode())}.{s}", "tampered payload")

def test_leeway():
    assert ss.verify_session_token(make(exp=time.time() - 5))  # within 10 s leeway

def test_bearer():
    assert ss.bearer_token("Bearer abc") == "abc"
    for bad in ("", "Basic abc", "Bearer ", "abc"):
        try: ss.bearer_token(bad); raise AssertionError(bad)
        except ss.SessionTokenError: pass

class R:
    def __init__(s, code, body): s.status_code, s._b, s.text = code, body, json.dumps(body)
    def json(s): return s._b

def test_exchange_request_shape():
    app = shopify_apps.public_app()
    with mock.patch.object(ss.http_requests, "post", return_value=R(200, {"access_token": "shpat_x"})) as post:
        assert ss.exchange_for_offline_token("demo.myshopify.com", app, "IDTOKEN")["access_token"] == "shpat_x"
    url = post.call_args.args[0]; body = post.call_args.kwargs["json"]
    assert url == "https://demo.myshopify.com/admin/oauth/access_token"
    assert body == {"client_id": "pub_id", "client_secret": "pub_secret",
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": "IDTOKEN", "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
                    "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
                    "expiring": "1"}

def test_exchange_errors():
    app = shopify_apps.public_app()
    for code, retry in ((400, True), (403, False), (500, True)):
        with mock.patch.object(ss.http_requests, "post", return_value=R(code, {"error": "x"})):
            try: ss.exchange_for_offline_token("demo.myshopify.com", app, "t"); raise AssertionError
            except ss.TokenExchangeError as e: assert e.retryable == retry, code
