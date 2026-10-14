"""
miraq_trace.py — human-readable, step-by-step trace of a channel turn
(Instagram / WhatsApp / Messenger), for demos and Meta App Review recordings.

One block per incoming DM, written to its own file:

    logs/YYYY-MM-DD/trace.txt

    [10:41:07.214] ━━━ instagram DM · conv 3f9c1a2b ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    [10:41:07.215] ① RECEIVED     from instagram user ••••4821 → store tiles.example.com (guest)
    [10:41:07.215]                "show me exterior product with matte finish and 8X12 sample size"
    [10:41:07.498] ② UNDERSTOOD   intent: product search · confidence 0.95 · MiraQ NLP · 283 ms
    [10:41:07.498]                Category     : Exterior
    [10:41:07.498]                Finish       : Matte
    [10:41:07.498]                Sample Size  : 8x12
    [10:41:08.102] ③ CATALOGUE    POST /products-advanced-new (store catalogue) · 604 ms
    [10:41:08.102]                query  {"filters": ...}
    [10:41:08.102]                6 products matched: Zyvora Havana Dune, Bravique Ash Stone, ...
    [10:41:08.311] ④ REPLIED      4 Instagram messages (1 text, 3 product cards) · total 1097 ms
    [10:41:08.311]                "Here are 6 exterior products with a matte finish..."

Privacy: the channel user id is masked to its last 4 characters, the
customer id / email / tokens are never written, and API keys are stripped
from query params.

Off by default. Turn on with:

    MIRAQ_TRACE_ENABLED=true

Only channel turns are traced (the /chat dispatch that routes/channel.py runs
in-process); widget traffic is never written here. Every function swallows
its own errors — tracing can never break a turn.
"""

import json
import logging
import os
import re

from flask import has_request_context, request

from chat_logger import (
    _DailyDirectoryHandler,
    _LOG_BASE_DIR,
    _MillisecondFormatter,
    _handler_setup_lock,
)
from identity import CHANNEL_ENVIRON_KEY

ENABLED = os.getenv("MIRAQ_TRACE_ENABLED", "false").strip().lower() in ("true", "1", "yes")

_PAD = " " * 15                     # aligns continuation lines under the step text
_SECRET_PARAM = re.compile(r"(key|secret|token|password|signature)", re.IGNORECASE)


def _logger() -> logging.Logger:
    log = logging.getLogger("miraq_trace")
    if log.handlers:
        return log
    with _handler_setup_lock:
        if log.handlers:
            return log
        log.setLevel(logging.INFO)
        log.propagate = False
        fmt = _MillisecondFormatter(fmt="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
        try:
            handler = _DailyDirectoryHandler(_LOG_BASE_DIR, "trace.txt")
            handler.setFormatter(fmt)
            log.addHandler(handler)
        except OSError as exc:
            print(f"[miraq_trace] WARNING: could not create trace log file: {exc}")
    return log


def _write(lines) -> None:
    log = _logger()
    for line in lines:
        log.info(line)


def _is_channel_turn() -> bool:
    return has_request_context() and bool(request.environ.get(CHANNEL_ENVIRON_KEY))


def _conv(session_id) -> str:
    return str(session_id or "")[:8]


def _channel_name(channel) -> str:
    c = str(channel or "")
    return {"instagram": "Instagram", "whatsapp": "WhatsApp", "messenger": "Messenger"}.get(c, c)


def _mask(user_id: str) -> str:
    s = str(user_id or "")
    return f"••••{s[-4:]}" if len(s) > 4 else "••••"


def _clip(text, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _pretty(key) -> str:
    s = str(key or "")
    s = s[3:] if s.startswith("pa_") else s
    return re.sub(r"[-_]+", " ", s).strip().title()


def _intent_label(intent) -> str:
    value = getattr(intent, "value", intent) or ""
    return str(value).replace("_", " ")


def _values(v) -> str:
    if isinstance(v, (list, tuple, set)):
        return ", ".join(str(x) for x in v)
    return str(v)


def _filter_rows(entities) -> list:
    """The filters MiraQ pulled out of the message, as (label, value) rows."""
    rows = []
    cats = list(getattr(entities, "target_category_slugs", None) or [])
    if cats:
        rows.append(("Category", ", ".join(_pretty(c) for c in cats)))
    elif getattr(entities, "category_name", None):
        rows.append(("Category", entities.category_name))
    for key, val in (getattr(entities, "attributes", None) or {}).items():
        rows.append((_pretty(key), _values(val)))
    for pair in getattr(entities, "attr_tag_or_pairs", None) or []:
        get = pair.get if isinstance(pair, dict) else (lambda k, p=pair: getattr(p, k, None))
        options = []
        if get("attr_key") and get("attr_term"):
            options.append(f"{_pretty(get('attr_key'))} = {get('attr_term')}")
        if get("tag_slug"):
            options.append(f"tag {get('tag_slug')}")
        for c in get("cat_slugs") or []:
            options.append(f"category {_pretty(c)}")
        if options:
            rows.append(("Any of", " OR ".join(options)))
    tags = getattr(entities, "tag_slugs", None)
    if tags:
        rows.append(("Tag", _values(tags)))
    for label, attr in (("Product", "product_name"), ("Products", "product_names"),
                        ("Search text", "search_term"), ("Quantity", "quantity"),
                        ("Order #", "order_id")):
        val = getattr(entities, attr, None)
        if val:
            rows.append((label, _values(val)))
    if getattr(entities, "on_sale", None):
        rows.append(("On sale", "yes"))
    in_stock = getattr(entities, "in_stock", None)
    if in_stock is not None:
        rows.append(("In stock", "yes" if in_stock else "no"))
    return rows


def _describe_call(call) -> str:
    surface = getattr(call, "surface", "") or ""
    where = {
        "custom_plugin":   "store catalogue",
        "admin":           "store catalogue",
        "shopify_graphql": "Shopify catalogue",
        "shopify_orders":  "Shopify orders",
        "loader_memory":   "cached catalogue",
    }.get(surface, surface or "store")
    return f"{getattr(call, 'method', '')} {getattr(call, 'endpoint', '')} ({where})"


def _call_query(call) -> str:
    params = {k: v for k, v in (getattr(call, "params", None) or {}).items()
              if not _SECRET_PARAM.search(str(k))}
    body = getattr(call, "body", None)
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if not _SECRET_PARAM.search(str(k))}
    shown = {}
    if params:
        shown["params"] = params
    if body:
        shown["body"] = body
    if not shown:
        return ""
    try:
        return _clip(json.dumps(shown, default=str, separators=(",", ":")), 400)
    except Exception:
        return ""


# ── Steps ────────────────────────────────────────────────────────────────────

def received(channel, user_id, session_id, store, message, signed_in) -> None:
    """① routes/channel.py: a DM arrived and was matched to a store."""
    if not ENABLED:
        return
    try:
        channel = _channel_name(channel)
        head = f"━━━ {channel} DM · conv {_conv(session_id)} "
        _write([
            "",
            head + "━" * max(10, 70 - len(head)),
            f"① RECEIVED     from {channel} user {_mask(user_id)} → store {store or '?'} "
            f"({'signed-in customer' if signed_in else 'guest'})",
            f'{_PAD}"{_clip(message)}"',
        ])
    except Exception:
        pass


def understood(session_id, intent, confidence, entities, used_llm, ms) -> None:
    """② routes/chat.py: final intent + filters, just before the catalogue query."""
    if not ENABLED or not _is_channel_turn():
        return
    try:
        engine = "MiraQ NLP + LLM assist" if used_llm else "MiraQ NLP"
        lines = [
            f"② UNDERSTOOD   intent: {_intent_label(intent)} · confidence {float(confidence or 0):.2f}"
            f" · {engine} · {int(ms)} ms"
        ]
        rows = _filter_rows(entities)
        width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            lines.append(f"{_PAD}{label.ljust(width)} : {_clip(value, 120)}")
        if not rows:
            lines.append(f"{_PAD}(no filters — general request)")
        _write(lines)
    except Exception:
        pass


def searched(session_id, calls, responses, products, ms) -> None:
    """③ routes/chat.py: the store query that ran and what came back."""
    if not ENABLED or not _is_channel_turn():
        return
    try:
        calls = list(calls or [])
        if not calls:
            return
        lines = [f"③ CATALOGUE    {_describe_call(calls[0])} · {int(ms)} ms"]
        if len(calls) > 1:
            lines.append(f"{_PAD}+ {len(calls) - 1} more call(s)")
        query = _call_query(calls[0])
        if query:
            lines.append(f"{_PAD}query  {query}")
        failed = [r for r in (responses or []) if isinstance(r, dict) and not r.get("success", True)]
        products = list(products or [])
        names = [str(p.get("name")) for p in products[:3] if isinstance(p, dict) and p.get("name")]
        more = ", …" if len(products) > 3 else ""
        if products:
            lines.append(f"{_PAD}{len(products)} product(s) matched: {_clip(', '.join(names), 140)}{more}")
        else:
            lines.append(f"{_PAD}0 products matched")
        if failed:
            lines.append(f"{_PAD}{len(failed)} call(s) failed")
        _write(lines)
    except Exception:
        pass


def replied(channel, session_id, status, intent, messages, turn, bot_message, ms) -> None:
    """④ routes/channel.py: what went back to the customer."""
    if not ENABLED:
        return
    try:
        cards = len(getattr(turn, "products", None) or [])
        options = len(getattr(turn, "options", None) or [])
        parts = [f"{cards} product card(s)"] if cards else []
        if options:
            parts.append(f"{options} quick repl{'y' if options == 1 else 'ies'}")
        detail = f" ({', '.join(parts)})" if parts else ""
        n = len(messages or [])
        channel = _channel_name(channel)
        text = re.sub(r"[*_`]+", "", str(bot_message or ""))
        _write([
            f"④ REPLIED      {n} {channel} message{'s' if n != 1 else ''}{detail} · "
            f"intent: {_intent_label(intent)} · status {status} · total {int(ms)} ms",
            f'{_PAD}"{_clip(text)}"',
        ])
    except Exception:
        pass
