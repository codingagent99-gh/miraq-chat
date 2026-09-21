#!/usr/bin/env python3
"""
dev_provision.py — local-dev driver for the multi-tenant lifecycle.

Exercises the same HTTP endpoints the WordPress plugin calls, so the backend
can be verified on localhost before anything is deployed. Signature
verification must be off for the signed paths:

    LICENSE_VERIFICATION_ENABLED=false

Subcommands
-----------
  provision   POST /provision-tenant     (paid path; fake signed payload)
  free        POST /activate-free        (free path)
  deactivate  POST /deactivate-tenant    (credential path — the free-tier one)
  revoke      POST /deactivate-tenant    (signed path)
  shopify     Convert an existing tenant row to a Shopify tenant (no endpoint
              provisions Shopify tenants yet — the row is seeded by hand).
  event       Send a signed Shopify webhook (order-paid / app-uninstalled /
              product-update), HMAC'd with SHOPIFY_CLIENT_SECRET.
  status      Read the control-plane DB directly and print every tenant:
              row state, whether its database still exists, table counts.
  wait        Poll `status` until a tenant leaves "warming" (build finished).

Every subcommand prints the request it sent and the full response. Anything
that fails prints the backend's error body, and `status` is the ground truth
afterwards — the backend's own log (logs/, or stdout) carries the detail for
anything that went wrong inside a build thread.

Examples
--------
  # free activation, then watch the build
  python dev_provision.py free --site-domain shop.test \
      --wp-base-url https://shop.test --woo-key ck_x --woo-secret cs_x
  python dev_provision.py wait --tenant-uuid <uuid printed above>

  # paid activation reusing that same tenant (the upgrade path)
  python dev_provision.py provision --license-id dev-001 --tenant-uuid <uuid> \
      --site-domain shop.test --wp-base-url https://shop.test \
      --woo-key ck_x --woo-secret cs_x

  # free-tier teardown, then confirm the DB is kept for the grace period
  python dev_provision.py deactivate --tenant-uuid <uuid> \
      --woo-key ck_x --woo-secret cs_x
  python dev_provision.py status
"""

import argparse
import json
import os
import sys
import time
import uuid as uuid_mod

import requests

DEFAULT_BACKEND = os.environ.get("MIRAQ_DEV_BACKEND", "http://localhost:5000")


# ── helpers ──────────────────────────────────────────────────────────────────

def _post(backend: str, path: str, body: dict, headers: dict | None = None) -> int:
    url = f"{backend.rstrip('/')}{path}"
    print(f"\n→ POST {url}")
    print(f"  body    : {json.dumps(_redact(body))}")
    if headers:
        print(f"  headers : {json.dumps(_redact(headers))}")

    try:
        resp = requests.post(url, json=body, headers=headers or {}, timeout=60)
    except requests.RequestException as e:
        print(f"✗ request failed: {type(e).__name__}: {e}")
        print("  Is the backend running, and is --backend pointing at it?")
        return 1

    print(f"← {resp.status_code}")
    try:
        print(f"  {json.dumps(resp.json(), indent=2)}")
    except ValueError:
        print(f"  {resp.text[:800]}")

    if resp.status_code >= 300:
        print("\n✗ FAILED. Check the backend log for the matching request id/timestamp —")
        print("  provisioning, teardown and the build thread all log at INFO.")
        return 1
    return 0


def _redact(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if any(s in k.lower() for s in ("secret", "signature")):
            out[k] = f"<{len(str(v))} chars>"
        else:
            out[k] = v
    return out


def _dsn(args) -> str:
    """
    The control-plane DSN, resolved the same way the backend resolves it, so
    the two cannot silently disagree:

        --dsn  >  .env's DATABASE_URL  >  the shell's DATABASE_URL

    .env deliberately outranks the shell environment here. The backend calls
    load_dotenv(), which does NOT override an already-set shell variable — so
    a stale DATABASE_URL left in a terminal session points this script at one
    database while the server uses another, and you get "database ... does
    not exist" for a tenant that was just created successfully.
    """
    if args.dsn:
        return args.dsn

    env_file_dsn = ""
    try:
        from dotenv import dotenv_values
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        env_file_dsn = (dotenv_values(env_path) or {}).get("DATABASE_URL") or ""
    except Exception:
        pass

    shell_dsn = os.environ.get("DATABASE_URL", "")

    if env_file_dsn and shell_dsn and env_file_dsn != shell_dsn:
        print(f"note: .env and $DATABASE_URL differ — using .env ({_hide_pw(env_file_dsn)})")
        print(f"      shell value was {_hide_pw(shell_dsn)}")

    dsn = env_file_dsn or shell_dsn
    if not dsn:
        raise SystemExit("No control-plane DSN. Pass --dsn, or set DATABASE_URL in .env.")
    return dsn


def _hide_pw(dsn: str) -> str:
    import urllib.parse
    try:
        p = urllib.parse.urlparse(dsn)
        if p.password:
            netloc = p.netloc.replace(f":{p.password}@", ":***@")
            return p._replace(netloc=netloc).geturl()
    except Exception:
        pass
    return dsn


def _connect(dsn: str):
    """Connect, or explain what went wrong and which databases do exist."""
    import psycopg2
    import urllib.parse
    try:
        return psycopg2.connect(dsn)
    except Exception as e:
        print(f"✗ cannot connect to the control-plane DB: {e}".rstrip())
        print(f"  DSN in use: {_hide_pw(dsn)}")
        try:
            admin = urllib.parse.urlparse(dsn)._replace(path="/postgres").geturl()
            conn = psycopg2.connect(admin)
            cur = conn.cursor()
            cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
            names = [r[0] for r in cur.fetchall()]
            conn.close()
            control = [n for n in names if not n.startswith("tenant_")]
            print(f"  databases on this server: {', '.join(control) or '(none)'}")
            print("  Pick the one the backend uses (check DATABASE_URL in .env) and pass it with --dsn.")
        except Exception:
            pass
        return None


def _fake_signed_payload(license_id: str) -> tuple[str, str]:
    """
    A payload shaped like the licence server's, unsigned. Only usable with
    LICENSE_VERIFICATION_ENABLED=false; with verification on the backend
    rejects it with 401, which is itself a useful check.
    """
    raw_payload = json.dumps(
        {"licenseId": license_id, "expiresAt": "2027-01-01T00:00:00Z"},
        separators=(",", ":"),
    )
    return raw_payload, "dev-placeholder-unused"


# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_provision(args) -> int:
    raw_payload, signature = _fake_signed_payload(args.license_id)
    body = {
        "raw_payload": raw_payload,
        "signature":   signature,
        "site_domain": args.site_domain,
        "wp_base_url": args.wp_base_url,
        "woo_key":     args.woo_key,
        "woo_secret":  args.woo_secret,
    }
    # Optional, and the whole point of the upgrade test: with it, the backend
    # reuses the existing (free) tenant row and database instead of creating a
    # second one.
    if args.tenant_uuid:
        body["tenant_uuid"] = args.tenant_uuid
    return _post(args.backend, "/provision-tenant", body)


def cmd_free(args) -> int:
    tenant_uuid = args.tenant_uuid or str(uuid_mod.uuid4())
    print(f"tenant_uuid: {tenant_uuid}")
    print("  (the plugin generates this once and keeps it in wc_chat_widget_uuid —")
    print("   pass it to `provision` later to test the free → paid upgrade)")
    return _post(args.backend, "/activate-free", {
        "tenant_uuid": tenant_uuid,
        "site_domain": args.site_domain,
        "wp_base_url": args.wp_base_url,
        "woo_key":     args.woo_key,
        "woo_secret":  args.woo_secret,
    })


def cmd_deactivate(args) -> int:
    """Credential path — what a free install's uninstall.php sends."""
    return _post(
        args.backend,
        "/deactivate-tenant",
        {"tenant_uuid": args.tenant_uuid},
        {"X-Consumer-Key": args.woo_key, "X-Consumer-Secret": args.woo_secret},
    )


def cmd_revoke(args) -> int:
    """Signed path — what a licensed install's uninstall.php sends."""
    raw_payload, signature = _fake_signed_payload(args.license_id)
    body = {"raw_payload": raw_payload, "signature": signature}
    if args.tenant_uuid:
        body["tenant_uuid"] = args.tenant_uuid
    return _post(args.backend, "/deactivate-tenant", body)


def cmd_status(args) -> int:
    """
    Ground truth, read straight from the control-plane DB — deliberately not
    via the backend, so it still works when the backend is down or a request
    is failing.
    """
    import psycopg2
    import urllib.parse

    dsn = _dsn(args)
    conn = _connect(dsn)
    if conn is None:
        return 1

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT tenant_id, license_id, plan, status, db_name, ecommerce_backend,
                   site_domain, build_attempts, last_build_error, archived_at,
                   widget_config_fetched_at
            FROM tenants
            ORDER BY created_at
        """)
        rows = cur.fetchall()
        if not rows:
            print("No tenants in the control-plane DB.")
            return 0

        cur.execute("SELECT datname FROM pg_database WHERE datname LIKE 'tenant_%'")
        existing = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()

    for (tid, lic, plan, status, db_name, backend, domain, attempts,
         err, archived_at, branding_at) in rows:
        print("─" * 72)
        print(f"tenant_id   : {tid}")
        print(f"license_id  : {lic}")
        print(f"plan/status : {plan} / {status}"
              + (f"   archived_at={archived_at}" if archived_at else ""))
        print(f"backend     : {backend}      site: {domain}")
        print(f"database    : {db_name}   {'EXISTS' if db_name in existing else 'GONE'}")
        print(f"branding    : {'fetched ' + str(branding_at) if branding_at else 'never fetched'}")
        if attempts:
            print(f"build_attempts: {attempts}")
        if err:
            print(f"last_build_error: {err[:300]}")

        # Per-tenant table counts, which is how you tell a build actually
        # produced a usable database rather than just a row.
        if db_name in existing:
            parsed = urllib.parse.urlparse(dsn)
            tdsn = parsed._replace(path=f"/{db_name}").geturl()
            try:
                tconn = psycopg2.connect(tdsn)
                tcur = tconn.cursor()
                tcur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public' ORDER BY table_name
                """)
                tables = [r[0] for r in tcur.fetchall()]
                counts = []
                for t in ("conversations", "messages", "chat_usage"):
                    if t in tables:
                        tcur.execute(f"SELECT count(*) FROM {t}")
                        counts.append(f"{t}={tcur.fetchone()[0]}")
                tconn.close()
                print(f"tables      : {', '.join(tables) or '(none)'}")
                if counts:
                    print(f"rows        : {', '.join(counts)}")
                leaked = [t for t in ("tenants", "shopify_tokens") if t in tables]
                if leaked:
                    print(f"⚠ control-plane tables present in tenant DB: {', '.join(leaked)}")
            except Exception as e:
                print(f"tables      : could not inspect ({e})")
    print("─" * 72)
    return 0


def cmd_wait(args) -> int:
    """Poll until the tenant leaves `warming`, so a build failure is visible."""
    dsn = _dsn(args)
    deadline = time.time() + args.timeout

    while time.time() < deadline:
        conn = _connect(dsn)
        if conn is None:
            return 1
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT status, last_build_error FROM tenants WHERE tenant_id = %s",
                (args.tenant_uuid,),
            )
            row = cur.fetchone()
        finally:
            conn.close()

        if row is None:
            print(f"✗ no tenant with tenant_id={args.tenant_uuid}")
            return 1

        status, err = row
        print(f"  status={status}")
        if status != "warming":
            if status == "active":
                print("✓ build finished — tenant is active")
                return 0
            print(f"✗ build ended in status={status}")
            if err:
                print(f"  last_build_error: {err}")
            print("  Full traceback is in the backend log (search _start_background_build).")
            return 1
        time.sleep(args.interval)

    print(f"✗ still warming after {args.timeout}s — check the backend log")
    return 1


# ── Shopify ──────────────────────────────────────────────────────────────────

def cmd_shopify(args) -> int:
    """
    Flip a tenant to the Shopify backend.

    Nothing in the backend creates Shopify tenants: /provision-tenant and
    /activate-free are both WooCommerce flows, and Stage 2 has no Shopify
    install endpoint yet. So the working path for testing is: create a tenant
    the normal way (`free`), which builds its database and schema, then
    convert the row here.

    Restart the backend afterwards. The tenant's StoreLoader is cached in the
    TenantRegistry as a WooCommerce loader; the row change is only picked up
    when it is rebuilt.
    """
    dsn = _dsn(args)
    conn = _connect(dsn)
    if conn is None:
        return 1
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenants
               SET ecommerce_backend = 'shopify',
                   shopify_domain    = %s,
                   site_domain       = COALESCE(site_domain, %s)
             WHERE tenant_id = %s
         RETURNING license_id, db_name
            """,
            (args.shopify_domain, args.shopify_domain, args.tenant_uuid),
        )
        row = cur.fetchone()
        if row is None:
            print(f"✗ no tenant with tenant_id={args.tenant_uuid}")
            return 1
        conn.commit()
    finally:
        conn.close()

    license_id, db_name = row
    print(f"✓ tenant {args.tenant_uuid} is now a Shopify tenant")
    print(f"  shopify_domain : {args.shopify_domain}")
    print(f"  license_id     : {license_id}   (send as X-MiraQ-License-Id)")
    print(f"  database       : {db_name}")
    print("\n  Restart the backend so the cached WooCommerce loader is dropped.")
    print("  Then: curl -H \"X-MiraQ-License-Id: <license_id>\" <backend>/shopify-token-status")
    return 0


def cmd_event(args) -> int:
    """
    Send a Shopify Events/webhook delivery, signed the way Shopify signs it:
    base64(HMAC-SHA256(raw_body, SHOPIFY_CLIENT_SECRET)) in Shopify-Hmac-Sha256,
    with the store in Shopify-Shop-Domain. The backend resolves the tenant from
    that domain — these routes are exempt from X-MiraQ-License-Id because
    Shopify cannot send it.
    """
    import base64
    import hashlib
    import hmac as hmac_mod

    secret = args.client_secret or os.environ.get("SHOPIFY_CLIENT_SECRET", "")
    if not secret:
        try:
            from dotenv import dotenv_values
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            secret = (dotenv_values(env_path) or {}).get("SHOPIFY_CLIENT_SECRET") or ""
        except Exception:
            pass
    if not secret:
        raise SystemExit("No SHOPIFY_CLIENT_SECRET (pass --client-secret, or set it in .env)")

    if args.body:
        raw = args.body.encode()
    elif args.topic == "order-paid":
        raw = json.dumps({
            "id": 5551234567890,
            "name": "#1001",
            "financial_status": "paid",
            "note_attributes": [{"name": "miraq_session_id", "value": args.session_id}],
        }).encode()
    elif args.topic == "product-update":
        raw = json.dumps({"id": 1234567890, "title": "Test product"}).encode()
    else:  # app-uninstalled
        raw = json.dumps({"id": 1234567890, "domain": args.shopify_domain}).encode()

    digest = base64.b64encode(
        hmac_mod.new(secret.encode(), raw, hashlib.sha256).digest()
    ).decode()

    url = f"{args.backend.rstrip('/')}/events/{args.topic}"
    print(f"\n→ POST {url}")
    print(f"  shop    : {args.shopify_domain}")
    print(f"  body    : {raw.decode()[:200]}")
    try:
        resp = requests.post(
            url,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Shopify-Hmac-Sha256": digest,
                "Shopify-Shop-Domain": args.shopify_domain,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"✗ request failed: {type(e).__name__}: {e}")
        return 1

    print(f"← {resp.status_code}")
    print(f"  {resp.text[:500]}")
    if args.topic == "order-paid" and resp.status_code < 300:
        print("\n  Now check the confirmation landed in the TENANT database, not the")
        print("  control-plane one:  python dev_provision.py status")
    return 0 if resp.status_code < 300 else 1


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--backend", default=DEFAULT_BACKEND,
                   help=f"backend base URL (default {DEFAULT_BACKEND})")
    sub = p.add_subparsers(dest="cmd", required=True)

    def store_args(sp, *, need_license=False):
        if need_license:
            sp.add_argument("--license-id", required=True, help="e.g. dev-001")
        sp.add_argument("--tenant-uuid", default=None)
        sp.add_argument("--site-domain", required=True)
        sp.add_argument("--wp-base-url", required=True)
        sp.add_argument("--woo-key", required=True)
        sp.add_argument("--woo-secret", default=os.environ.get("WOO_SECRET"),
                        help="or set WOO_SECRET")

    sp = sub.add_parser("provision", help="paid activation")
    store_args(sp, need_license=True)
    sp.set_defaults(func=cmd_provision)

    sp = sub.add_parser("free", help="free activation")
    store_args(sp)
    sp.set_defaults(func=cmd_free)

    sp = sub.add_parser("deactivate", help="teardown via store credentials")
    sp.add_argument("--tenant-uuid", required=True)
    sp.add_argument("--woo-key", required=True)
    sp.add_argument("--woo-secret", default=os.environ.get("WOO_SECRET"))
    sp.set_defaults(func=cmd_deactivate)

    sp = sub.add_parser("revoke", help="teardown via signed licence")
    sp.add_argument("--license-id", required=True)
    sp.add_argument("--tenant-uuid", default=None)
    sp.set_defaults(func=cmd_revoke)

    sp = sub.add_parser("shopify", help="convert a tenant row to the Shopify backend")
    sp.add_argument("--tenant-uuid", required=True)
    sp.add_argument("--shopify-domain", required=True, help="e.g. my-store.myshopify.com")
    sp.add_argument("--dsn", default=None, help="defaults to .env's DATABASE_URL")
    sp.set_defaults(func=cmd_shopify)

    sp = sub.add_parser("event", help="send a signed Shopify webhook")
    sp.add_argument("--topic", required=True,
                    choices=["order-paid", "app-uninstalled", "product-update"])
    sp.add_argument("--shopify-domain", required=True)
    sp.add_argument("--session-id", default="dev-session-1",
                    help="order-paid: the miraq_session_id note attribute")
    sp.add_argument("--body", default=None, help="raw JSON body, overrides the built-in sample")
    sp.add_argument("--client-secret", default=None, help="defaults to SHOPIFY_CLIENT_SECRET")
    sp.set_defaults(func=cmd_event)

    sp = sub.add_parser("status", help="dump every tenant from the control-plane DB")
    sp.add_argument("--dsn", default=None, help="defaults to $DATABASE_URL")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("wait", help="poll until a tenant finishes building")
    sp.add_argument("--tenant-uuid", required=True)
    sp.add_argument("--dsn", default=None, help="defaults to $DATABASE_URL")
    sp.add_argument("--timeout", type=int, default=300)
    sp.add_argument("--interval", type=int, default=5)
    sp.set_defaults(func=cmd_wait)

    args = p.parse_args()

    if getattr(args, "woo_secret", "sentinel") is None:
        raise SystemExit("Provide --woo-secret or set WOO_SECRET")

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())