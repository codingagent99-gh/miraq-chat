"""
Tests for the store's site key (site_key.py) replacing the WooCommerce key:
signed plugin requests, v2 identity tokens, the site-key check at
activation, and the uninstall / branding-push checks. The pre-1.0.8 bridge
(old WooCommerce key) is gone, and these check it stays gone.

No network, no database: HTTP sessions and the Tenant model are faked.

Run (either works):
    python test_site_key.py
    pytest --noconftest test_site_key.py
"""

import base64
import json
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import Flask

import identity as idm
import site_key


def _new_key():
    sk = Ed25519PrivateKey.generate()
    raw = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sk, base64.b64encode(raw).decode("ascii")


SK, PK = _new_key()
OTHER_SK, OTHER_PK = _new_key()


def _tenant(pk=PK, license_id="L1"):
    features = {"site_key": {"public_key": pk}} if pk else {}
    return SimpleNamespace(features=features, license_id=license_id, tenant_id="t1",
                           wp_base_url="https://shop.example.com")


def _payload(**over):
    now = int(time.time())
    p = {"v": 2, "lid": "L1", "cid": 7, "role": "customer", "email": "a@b.c", "iat": now, "exp": now + 3600}
    p.update(over)
    return p


# ── keys ─────────────────────────────────────────────────────────────────────

def test_public_key_parsing():
    assert site_key.parse_public_key(PK) is not None
    urlsafe = PK.replace("+", "-").replace("/", "_").rstrip("=")
    assert site_key.normalize_public_key(urlsafe) == PK
    for bad in ("", "not base64!!", base64.b64encode(b"short").decode(), base64.b64encode(b"x" * 33).decode()):
        assert site_key.parse_public_key(bad) is None
        assert site_key.normalize_public_key(bad) == ""


def test_storing_and_clearing_the_site_key_keeps_other_features():
    t = _tenant(pk="")
    t.features = {"http_profile": "browser"}
    site_key.store_public_key(t, PK)
    assert site_key.public_key_b64(t) == PK
    assert t.features["http_profile"] == "browser"
    site_key.clear_public_key(t)
    assert site_key.public_key_b64(t) == "" and t.features == {"http_profile": "browser"}


# ── signed requests ──────────────────────────────────────────────────────────

def _signed_ok(tenant, purpose, body=b'{"a":1}', sign_purpose=None, sign_body=None, sign_key=SK,
               ts=None, headers=None, **kw):
    hdrs = site_key.sign_request(sign_key, sign_purpose or purpose,
                                 body if sign_body is None else sign_body, timestamp=ts)
    if headers is not None:
        hdrs = headers
    with Flask(__name__).test_request_context("/x", method="POST", data=body, headers=hdrs):
        return site_key.verify_signed_request(tenant, purpose, **kw)


def test_signed_request_is_accepted():
    assert _signed_ok(_tenant(), "deactivate-tenant") is True


@pytest.mark.parametrize("case", ["wrong purpose", "tampered body", "other key", "too old", "in the future",
                                  "no headers", "no site key on record"])
def test_signed_request_is_refused(case):
    t = _tenant()
    kwargs = {
        "wrong purpose": dict(sign_purpose="branding-push"),
        "tampered body": dict(sign_body=b'{"a":2}'),
        "other key": dict(sign_key=OTHER_SK),
        "too old": dict(ts=int(time.time()) - 600),
        "in the future": dict(ts=int(time.time()) + 600),
        "no headers": dict(headers={}),
        "no site key on record": {},
    }[case]
    if case == "no site key on record":
        t = _tenant(pk="")
    assert _signed_ok(t, "deactivate-tenant", **kwargs) is False


# ── identity tokens ──────────────────────────────────────────────────────────

def _verify(token, **kw):
    kw.setdefault("license_id", "L1")
    kw.setdefault("public_key", site_key.parse_public_key(PK))
    return idm.verify_woo_identity(token, **kw)


def test_v2_token_verifies_with_the_site_key():
    ident = _verify(idm.sign_woo_identity_v2(_payload(), SK))
    assert (ident.customer_id, ident.source) == ("7", "wp_token")


@pytest.mark.parametrize("case", ["other key", "other store", "no key on record", "tampered", "garbage"])
def test_bad_v2_tokens_are_guests(case):
    token = idm.sign_woo_identity_v2(_payload(lid="L2" if case == "other store" else "L1"),
                                     OTHER_SK if case == "other key" else SK)
    if case == "tampered":
        body, sig = token.split(".")
        forged = base64.urlsafe_b64encode(json.dumps(_payload(cid=99), separators=(",", ":")).encode()).rstrip(b"=").decode()
        token = f"{forged}.{sig}"
    if case == "garbage":
        token = "abc.def"
    kw = {"public_key": None} if case == "no key on record" else {}
    assert _verify(token, **kw).is_guest


def test_expired_v2_token_asks_for_a_refresh():
    with pytest.raises(idm.IdentityExpired):
        _verify(idm.sign_woo_identity_v2(_payload(exp=int(time.time()) - 600), SK))


def test_v1_token_is_never_trusted_and_asks_for_a_refresh():
    body = base64.urlsafe_b64encode(json.dumps(_payload(v=1), separators=(",", ":")).encode()).rstrip(b"=").decode()
    with pytest.raises(idm.IdentityExpired):        # a page from an old plugin → widget refreshes
        _verify(f"{body}.anything")
    with pytest.raises(TypeError):                  # no way left to pass the old secret
        _verify(f"{body}.anything", consumer_secret="cs_old")


def test_resolve_identity_uses_the_stored_site_key(monkeypatch):
    t = _tenant()
    t.ecommerce_backend = "woocommerce"
    token = idm.sign_woo_identity_v2(_payload(), SK)
    with Flask(__name__).test_request_context("/chat", headers={"X-MiraQ-Identity": token}):
        assert idm.resolve_identity(t).customer_id == "7"


# ── the site's own key ───────────────────────────────────────────────────────

class _Session:
    def __init__(self, status=200, body=None, exc=None):
        self.status, self.body, self.exc, self.urls = status, body, exc, []

    def request(self, method, url, headers=None, **kw):
        import requests
        self.urls.append(url)
        if self.exc:
            raise self.exc
        r = requests.models.Response()
        r.status_code = self.status
        r._content = json.dumps(self.body if self.body is not None else {}).encode()
        r.url = url
        return r


@pytest.mark.parametrize("session, allowed, why", [
    (_Session(body={"public_key": PK}), True, "verified"),
    (_Session(body={"public_key": OTHER_PK}), False, "mismatch"),
    (_Session(status=404), True, "missing"),
    (_Session(status=500), True, "unreachable"),
    (_Session(exc=ConnectionError("down")), True, "unreachable"),
])
def test_activation_key_is_checked_against_the_site(session, allowed, why):
    assert site_key.check_site_key_matches("https://shop.example.com", PK, session=session) == (allowed, why)
    if session.urls:
        assert session.urls[0] == "https://shop.example.com/wp-json/miraq/v1/site-key"


# ── routes ───────────────────────────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter_by(self, **kw):
        return SimpleNamespace(first=lambda: next(
            (r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())), None))

    def get(self, key):
        return next((r for r in self.rows if str(r.tenant_id) == str(key)), None)


class _FakeTenantModel:
    query = None


def _app(monkeypatch, module, rows, site_session):
    import routes.provisioning as prov
    import routes.deactivation as deact
    import routes.webhook_routes as hooks
    commits = []
    model = type("Tenant", (_FakeTenantModel,), {"query": _FakeQuery(rows)})
    fake_db = SimpleNamespace(session=SimpleNamespace(
        commit=lambda: commits.append(1), rollback=lambda: None, add=rows.append,
        get=lambda m, key: _FakeQuery(rows).get(key)))
    for mod in (prov, deact):
        monkeypatch.setattr(mod, "Tenant", model)
        monkeypatch.setattr(mod, "db", fake_db)
    monkeypatch.setattr(hooks, "db", fake_db)
    monkeypatch.setattr(site_key, "fetch_site_public_key",
                        lambda base, **kw: site_key.__dict__["_real_fetch"](base, session=site_session))
    app = Flask(__name__)
    app.register_blueprint({"prov": prov.provisioning_bp, "deact": deact.deactivation_bp,
                            "hooks": hooks.webhook_bp}[module])
    return app, commits


site_key._real_fetch = site_key.fetch_site_public_key


def _post_signed(client, url, body, purpose, key=SK, extra=None):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", **site_key.sign_request(key, purpose, raw), **(extra or {})}
    return client.post(url, data=raw, headers=headers)


def test_register_site_key_route_is_gone(monkeypatch):
    t = _tenant(pk="")
    app, commits = _app(monkeypatch, "prov", [t], _Session(body={"public_key": PK}))
    resp = _post_signed(app.test_client(), "/register-site-key",
                        {"license_id": "L1", "site_public_key": PK}, "register-site-key",
                        extra={"X-Consumer-Key": "ck_old", "X-Consumer-Secret": "cs_old"})
    assert resp.status_code == 404
    assert site_key.public_key_b64(t) == "" and not commits


def test_activate_free_stores_only_the_public_key(monkeypatch):
    import routes.provisioning as prov
    rows = []

    class FakeTenant(SimpleNamespace):
        query = _FakeQuery(rows)

        def __init__(self, **kw):
            defaults = dict(features={}, site_domain=None,
                            wp_base_url=None, status="active", build_attempts=0, archived_at=None,
                            last_build_error=None, schema_migrated_at=None)
            super().__init__(**{**defaults, **kw})

    app, _ = _app(monkeypatch, "prov", rows, _Session(body={"public_key": PK}))
    monkeypatch.setattr(prov, "Tenant", FakeTenant)
    monkeypatch.setattr(prov, "ensure_tenant_database", lambda *a: None)
    monkeypatch.setattr(prov, "_create_tenant_schema", lambda *a: None)
    monkeypatch.setattr(prov, "_start_background_build", lambda *a: None)
    import cors_manager
    monkeypatch.setattr(cors_manager, "add_tenant_origin", lambda *a: None)
    app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://x/y"

    body = {"tenant_uuid": "8c4f6a0e-5d3b-4b1e-9f7a-2b6c1d0e9a11", "site_domain": "shop.example.com",
            "wp_base_url": "https://shop.example.com", "site_public_key": PK}
    resp = app.test_client().post("/activate-free", json=body)
    assert resp.status_code == 200, resp.get_json()
    assert site_key.public_key_b64(rows[0]) == PK
    assert not hasattr(rows[0], "woo_key") and not hasattr(rows[0], "woo_secret_encrypted")

    resp = app.test_client().post("/activate-free", json={**body, "site_public_key": "nope"})
    assert resp.status_code == 400
    resp = app.test_client().post("/activate-free", json={**body, "woo_key": "ck", "woo_secret": "cs",
                                                          "site_public_key": ""})
    assert resp.status_code == 400


def test_activate_free_refuses_a_key_the_site_does_not_serve(monkeypatch):
    rows = []
    app, _ = _app(monkeypatch, "prov", rows, _Session(body={"public_key": OTHER_PK}))
    resp = app.test_client().post("/activate-free", json={
        "tenant_uuid": "8c4f6a0e-5d3b-4b1e-9f7a-2b6c1d0e9a11", "wp_base_url": "https://shop.example.com",
        "site_public_key": PK})
    assert resp.status_code == 403 and rows == []


def test_uninstall_is_accepted_when_signed_with_the_site_key(monkeypatch):
    import routes.deactivation as deact
    t = _tenant()
    t.tenant_id = "8c4f6a0e-5d3b-4b1e-9f7a-2b6c1d0e9a11"
    t.status = "active"
    app, _ = _app(monkeypatch, "deact", [t], _Session())
    torn = []
    monkeypatch.setattr(deact, "teardown_tenant", lambda tenant, **kw: torn.append(tenant) or {"success": True})
    body = {"tenant_uuid": t.tenant_id}

    assert app.test_client().post("/deactivate-tenant", json=body).status_code == 401          # unsigned
    assert app.test_client().post("/deactivate-tenant", json=body, headers={
        "X-Consumer-Key": "ck_old", "X-Consumer-Secret": "cs_old"}).status_code == 401           # old key
    assert _post_signed(app.test_client(), "/deactivate-tenant", body, "deactivate-tenant",
                        key=OTHER_SK).status_code == 401                                        # wrong key
    assert _post_signed(app.test_client(), "/deactivate-tenant", body, "branding-push").status_code == 401
    assert torn == []
    assert _post_signed(app.test_client(), "/deactivate-tenant", body, "deactivate-tenant").status_code == 200
    assert torn == [t]


def test_teardown_drops_the_site_key(monkeypatch):
    import routes.deactivation as deact
    t = _tenant()
    t.db_name, t.shopify_domain = "tenant_x", None
    monkeypatch.setattr(deact, "db", SimpleNamespace(session=SimpleNamespace(commit=lambda: None)))
    import tenant_snapshot_store
    monkeypatch.setattr(tenant_snapshot_store.snapshot_store, "delete", lambda *a: None)
    assert deact.teardown_tenant(t)["success"] is True
    assert site_key.public_key_b64(t) == "" and t.status == "archived"


def test_branding_push_accepts_only_the_site_key(monkeypatch):
    import routes.webhook_routes as hooks
    from flask import g
    t = _tenant()
    app, _ = _app(monkeypatch, "hooks", [t], _Session())
    saved = []
    import widget_branding
    monkeypatch.setattr(widget_branding, "store_widget_branding", lambda tenant, **kw: saved.append(kw))

    @app.before_request
    def _bind():
        g.tenant = t

    url, body = "/webhooks/woocommerce/L1/branding-push", {"image_url": "https://s/l.png", "text": "Hi"}
    assert app.test_client().post(url, json=body).status_code == 401
    assert _post_signed(app.test_client(), url, body, "branding-push", key=OTHER_SK).status_code == 401
    assert _post_signed(app.test_client(), url, body, "branding-push").status_code == 200
    assert saved == [body]

    # The old WooCommerce key headers are no longer a way in.
    resp = app.test_client().post(url, json=body, headers={"X-Consumer-Key": "ck_old", "X-Consumer-Secret": "cs_old"})
    assert resp.status_code == 401 and saved == [body]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--noconftest", "-q"]))
