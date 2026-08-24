"""
ecommerce/shopify_proxy.py — Shopify App Proxy request authentication.

The problem this solves
──────────────────────
Until now, the customer's identity arrived as a plain JSON/query field
supplied by the browser (``user_context.customer_id``, ``?customer_id=``).
Anyone could send any value with curl and read another shopper's order
history or saved addresses. Phase 3 stopped "any order number reads any
order"; this module stops "any caller can claim to be any customer".

How it works
────────────
The widget calls the backend through a Shopify **App Proxy** path on the
store's own domain (e.g. ``https://store.myshopify.com/apps/miraq/chat``).
Shopify forwards the request to us and appends query parameters:

    shop, path_prefix, timestamp, signature
    logged_in_customer_id   ← present ONLY when a customer is logged in

``signature`` is an HMAC-SHA256 over the other parameters, keyed with the
app's client secret. Because only Shopify and we hold that secret, a valid
signature proves the request really came through Shopify, and therefore that
``logged_in_customer_id`` reflects a real authenticated session rather than a
browser-supplied claim.

Signature algorithm (see VERIFY below):
    1. drop ``signature``
    2. sort the remaining parameters by key
    3. render each as ``key=value`` (repeated values joined with ",")
    4. concatenate with no separator
    5. HMAC-SHA256 with the app client secret, hex digest
    6. constant-time compare against the supplied ``signature``

VERIFY (open item C8): this matches Shopify's documented app-proxy scheme as
of my knowledge, but it could NOT be checked against live docs in the build
environment. It is also the one thing here that must be exactly right — a
subtly wrong construction either rejects every real request or, worse,
accepts forged ones. Confirm against the current "Authenticate app proxy
requests" documentation, and use the dev-store test in the Phase 7 notes
(a request with a tampered parameter must be rejected).

Note the distinction from webhooks: webhooks use a base64 HMAC over the raw
request BODY in an ``X-Shopify-Hmac-Sha256`` header. This is the query-string
scheme, and the two are not interchangeable.
"""

import base64
import hashlib
import hmac
import time
from typing import Mapping, Optional, Tuple

from chat_logger import get_logger

logger = get_logger("miraq_chat")

# Identity modes (see app_config.SHOPIFY_CUSTOMER_AUTH).
MODE_APP_PROXY = "app_proxy"
MODE_INSECURE_CLIENT_CLAIM = "insecure_client_claim"

SIGNATURE_PARAM = "signature"
CUSTOMER_PARAM = "logged_in_customer_id"
TIMESTAMP_PARAM = "timestamp"


def _canonical_query(params: Mapping[str, object]) -> str:
    """Build the string Shopify signs: sorted ``key=value`` pairs, concatenated.

    Repeated parameters are joined with "," in the order received, matching
    Shopify's handling of array-style query parameters.
    """
    parts = []
    for key in sorted(params.keys()):
        if key == SIGNATURE_PARAM:
            continue
        value = params[key]
        if isinstance(value, (list, tuple)):
            rendered = ",".join(str(v) for v in value)
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return "".join(parts)


def verify_app_proxy_signature(
    params: Mapping[str, object],
    client_secret: str,
    *,
    max_age_seconds: Optional[int] = None,
) -> Tuple[bool, str]:
    """Return ``(ok, reason)`` for a proxied request's query parameters.

    ``reason`` is a short machine-ish string for logging; it is never shown to
    the shopper. Every failure path returns False — there is deliberately no
    "couldn't check, allow anyway" branch, because that would reintroduce the
    hole this module exists to close.
    """
    if not client_secret:
        # Misconfiguration, not an attack — but we still cannot authenticate,
        # so the request must not be trusted.
        return False, "no_client_secret_configured"

    supplied = params.get(SIGNATURE_PARAM)
    if isinstance(supplied, (list, tuple)):
        supplied = supplied[0] if supplied else None
    if not supplied:
        return False, "missing_signature"

    expected = hmac.new(
        client_secret.encode("utf-8"),
        _canonical_query(params).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Constant-time compare: a short-circuiting == leaks how much of the
    # signature matched, which is enough to forge one byte at a time.
    if not hmac.compare_digest(expected, str(supplied)):
        return False, "signature_mismatch"

    # Optional replay window. The signature covers `timestamp`, so an attacker
    # cannot change it, but a captured URL would otherwise stay valid forever.
    if max_age_seconds:
        raw_ts = params.get(TIMESTAMP_PARAM)
        if isinstance(raw_ts, (list, tuple)):
            raw_ts = raw_ts[0] if raw_ts else None
        try:
            age = time.time() - int(str(raw_ts))
        except (TypeError, ValueError):
            return False, "bad_timestamp"
        # Allow a little clock skew in the future direction.
        if age > max_age_seconds or age < -300:
            return False, "stale_timestamp"

    return True, "ok"


def customer_id_from_proxy(params: Mapping[str, object]) -> Optional[str]:
    """Extract the authenticated customer id, or None for a guest.

    Only meaningful after ``verify_app_proxy_signature`` has returned True —
    the value is part of the signed payload, so it is trustworthy exactly when
    the signature is.

    Shopify sends the parameter empty (or omits it) for logged-out visitors;
    both mean "guest", never "unknown, assume the client's word for it".
    """
    raw = params.get(CUSTOMER_PARAM)
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    value = str(raw or "").strip()
    return value or None


def resolve_shopify_customer_id(
    params: Mapping[str, object],
    *,
    mode: str,
    client_secret: str,
    claimed_customer_id=None,
    max_age_seconds: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the customer identity for a Shopify request.

    Returns ``(customer_id, error)``. ``error`` non-None means the request must
    be rejected outright — do not fall back to the claimed id.

    In ``app_proxy`` mode the client-supplied id is ignored completely, even
    when it agrees with the proxy: accepting it "because it matches" would
    make the check decorative.
    """
    if mode == MODE_INSECURE_CLIENT_CLAIM:
        # Development escape hatch. app_config logs a startup warning; this
        # logs per request so it cannot go unnoticed in a deployed environment.
        logger.warning(
            "SHOPIFY_CUSTOMER_AUTH=insecure_client_claim — trusting a "
            "client-supplied customer_id. This allows any caller to read any "
            "customer's orders and addresses. NEVER use this in production."
        )
        return (str(claimed_customer_id) if claimed_customer_id else None), None

    ok, reason = verify_app_proxy_signature(
        params, client_secret, max_age_seconds=max_age_seconds
    )
    if not ok:
        logger.error(f"Shopify app proxy verification failed | reason={reason}")
        return None, reason

    resolved = customer_id_from_proxy(params)

    if claimed_customer_id and str(claimed_customer_id) != str(resolved or ""):
        # Not necessarily an attack (a stale cached page can do this), but it
        # is always worth seeing, and the proxy value always wins.
        logger.warning(
            "Shopify: ignoring client-claimed customer_id "
            f"{str(claimed_customer_id)!r}; proxy says {resolved!r}"
        )

    return resolved, None


def verify_events_hmac(raw_body: bytes, header_value: Optional[str], client_secret: str) -> Tuple[bool, str]:
    """Verify an Events (or webhook) HTTPS delivery. See the module docstring's
    note on the distinction from the App Proxy scheme above — this is the
    OTHER one: base64 HMAC-SHA256 over the raw request body, not a
    query-string signature.

    Must be computed over the exact bytes Shopify sent, before any JSON
    parsing — re-serializing the parsed body can reorder keys or change
    whitespace and silently break every signature.

    Returns ``(ok, reason)``, same shape as ``verify_app_proxy_signature`` and
    for the same reason: every failure path returns False, with no
    "couldn't check, allow anyway" branch.
    """
    if not client_secret:
        return False, "no_client_secret_configured"
    if not header_value:
        return False, "missing_hmac_header"

    expected = base64.b64encode(
        hmac.new(client_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")

    if not hmac.compare_digest(expected, header_value):
        return False, "signature_mismatch"

    return True, "ok"