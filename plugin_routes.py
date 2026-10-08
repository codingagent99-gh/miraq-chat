"""
plugin_routes.py — Where the MiraQ WordPress plugin's REST routes live for one store.

Plugin 1.0.6 moved its routes to the `miraq/v1` namespace (WordPress.org requires a
plugin-specific prefix). Stores still on an older plugin only answer on the old
addresses:

    old                                  new (plugin >= 1.0.6)
    custom-api/v1/<path>                 miraq/v1/<path>          (same <path>)
    wdget-logo-uploader/v1/data          miraq/v1/branding

1.0.6 keeps answering on the old addresses too while MIRAQ_LEGACY_REST_ROUTES is
true, but a later release turns them off. So the backend cannot hardcode either
namespace: it asks each store once (detect_plugin_namespace) and builds every
plugin URL from the answer.

When it runs:
  * TenantRegistry._rehydrate — before the StoreLoader is built, so the catalog
    load, search, orders, etc. all use the right base from the first call.
  * widget_branding.fetch_and_store_widget_branding — the daily per-tenant
    refresh. It re-detects and updates a resident loader in place, so a store
    that upgrades its plugin is picked up within a day without a restart.

Detection is one unauthenticated GET of miraq/v1/refresh-nonce, a public route
that only exists on plugin >= 1.0.6. A 2xx means the new namespace; anything
else (404 from an older plugin, an error, a timeout) falls back to the old one,
which every plugin version answers on today.
"""

from __future__ import annotations

from typing import Optional

import requests

from chat_logger import get_logger

logger = get_logger("miraq_chat")

NEW_NAMESPACE = "miraq/v1"
LEGACY_NAMESPACE = "custom-api/v1"

# Branding moved path as well as namespace.
_NEW_BRANDING_PATH = "/branding"
_LEGACY_BRANDING_URL_SUFFIX = "/wp-json/wdget-logo-uploader/v1/data"

# Public on every plugin >= 1.0.6; absent (404) on older ones.
_DETECT_PATH = "/refresh-nonce"


def plugin_api_base(wp_base: str, namespace: str) -> str:
    """{wp_base}/wp-json/<namespace> — the base every custom_plugin call joins onto."""
    return f"{(wp_base or '').rstrip('/')}/wp-json/{namespace}"


def namespace_of(custom_api_base: str) -> str:
    """Which namespace an existing base URL points at."""
    return NEW_NAMESPACE if f"/wp-json/{NEW_NAMESPACE}" in (custom_api_base or "") else LEGACY_NAMESPACE


def branding_url(wp_base: str, namespace: str) -> str:
    """URL of the authenticated branding route for this namespace."""
    if namespace == NEW_NAMESPACE:
        return plugin_api_base(wp_base, NEW_NAMESPACE) + _NEW_BRANDING_PATH
    return f"{(wp_base or '').rstrip('/')}{_LEGACY_BRANDING_URL_SUFFIX}"


def detect_plugin_namespace(wp_base: str, *, state=None,
                            session: Optional[requests.Session] = None,
                            timeout: int = 10, license_id: str = "") -> str:
    """
    NEW_NAMESPACE when the store's plugin answers on miraq/v1, else LEGACY_NAMESPACE.

    `state` is the tenant's HttpProfileState. When given, the probe goes through
    http_profiles.send so it uses the same firewall header profile as every
    other call to this store (a WAF that blocks the default profile would
    otherwise make every store look like an old plugin).

    Never raises: any failure falls back to the legacy namespace, which all
    current plugin versions answer on.
    """
    wp_base = (wp_base or "").rstrip("/")
    if not wp_base:
        return LEGACY_NAMESPACE

    url = plugin_api_base(wp_base, NEW_NAMESPACE) + _DETECT_PATH
    session = session or requests.Session()
    try:
        if state is not None:
            from http_profiles import send as http_send
            resp = http_send(session, "GET", url, state=state, timeout=timeout)
        else:
            resp = session.get(url, timeout=timeout)
    except Exception as e:
        logger.warning(
            f"plugin_routes: namespace probe failed — using {LEGACY_NAMESPACE} | "
            f"license_id={license_id!r} | {type(e).__name__}: {e}"
        )
        return LEGACY_NAMESPACE

    if 200 <= resp.status_code < 300:
        namespace = NEW_NAMESPACE
    else:
        namespace = LEGACY_NAMESPACE
    logger.info(
        f"plugin_routes: namespace={namespace} (probe HTTP {resp.status_code}) | "
        f"license_id={license_id!r}"
    )
    return namespace