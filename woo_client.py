"""
WooCommerce API client for executing API calls.
"""

from typing import List
import json as _json
import threading as _threading
import time as _health_time
import requests as http_requests
from requests.auth import HTTPBasicAuth

from models import WooAPICall
from chat_logger import get_logger, get_api_logger, sanitize_url

logger = get_logger("miraq_chat")
api_logger = get_api_logger()


# ══════════════════════════════════════════════════════════════════════
# Upstream (WooCommerce/WordPress) health, observed from real traffic.
#
# An HTTP status code is not enough to tell whether the store is usable.
# wp-social returned 200 with Content-Type: application/json while every
# parent-product body was unparseable — any status-only probe would have
# reported a perfectly healthy store throughout the outage.
#
# So health is recorded from the calls the app already makes, rather than
# from a synthetic probe: no extra traffic, and it measures exactly what
# the app actually experiences.
#
# Three outcomes, deliberately distinct:
#   ok       — parsed first time
#   salvaged — body was polluted but valid JSON was recovered. DEGRADED,
#              not down: the caller got correct data. Surfacing it as an
#              outage would black out the widget for a working store.
#   failed   — no usable data at all.
#
# NOTE (multi-store): these counters are PROCESS-WIDE, not per-tenant. One
# tenant's broken store therefore colours the reading for every tenant
# served by the same process. Keying _UPSTREAM by license id is the correct
# fix once the health signal is actually surfaced per tenant; until then
# this is a coarse process-level signal and should be read as one.
# ══════════════════════════════════════════════════════════════════════
_UPSTREAM_LOCK = _threading.Lock()
_UNHEALTHY_AFTER = 3        # consecutive hard failures before "down"
_SALVAGE_WINDOW_S = 300     # a salvage older than this stops mattering

_UPSTREAM = {
    "consecutive_failures": 0,
    "last_success_ts": None,
    "last_failure_ts": None,
    "last_failure_endpoint": "",
    "last_failure_error": "",
    "last_salvage_ts": None,
    "last_salvage_endpoint": "",
    "salvage_count": 0,
}


def _record_upstream(outcome, endpoint="", error=""):
    """Record one API outcome: 'ok', 'salvaged' or 'failed'."""
    _now = _health_time.time()
    with _UPSTREAM_LOCK:
        if outcome == "failed":
            _UPSTREAM["consecutive_failures"] += 1
            _UPSTREAM["last_failure_ts"] = _now
            _UPSTREAM["last_failure_endpoint"] = str(endpoint)
            _UPSTREAM["last_failure_error"] = str(error)[:200]
            return
        # Any usable response clears the failure streak.
        _UPSTREAM["consecutive_failures"] = 0
        _UPSTREAM["last_success_ts"] = _now
        if outcome == "salvaged":
            _UPSTREAM["salvage_count"] += 1
            _UPSTREAM["last_salvage_ts"] = _now
            _UPSTREAM["last_salvage_endpoint"] = str(endpoint)


def upstream_health():
    """Snapshot for /health. 'down' | 'degraded' | 'ok'.

    down     — _UNHEALTHY_AFTER consecutive hard failures, nothing usable since
    degraded — usable, but a body needed salvaging recently (a real server-side
               fault that has not yet cost the user anything)
    """
    _now = _health_time.time()
    with _UPSTREAM_LOCK:
        _snap = dict(_UPSTREAM)

    _fails = _snap["consecutive_failures"]
    _recent_salvage = (
        _snap["last_salvage_ts"] is not None
        and (_now - _snap["last_salvage_ts"]) <= _SALVAGE_WINDOW_S
    )

    if _fails >= _UNHEALTHY_AFTER:
        _status, _reasons = "down", [
            f"{_fails} consecutive upstream failures",
            f"last: {_snap['last_failure_endpoint']} — {_snap['last_failure_error']}",
        ]
    elif _recent_salvage:
        _status, _reasons = "degraded", [
            "upstream returned a polluted response body that had to be salvaged",
            f"last: {_snap['last_salvage_endpoint']}",
        ]
    elif _fails:
        _status, _reasons = "degraded", [f"{_fails} recent upstream failure(s)"]
    else:
        _status, _reasons = "ok", []

    return {
        "status": _status,
        "reasons": _reasons,
        "consecutive_failures": _fails,
        "salvage_count": _snap["salvage_count"],
        "seconds_since_success": (
            round(_now - _snap["last_success_ts"], 1)
            if _snap["last_success_ts"] else None
        ),
    }


def _parse_json_tolerant(resp, endpoint_short):
    """resp.json(), but survives a body with PHP notices printed in front of it.

    A misbehaving WP plugin that emits warnings during a REST request writes them
    to the output stream BEFORE WordPress serialises its response, so the body is
    literally `<br /><b>Warning</b>: ...` followed by perfectly good JSON, served
    with Content-Type: application/json and a 200. Strict json.loads() rejects the
    whole thing at column 1 and a complete, valid payload is thrown away.

    raw_decode() from the first brace parses the first complete JSON value and
    ignores anything after it, which also covers notices emitted at the end.

    Deliberately loud: this masks a real server-side fault, and a silent recovery
    would let it sit there forever. Every salvage logs an error with the leading
    junk so the offending plugin and file stay visible in the logs.
    """
    try:
        return resp.json(), False
    except ValueError as exc:
        _strict_error = exc

    text = resp.text or ""
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i != -1),
        default=-1,
    )
    if start == -1:
        raise _strict_error

    try:
        data, _end = _json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        # No complete JSON value after the junk either — the body really is
        # broken. Surface the ORIGINAL decode error so the failure log and the
        # caller-visible error read exactly as they did before this helper.
        raise _strict_error

    logger.error(
        f"API body polluted before JSON: {endpoint_short} | "
        f"salvaged {len(text) - start} of {len(text)} bytes | "
        f"leading_junk={text[:start][:300]!r}"
    )
    return data, True

# Minimal headers that pass WordPress.com Atomic's bot detection.
# Full BROWSER_HEADERS with query-string credentials triggered 429s.
_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    # "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":     "application/json",
}
# _WC_AUTH and _CUSTOM_API_HEADERS are now built per-call inside execute()
# from the request-scoped loader, so they carry the correct tenant credentials.

class WooClient:
    """Executes WooCommerce API calls."""

    def __init__(self):
        self.session = http_requests.Session()
        self.session.headers.update(_BASE_HEADERS)

    def execute(self, api_call: WooAPICall) -> dict:
        """Execute a single API call and return raw response."""
        import json as _json
        import time as _time
        from store_registry import get_store_loader

        # Resolve credentials and base URLs from the request-scoped loader so
        # every call uses the correct tenant — not the process-level env globals.
        loader = get_store_loader()
        if not loader:
            raise RuntimeError(
                "woo_client.execute(): no tenant loader resolved — "
                "is X-MiraQ-License-Id missing from the request?"
            )
        _key         = loader.consumer_key
        _secret      = loader.consumer_secret
        _woo_base    = loader.base
        _custom_base = loader.custom_api_base
        _wc_auth        = HTTPBasicAuth(_key, _secret)
        _custom_headers = {**_BASE_HEADERS, "X-Consumer-Key": _key, "X-Consumer-Secret": _secret}

        params = dict(api_call.params)
        is_custom_api = api_call.surface == "custom_plugin"

        # Resolve relative endpoints to full URLs
        endpoint = api_call.endpoint
        if not endpoint.startswith("http"):
            base = _custom_base if is_custom_api else _woo_base
            endpoint = base.rstrip("/") + endpoint

        # Auth strategy:
        #   - custom-api/v1/*  → X-Consumer-Key / X-Consumer-Secret headers
        #   - wc/v3/*          → HTTPBasicAuth (no credentials in query string)
        auth    = None           if is_custom_api else _wc_auth
        headers = _custom_headers if is_custom_api else {}

        # ── Logging ───────────────────────────────────────────────────────────
        sanitized_endpoint = sanitize_url(endpoint)
        endpoint_short     = sanitized_endpoint.split("/")[-1]
        safe_params        = {k: v for k, v in params.items()
                              if k not in ("consumer_key", "consumer_secret")}

        context = ""
        if api_call.session_id or api_call.user_message:
            context = f" | session={api_call.session_id} | q={api_call.user_message!r}"

        if api_call.method == "GET":
            api_logger.info(f"REQUEST GET {sanitized_endpoint} | params={safe_params}{context}")
        else:
            try:
                body_str = _json.dumps(api_call.body, separators=(",", ":"))
            except Exception:
                body_str = str(api_call.body)
            api_logger.info(f"REQUEST {api_call.method} {sanitized_endpoint} | body={body_str}{context}")

        logger.info(f"API call: {api_call.method} {endpoint_short} | params={safe_params}")

        _req_start = _time.time()

        # Bound before the try so the failure handler below can still describe
        # the response when the request SUCCEEDED and only parsing blew up —
        # requests' JSONDecodeError carries no .response, which is why a 2xx
        # with an empty or HTML body used to log nothing but "Expecting value:
        # line 1 column 1 (char 0)".
        resp = None

        try:
            if api_call.method == "GET":
                resp = self.session.get(
                    endpoint,
                    auth=auth,
                    headers=headers,
                    params=params,
                    timeout=45,
                )
            else:
                resp = self.session.request(
                    method=api_call.method,
                    url=endpoint,
                    auth=auth,
                    headers=headers,
                    params=params,
                    json=api_call.body,
                    timeout=45,
                )

            resp.raise_for_status()
            _elapsed_ms = round((_time.time() - _req_start) * 1000)
            data, _salvaged = _parse_json_tolerant(resp, endpoint_short)
            _record_upstream("salvaged" if _salvaged else "ok", endpoint_short)

            # ── Response logging ──────────────────────────────────────────────
            if isinstance(data, dict) and "products" in data:
                items = data.get("products", [])
            elif isinstance(data, list):
                items = data
            else:
                items = []

            if items:
                product_summary = ", ".join(
                    f"{p.get('id')}:{p.get('name', '?')}" for p in items[:20]
                )
                api_logger.info(
                    f"RESPONSE {api_call.method} {endpoint_short} | "
                    f"status={resp.status_code} | count={len(items)} | "
                    f"time_ms={_elapsed_ms} | products=[{product_summary}]"
                )
            else:
                api_logger.info(
                    f"RESPONSE {api_call.method} {endpoint_short} | "
                    f"status={resp.status_code} | count={len(items)} | "
                    f"time_ms={_elapsed_ms}"
                )

            logger.info(
                f"API response: {endpoint_short} | status={resp.status_code} | "
                f"count={len(items)} | time_ms={_elapsed_ms}"
            )

            if isinstance(data, dict) and "products" in data:
                result = {
                    "success":     True,
                    "data":        data.get("products", []),
                    "total":       str(data.get("total", "")) or None,
                    "total_pages": str(data.get("pages", "")) or None,
                }
                if data.get("or_group_breakdown"):
                    result["or_group_breakdown"] = data["or_group_breakdown"]
                return result

            return {
                "success":     True,
                "data":        data,
                "total":       resp.headers.get("X-WP-Total"),
                "total_pages": resp.headers.get("X-WP-TotalPages"),
            }

        except Exception as e:
            body_preview = ""
            if hasattr(e, "response") and e.response is not None:
                try:
                    body_preview = f" | response_body={e.response.text[:500]!r}"
                except Exception:
                    pass
            # A 2xx whose body will not parse leaves body_preview empty above,
            # so the log said only that JSON decoding failed and never what
            # actually came back. Status, content type, byte length and a short
            # preview separate an empty body from an HTML error page from a
            # truncated payload — the difference between three very different
            # server-side faults.
            if not body_preview and resp is not None:
                try:
                    body_preview = (
                        f" | status={resp.status_code}"
                        f" | content_type={resp.headers.get('Content-Type')!r}"
                        f" | length={len(resp.content)}"
                        f" | response_preview={resp.text[:300]!r}"
                    )
                except Exception:
                    pass
            _elapsed_ms = round((_time.time() - _req_start) * 1000)
            _record_upstream("failed", endpoint_short, str(e))
            api_logger.error(
                f"RESPONSE {api_call.method} {endpoint_short} | "
                f"status=ERROR | time_ms={_elapsed_ms} | "
                f"error={str(e)}{body_preview}"
            )
            logger.error(
                f"API error: {endpoint_short} | error={str(e)}{body_preview}",
                exc_info=True,
            )
            return {"success": False, "data": [], "error": str(e)}

    def execute_all(self, api_calls: List[WooAPICall]) -> List[dict]:
        results = []
        for call in api_calls:
            results.append(self.execute(call))
        return results


# Global WooClient instance
woo_client = WooClient()