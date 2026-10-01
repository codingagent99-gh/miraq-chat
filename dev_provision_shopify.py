#!/usr/bin/env python3
"""
dev_provision_shopify.py — set up and talk to a Shopify store on a LOCAL backend.

One-time, in .env:
    DEV_PROVISIONING_ENABLED=true
    SHOPIFY_CLIENT_ID=...          # the app installed on your dev store
    SHOPIFY_CLIENT_SECRET=...
    DEV_SHOPIFY_SHOP=my-store.myshopify.com   # optional: lets you omit --shop

Then, with the backend running:

    python dev_provision_shopify.py setup
    python dev_provision_shopify.py chat "show me lexi and luna"

Commands
--------
  setup    Create (or refresh) the tenant, get + check the Admin API token,
           build the catalog, and wait until it is ready. Safe to re-run:
           the tenant, its database and chat history are reused, and the
           running backend picks up the new catalog without a restart.
             --admin-token shpat_...  use a token you already have instead of
                                      the client_credentials grant
             --client-id ID           use this app instead of .env's; with
             --client-secret SECRET   the secret it is registered on the fly
             --force                  overwrite a store that was installed
                                      through the real OAuth flow in this
                                      database (see "Published stores")
             --timeout N              seconds to wait for the catalog (a big
                                      catalog takes longer; default 600)
  status   Tenant row, token and catalog state.
  chat     Send one message as the storefront widget would. Keeps a session
           per shop so follow-ups ("only white ones") work; --new starts over.
  remove   Archive the tenant (same as uninstalling the app). The database is
           kept for the grace period; `setup` brings it back.

How the token is obtained (default): the client_credentials grant with the
app's credentials. Shopify only allows that when the app belongs to the SAME
organisation as the store and is installed on it — true for a development
store in your Partner organisation. If it is refused, `setup` says so.

Published stores
----------------
A live store is not in your Partner organisation, so the default grant is
refused. Use one of:
    setup --client-id ID --client-secret SECRET   an app created in the
                                                  store's own organisation
                                                  and installed on it
    setup --admin-token shpat_...                 a token you already have
The local backend only READS from the store (catalog, products, order
status); it creates nothing there, and `remove` only archives the local
tenant — the app stays installed in the store.
If the store's real OAuth install lives in the same database (e.g. you
pointed DATABASE_URL at a shared one), `setup` refuses to overwrite it:
doing so would break that install within the hour. Use a local database.

Running against a backend that is not on this machine: set
DEV_PROVISIONING_KEY to the same value on both sides (the script sends it as
X-Dev-Provisioning-Key). Local requests don't need it.

Talks only to the backend's /dev/shopify/* routes (routes/dev_shopify.py) and
/chat. Those routes 404 unless DEV_PROVISIONING_ENABLED is true.
"""

import argparse
import json
import os
import sys
import time
import uuid

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_SESSIONS_FILE = os.path.join(_HERE, ".dev_shopify_sessions.json")


def _env(name: str, default: str = "") -> str:
    """Shell first, then the project's .env (without exporting it)."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        from dotenv import dotenv_values
        return (dotenv_values(os.path.join(_HERE, ".env")) or {}).get(name) or default
    except Exception:
        return default


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _call(method: str, backend: str, path: str, **kw):
    url = f"{backend.rstrip('/')}{path}"
    key = _env("DEV_PROVISIONING_KEY")
    if key and path.startswith("/dev/"):
        kw.setdefault("headers", {})["X-Dev-Provisioning-Key"] = key
    try:
        resp = requests.request(method, url, timeout=kw.pop("timeout", 60), **kw)
    except requests.RequestException as e:
        sys.exit(f"✗ could not reach the backend at {backend} ({type(e).__name__}).\n"
                 "  Is it running? Point --backend (or MIRAQ_DEV_BACKEND) at it.")
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:800]}
    # The dev routes can be unreachable for three different reasons, and each
    # looks different on the wire — say which one it is rather than guess.
    if path.startswith("/dev/"):
        err = str(body.get("error", ""))
        if resp.status_code == 404 and err == "not found":
            # routes/dev_shopify.py answered: installed, but switched off.
            sys.exit(f"✗ the backend at {backend} has the dev routes, but they are switched off.\n"
                     "  Add DEV_PROVISIONING_ENABLED=true to THAT backend's .env (the one in the\n"
                     "  folder it runs from), then restart it — .env is only read at startup.")
        if resp.status_code == 404 and "no tenant" not in err:
            # Flask's own 404: nothing is registered at /dev/shopify/.
            sys.exit(f"✗ the backend at {backend} has no dev routes at all.\n"
                     "  Its server.py doesn't register them: copy BOTH server.py and\n"
                     "  routes/dev_shopify.py from shopify-dev-provisioning.zip, then restart.\n"
                     "  (Or this is a different backend — check --backend / MIRAQ_DEV_BACKEND.)")
        if resp.status_code == 400 and "missing tenant" in err:
            # Tenant resolution ran on a path it should skip.
            sys.exit(f"✗ the backend at {backend} is asking for a licence id on the dev routes.\n"
                     "  Its store_registry.py is the old one: copy store_registry.py from\n"
                     "  shopify-dev-provisioning.zip, then restart.")
    if resp.status_code == 403 and path.startswith("/dev/"):
        sys.exit("✗ the backend refused: it only accepts dev requests from its own machine.\n"
                 "  For a remote backend, set the same DEV_PROVISIONING_KEY in both .env files.")
    return resp.status_code, body


def _tenant(backend: str, shop: str):
    code, body = _call("GET", backend, "/dev/shopify/tenant", params={"shop": shop})
    return body if code == 200 else None


# ── output ───────────────────────────────────────────────────────────────────

def _print_tenant(t: dict) -> None:
    print(f"  shop        : {t.get('shop')}")
    print(f"  status      : {t.get('status')}")
    print(f"  license_id  : {t.get('license_id')}")
    print(f"  tenant_id   : {t.get('tenant_id')}")
    print(f"  database    : {t.get('db_name')}")
    tok = t.get("token") or {}
    if tok:
        print(f"  token       : {tok.get('source')} (expires {tok.get('expires_at')})")
        if tok.get("last_error"):
            print(f"  token error : {tok['last_error']}")
    loader = t.get("loader")
    if loader:
        state = "DEGRADED — " + "; ".join(loader.get("reasons") or []) if loader.get("degraded") else "ok"
        print(f"  catalog     : {loader.get('products')} products, "
              f"{loader.get('categories')} categories ({state})")
    elif t.get("status") == "active":
        print("  catalog     : not loaded in this backend process yet (loads on first chat)")
    if t.get("last_build_error"):
        print(f"  build error : {t['last_build_error']}")


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_setup(args) -> int:
    payload = {"shop": args.shop}
    if args.admin_token:
        payload["admin_token"] = args.admin_token
    if args.client_id:
        payload["client_id"] = args.client_id
    if args.client_secret:
        payload["client_secret"] = args.client_secret
    if args.force:
        payload["force"] = True

    print(f"→ provisioning {args.shop} on {args.backend}")
    code, body = _call("POST", args.backend, "/dev/shopify/tenant", json=payload)
    if code != 200 or not body.get("success"):
        step = body.get("step")
        print(f"✗ failed{f' at {step}' if step else ''} (HTTP {code})")
        print(f"  {body.get('error') or body}")
        if body.get("where"):
            print(f"  at {body['where']} — full traceback in the backend log (logs/<date>/chat.txt)")
        elif code >= 500 and not step:
            print("  The backend hid the error. Make sure it runs the current routes/dev_shopify.py\n"
                  "  (restart it after updating); the traceback is in logs/<date>/chat.txt.")
        return 1

    print(f"✓ {'re-provisioned' if body.get('reinstall') else 'created'} tenant for "
          f"“{body.get('shop_name')}” — token: {body.get('token_source')}")
    print("  building catalog", end="", flush=True)

    deadline = time.time() + args.timeout
    t = body
    while time.time() < deadline:
        t = _tenant(args.backend, args.shop) or t
        if t.get("status") in ("active", "provision_failed"):
            break
        print(".", end="", flush=True)
        time.sleep(3)
    print()

    _print_tenant(t)
    if t.get("status") != "active":
        if t.get("status") == "warming":
            print(f"\n… still building after {args.timeout}s — check `status` in a minute.")
        else:
            print("\n✗ catalog build failed — see the build error above and the backend log.")
        return 1

    print("\nReady. Try:")
    print(f'  python dev_provision_shopify.py chat{"" if _env("DEV_SHOPIFY_SHOP") else f" --shop {args.shop}"} '
          '"show me your products"')
    print("  or with curl:")
    print(f"  curl -X POST {args.backend}/chat -H 'Content-Type: application/json' \\")
    print(f"       -H 'X-MiraQ-License-Id: {t.get('license_id')}' \\")
    print("       -d '{\"message\": \"show me your products\", \"platform\": \"shopify\"}'")
    return 0


def cmd_status(args) -> int:
    t = _tenant(args.backend, args.shop)
    if t is None:
        print(f"✗ no tenant for {args.shop} — run `setup` first")
        return 1
    _print_tenant(t)
    return 0


def _load_sessions() -> dict:
    try:
        with open(_SESSIONS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cmd_chat(args) -> int:
    t = _tenant(args.backend, args.shop)
    if t is None:
        print(f"✗ no tenant for {args.shop} — run `setup` first")
        return 1

    sessions = _load_sessions()
    if args.new or args.shop not in sessions:
        sessions[args.shop] = str(uuid.uuid4())
        try:
            with open(_SESSIONS_FILE, "w") as f:
                json.dump(sessions, f, indent=2)
        except OSError:
            pass  # a fresh session every time is fine too

    message = " ".join(args.message)
    code, body = _call(
        "POST", args.backend, "/chat",
        json={"message": message, "platform": "shopify", "page": args.page},
        headers={
            "X-MiraQ-License-Id": t["license_id"],
            "X-MiraQ-Session": sessions[args.shop],
        },
        timeout=120,
    )

    print(f"you › {message}")
    print(f"bot › {body.get('bot_message') or body}")
    products = body.get("products") or []
    if products:
        print("\n  products:")
        for p in products:
            price = f"  {p.get('price')}" if p.get("price") not in (None, "", 0) else ""
            print(f"   • {p.get('name')}  (id {p.get('id')}){price}")
    if body.get("suggestions"):
        print(f"\n  suggestions: {' | '.join(body['suggestions'])}")
    if args.verbose:
        print(f"\n  intent={body.get('intent')} http={code}")
        print(f"  metadata={json.dumps(body.get('metadata'), default=str)[:800]}")
    return 0 if code < 300 else 1


def cmd_remove(args) -> int:
    code, body = _call("DELETE", args.backend, "/dev/shopify/tenant", params={"shop": args.shop})
    if code == 200 and body.get("success"):
        note = " (already archived)" if body.get("already_archived") else ""
        print(f"✓ {args.shop} archived{note}. Database kept — `setup` restores it.")
        return 0
    print(f"✗ HTTP {code}: {body.get('error') or body}")
    return 1


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--backend", default=_env("MIRAQ_DEV_BACKEND", "http://localhost:5009"),
                   help="backend base URL (default: MIRAQ_DEV_BACKEND or http://localhost:5009)")
    p.add_argument("--shop", default=_env("DEV_SHOPIFY_SHOP"),
                   help="store domain, e.g. my-store.myshopify.com (default: DEV_SHOPIFY_SHOP)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup", help="create/refresh the tenant and build its catalog")
    sp.add_argument("--admin-token", default=_env("DEV_SHOPIFY_ADMIN_TOKEN"),
                    help="use this Admin API token instead of client_credentials")
    sp.add_argument("--client-id", default=_env("DEV_SHOPIFY_CLIENT_ID") or None,
                    help="use this app instead of .env's SHOPIFY_CLIENT_ID")
    sp.add_argument("--client-secret", default=_env("DEV_SHOPIFY_CLIENT_SECRET") or None,
                    help="the --client-id app's secret (registers it if new)")
    sp.add_argument("--force", action="store_true",
                    help="overwrite a store installed through the real OAuth flow")
    sp.add_argument("--timeout", type=int, default=600, help="seconds to wait for the catalog build")
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser("status", help="show tenant, token and catalog state")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("chat", help="send a chat message as the storefront widget")
    sp.add_argument("message", nargs="+")
    sp.add_argument("--new", action="store_true", help="start a new conversation")
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("-v", "--verbose", action="store_true", help="also print intent and metadata")
    sp.set_defaults(func=cmd_chat)

    sp = sub.add_parser("remove", help="archive the tenant (like uninstalling the app)")
    sp.set_defaults(func=cmd_remove)

    # --shop is accepted after the command too ("setup --shop x"), which is
    # where people naturally type it. SUPPRESS keeps an absent sub-level flag
    # from overwriting the top-level value / DEV_SHOPIFY_SHOP default.
    for sp in sub.choices.values():
        sp.add_argument("--shop", default=argparse.SUPPRESS,
                        help="store domain, e.g. my-store.myshopify.com")

    args = p.parse_args()

    shop = (args.shop or "").strip().lower()
    shop = shop.removeprefix("https://").removeprefix("http://").rstrip("/")
    if not shop:
        p.error("no store given — add --shop my-store.myshopify.com, or set DEV_SHOPIFY_SHOP in .env")
    if "." not in shop:
        shop = f"{shop}.myshopify.com"
    if not shop.endswith(".myshopify.com"):
        p.error(f"{shop!r} looks like the store's own web domain. Use its myshopify.com domain "
                "instead (Shopify admin → Settings → Domains), e.g. my-store.myshopify.com")
    args.shop = shop

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
