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
from store_registry import get_store_loader
from http_profiles import send as http_send, is_waf_block
from chat_logger import get_logger, get_api_logger, get_order_logger, sanitize_url

logger = get_logger("miraq_chat")
api_logger = get_api_logger()
order_logger = get_order_logger()

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
# ══════════════════════════════════════════════════════════════════════
_UPSTREAM_LOCK = _threading.Lock()
_UNHEALTHY_AFTER = 3        # consecutive hard failures before "down"

# Per-tenant: one state dict per tenant_id. A process-wide tracker let one
# tenant's broken WooCommerce install turn /health "down" (503, blocking) for
# every other tenant sharing this process. /health now reports the upstream
# state of whichever tenant the probe identified itself as.
# Upper bound on concurrent outbound WooCommerce calls per request. Four is
# comfortably under requests' default HTTPAdapter pool_maxsize of 10, which
# the WooClient singleton shares across every gunicorn thread -- going wider
# would start discarding connections and paying a fresh TCP+TLS handshake on
# calls that could have reused one.
_MAX_PARALLEL_WOO_CALLS = 4
_SALVAGE_WINDOW_S = 300     # a salvage older than this stops mattering

def _new_upstream_state() -> dict:
    return {
        "consecutive_failures": 0,
        "last_success_ts": None,
        "last_failure_ts": None,
        "last_failure_endpoint": "",
        "last_failure_error": "",
        "last_salvage_ts": None,
        "last_salvage_endpoint": "",
        "salvage_count": 0,
    }


_UPSTREAM_BY_TENANT: dict = {}


def _record_upstream(outcome, endpoint="", error="", tenant_key=""):
    """Record one API outcome for one tenant: 'ok', 'salvaged' or 'failed'."""
    _now = _health_time.time()
    with _UPSTREAM_LOCK:
        _st = _UPSTREAM_BY_TENANT.setdefault(str(tenant_key or ""), _new_upstream_state())
        if outcome == "failed":
            _st["consecutive_failures"] += 1
            _st["last_failure_ts"] = _now
            _st["last_failure_endpoint"] = str(endpoint)
            _st["last_failure_error"] = str(error)[:200]
            return
        # Any usable response clears the failure streak.
        _st["consecutive_failures"] = 0
        _st["last_success_ts"] = _now
        if outcome == "salvaged":
            _st["salvage_count"] += 1
            _st["last_salvage_ts"] = _now
            _st["last_salvage_endpoint"] = str(endpoint)


def upstream_health(tenant_key=None):
    """Snapshot of ONE tenant's upstream for /health. 'down' | 'degraded' | 'ok' | 'unknown'.
    unknown  — no tenant identified, or no call recorded for it yet
    
    down     — _UNHEALTHY_AFTER consecutive hard failures, nothing usable since
    degraded — usable, but a body needed salvaging recently (a real server-side
               fault that has not yet cost the user anything)
    """
    _now = _health_time.time()
    with _UPSTREAM_LOCK:
        _state = _UPSTREAM_BY_TENANT.get(str(tenant_key or "")) if tenant_key else None
        _snap = dict(_state) if _state is not None else None
    
    if _snap is None:
        return {
            "status": "unknown",
            "reasons": [],
            "consecutive_failures": 0,
            "salvage_count": 0,
            "seconds_since_success": None,
        }

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

# Browser/bot headers are NOT a constant here any more. This used to send a
# hardcoded minimal set ("Mozilla/5.0" + Accept) — which WordPress.com Atomic
# accepted and cPanel ModSecurity rejects with 406 — while the catalog
# fetcher sent a different, full set. Both now come from the tenant's own
# header profile (loader.http, see http_profiles.py), which is chosen at
# provisioning and adapts at runtime when a firewall blocks it.

def _build_auth(loader, is_custom_api: bool):
    """Auth strategy, resolved per-call from the tenant's own credentials:
      - custom-api/v1/*  → X-Consumer-Key / X-Consumer-Secret headers
      - wc/v3/*          → HTTPBasicAuth (no credentials in query string)
    Returns (auth, auth_headers); the profile headers are layered underneath
    by http_profiles.send().
    """
    if is_custom_api:
        return None, {
            "X-Consumer-Key":    loader.consumer_key,
            "X-Consumer-Secret": loader.consumer_secret,
        }
    return HTTPBasicAuth(loader.consumer_key, loader.consumer_secret), {}


class WooClient:
    """Executes WooCommerce API calls."""

    def __init__(self):
        # Shared by every tenant, so it must carry no headers of its own —
        # session-level headers are merged into every request and would leak
        # one tenant's profile into another's.
        self.session = http_requests.Session()

    def execute(self, api_call: WooAPICall, loader=None) -> dict:
        """Execute a single API call and return raw response.

        loader: the tenant StoreLoader to resolve credentials/URLs from.
        Defaults to get_store_loader() (the current request's bound tenant)
        for the ~25 call sites across handlers/parsers/routes that call this
        directly inside a request. execute_all() resolves it once itself and
        passes it explicitly instead, because its ThreadPoolExecutor workers
        have no request context of their own — get_store_loader() would
        return None there, silently, which is a wrong-tenant-data risk, not
        an exception.
        """
        import json as _json
        import time as _time

        if loader is None:
            loader = get_store_loader()
        if not loader:
            raise RuntimeError(
                "woo_client.execute(): no tenant loader resolved — "
                "is X-MiraQ-License-Id missing from the request?"
            )

        # ── Shopify backstop ──────────────────────────────────────────────────
        # On a Shopify tenant no WooCommerce request is ever legitimate.
        # ShopifyEndpoints returns surface="shopify_admin" stubs whose endpoint
        # paths are placeholders; executing them would resolve against this
        # tenant's woo_base_url and hit an unrelated store.
        #
        # This guard lives here (not only in chat.py's dispatcher) because
        # ~25 call sites across handlers, parsers and routes call woo_client
        # directly, bypassing that dispatcher. execute_all() delegates here
        # too, so this is the one place that provably covers all of them.
        #
        # Returning the standard failure envelope — rather than raising —
        # means every existing caller's `if result.get("success")` branch
        # degrades safely with no other change.
        if loader.ecommerce_backend == "shopify":
            logger.warning(
                "WooClient: blocked WooCommerce call on Shopify tenant | "
                f"tenant={loader.license_id!r} | "
                f"{api_call.method} {api_call.endpoint} | "
                f"surface={getattr(api_call, 'surface', '')} | "
                f"description={api_call.description!r}"
            )
            return {
                "success": False,
                "data": [],
                "error": "unsupported_on_shopify",
                "unsupported_on_shopify": True,
            }

        params = dict(api_call.params)
        is_custom_api = api_call.surface == "custom_plugin"

        # Order creation (Row 5.4 — POST /orders, admin surface) gets its own
        # logger per request, isolated from api.txt entirely. list_rep_orders /
        # list_cs_orders also POST to "/orders" but on the custom_plugin
        # surface, so the surface check keeps them out of this branch.
        is_order_create = (
            api_call.method == "POST"
            and api_call.endpoint == "/orders"
            and api_call.surface == "admin"
        )
        _api_log = order_logger if is_order_create else api_logger

        # Resolve relative endpoints to full URLs — from this tenant's own
        # loader, not a process-wide global.
        endpoint = api_call.endpoint
        if not endpoint.startswith("http"):
            base = loader.custom_api_base if is_custom_api else loader.base
            endpoint = base.rstrip("/") + endpoint

        auth, headers = _build_auth(loader, is_custom_api)

        # ── Logging ───────────────────────────────────────────────────────────
        sanitized_endpoint = sanitize_url(endpoint)
        endpoint_short     = sanitized_endpoint.split("/")[-1]
        safe_params        = {k: v for k, v in params.items()
                              if k not in ("consumer_key", "consumer_secret")}

        context = ""
        if api_call.session_id or api_call.user_message:
            context = f" | session={api_call.session_id} | q={api_call.user_message!r}"

        if api_call.method == "GET":
            _api_log.info(f"REQUEST GET {sanitized_endpoint} | params={safe_params}{context}")
        else:
            try:
                body_str = _json.dumps(api_call.body, separators=(",", ":"))
            except Exception:
                body_str = str(api_call.body)
            _api_log.info(f"REQUEST {api_call.method} {sanitized_endpoint} | body={body_str}{context}")

        # The body matters as much as the params on POST endpoints — a
        # resolved-but-wrong customer_id is invisible when only params are
        # logged, which is exactly the blind spot that made an empty CS-rep
        # order list impossible to diagnose from logs alone. It is logged in
        # full by _api_log above; repeating it here put a second copy of every
        # filter tree on stdout, where PM2 captures it. One copy is enough.
        logger.info(f"API call: {api_call.method} {endpoint_short} | params={safe_params}")

        _req_start = _time.time()

        # Bound before the try so the failure handler below can still describe
        # the response when the request SUCCEEDED and only parsing blew up —
        # requests' JSONDecodeError carries no .response, which is why a 2xx
        # with an empty or HTML body used to log nothing but "Expecting value:
        # line 1 column 1 (char 0)".
        resp = None

        try:
            # Timed into the "woo_api" bucket so the timing log can separate
            # time waiting on WooCommerce from time spent in our own code.
            import timing_logger
            with timing_logger.stage("woo_api"):
                # http_send retries with the tenant's other header profiles
                # ONLY on a recognisable firewall block (nothing reached
                # WordPress), never on timeouts or WordPress errors — so this
                # is safe for order creation too.
                if api_call.method == "GET":
                    resp = http_send(
                        self.session, "GET", endpoint,
                        state=loader.http,
                        auth=auth,
                        headers=headers,
                        params=params,
                        timeout=45,
                    )
                else:
                    resp = http_send(
                        self.session, api_call.method, endpoint,
                        state=loader.http,
                        auth=auth,
                        headers=headers,
                        params=params,
                        json=api_call.body,
                        timeout=45,
                    )

            resp.raise_for_status()
            _elapsed_ms = round((_time.time() - _req_start) * 1000)
            data, _salvaged = _parse_json_tolerant(resp, endpoint_short)
            _record_upstream("salvaged" if _salvaged else "ok", endpoint_short, tenant_key=loader.tenant_id)
            # ── Response logging ──────────────────────────────────────────────
            # `count` is the number of ITEMS in a list-shaped response. Aggregate
            # endpoints return a dict of totals with no item list, which used to
            # log a bare count=0 — indistinguishable from "no results" while the
            # body actually held real numbers. Summarise those instead.
            _summary_extra = ""
            if isinstance(data, dict) and "products" in data:
                items = data.get("products", [])
            elif isinstance(data, list):
                items = data
            elif isinstance(data, dict) and isinstance(data.get("data"), list):
                # custom_plugin convention: {"success": bool, "count": int, "data": [...]}
                # (e.g. /company-order-addresses, /saved-addresses). Without this,
                # every such endpoint logs count=0 regardless of how many rows it
                # actually returned — indistinguishable from a genuinely empty result.
                items = data["data"]
            else:
                items = []
                if isinstance(data, dict):
                    _keys = ("total_orders", "total_items", "unattributed_orders", "truncated")
                    _present = {k: data.get(k) for k in _keys if k in data}
                    if _present:
                        _summary_extra = " | " + " ".join(f"{k}={v}" for k, v in _present.items())
                        if data.get("reps") is not None:
                            _summary_extra += f" reps={len(data.get('reps') or [])}"
                        # Row count for list mode. Without this, a response
                        # carrying order rows and one carrying only totals log
                        # identically — which is exactly the difference between
                        # a deployed and an undeployed plugin when the caller
                        # asked for list=1. Logged as absent vs 0 on purpose:
                        # "key missing" means the plugin never built rows,
                        # "0 rows" means it did and found none.
                        _summary_extra += (
                            f" orders={len(data.get('orders') or [])}"
                            if "orders" in data else " orders=<absent>"
                        )
                    else:
                        _summary_extra = f" | keys={sorted(data.keys())[:8]}"

            if items:
                product_summary = ", ".join(
                    f"{p.get('id')}:{p.get('name', '?')}" for p in items[:20]
                )
                _api_log.info(
                    f"RESPONSE {api_call.method} {endpoint_short} | "
                    f"status={resp.status_code} | count={len(items)} | "
                    f"time_ms={_elapsed_ms} | products=[{product_summary}]"
                )
            else:
                _api_log.info(
                    f"RESPONSE {api_call.method} {endpoint_short} | "
                    f"status={resp.status_code} | count={len(items)} | "
                    f"time_ms={_elapsed_ms}{_summary_extra}"
                )

            logger.info(
                f"API response: {endpoint_short} | status={resp.status_code} | "
                f"count={len(items)} | time_ms={_elapsed_ms}{_summary_extra}"
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
            _record_upstream("failed", endpoint_short, str(e), tenant_key=loader.tenant_id)
            _api_log.error(
                f"RESPONSE {api_call.method} {endpoint_short} | "
                f"status=ERROR | time_ms={_elapsed_ms} | "
                f"error={str(e)}{body_preview}"
            )
            logger.error(
                f"API error: {endpoint_short} | error={str(e)}{body_preview}",
                exc_info=True,
            )
            # Preserve the API's own error code/message. WordPress returns
            # {"code": "...", "message": "...", "data": {"status": 4xx}} — a
            # precise reason ("no rep by that name") that callers can act on.
            # Collapsing it to a bare string forced every caller into a
            # generic "something went wrong", hiding the real cause.
            _err = {"success": False, "data": [], "error": str(e)}
            # Lets callers tell "the store's firewall refused us" apart from
            # a genuinely empty result instead of reporting "Zero results".
            if is_waf_block(resp):
                _err["waf_blocked"] = True
                _err["http_profile"] = loader.http.name
            if hasattr(e, "response") and e.response is not None:
                _err["status_code"] = e.response.status_code
                try:
                    _body = e.response.json()
                    if isinstance(_body, dict) and _body.get("code"):
                        _err["error_code"] = _body.get("code")
                        _err["error_message"] = _body.get("message")
                except Exception:
                    pass
            return _err

    def execute_all(self, api_calls: List[WooAPICall]) -> List[dict]:
        """Run every call, in parallel when there is more than one.

        Each WooCommerce call costs 2.5-3s of pure waiting (measured Sep 2026;
        flat whether the server is idle or saturated, so it is upstream
        latency, not contention). Run serially, a three-call request waits
        ~8s for work that could finish in ~3s. The wait releases the GIL, so
        threads genuinely overlap here.

        Results stay in the order of api_calls -- callers downstream pair
        each response with its .call by position.

        TENANT RESOLUTION: get_store_loader() reads Flask's g, which is
        request-context-local — ThreadPoolExecutor workers below have no
        request context of their own, so a call to it from inside a worker
        returns None silently. That's not an exception; every call in the
        batch would look like a normal failure, or worse, would each fail
        the "if not loader: raise" check independently in a way that's easy
        to shrug off as flaky. The actual risk if this were missed is worse
        than either: a WRONG loader (a different tenant's, resolved by
        chance from process state) rather than no loader — wrong-tenant
        data, not an error at all. So the loader is resolved exactly once,
        here, in this calling thread (which does have request context), and
        passed explicitly into every worker's execute() call. Every call in
        one batch is provably the same tenant's, and no worker thread ever
        calls get_store_loader() itself.

        TIMING: execute() records into the "woo_api" bucket, but
        timing_logger.accumulate() no-ops outside a request context, and
        worker threads have none. So the batch is wrapped here instead, which
        also gives the more useful number: wall-clock waiting on WooCommerce,
        not the sum of overlapping calls. The single-call path is left inline
        so execute() keeps recording it -- wrapping there too would
        double-count.
        """
        if not api_calls:
            return []

        loader = get_store_loader()
        if not loader:
            raise RuntimeError(
                "woo_client.execute_all(): no tenant loader resolved — "
                "is X-MiraQ-License-Id missing from the request?"
            )

        if len(api_calls) == 1:
            return [self.execute(api_calls[0], loader=loader)]

        import timing_logger
        from concurrent.futures import ThreadPoolExecutor

        # Bounded: with N gunicorn threads each fanning out, an unbounded
        # pool would multiply into far more sockets than WooCommerce (or the
        # session's connection pool) wants to see at once.
        max_workers = min(len(api_calls), _MAX_PARALLEL_WOO_CALLS)

        def _run(call):
            # execute() already converts failures into an error dict; this is
            # a backstop so one unexpected raise cannot lose the whole batch.
            # loader is captured from the enclosing scope — resolved once,
            # above, in the calling thread — never re-resolved in the worker.
            try:
                return self.execute(call, loader=loader)
            except Exception as exc:
                logger.error(f"execute_all: call failed | error={exc}", exc_info=True)
                return {"success": False, "error": str(exc), "call": call}

        with timing_logger.stage("woo_api"):
            with ThreadPoolExecutor(max_workers=max_workers,
                                    thread_name_prefix="woo") as pool:
                return list(pool.map(_run, api_calls))


# Global WooClient instance
woo_client = WooClient()