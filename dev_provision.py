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

DEFAULT_BACKEND = os.environ.get("MIRAQ_DEV_BACKEND", "http://localhost:5009")
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:admin@localhost:5432/miraq_chat_multi")


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
    dsn = args.dsn or DEFAULT_DSN
    if not dsn:
        raise SystemExit("No control-plane DSN. Pass --dsn or set DATABASE_URL.")
    return dsn


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
    try:
        conn = psycopg2.connect(dsn)
    except Exception as e:
        print(f"✗ cannot connect to control-plane DB: {e}")
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
    import psycopg2
    dsn = _dsn(args)
    deadline = time.time() + args.timeout

    while time.time() < deadline:
        conn = psycopg2.connect(dsn)
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