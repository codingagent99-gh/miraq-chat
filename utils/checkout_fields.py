"""
utils/checkout_fields.py — Required-field resolution and address validation for
the bulk order flow.

Why this module exists
──────────────────────
The bulk order flow places orders through POST /wc/v3/orders. That endpoint
performs NO address validation — unlike the real checkout, which runs
WC_Checkout::validate_posted_data() and enforces every `required` flag. So the
bulk flow has to do the enforcement itself, or blank-address orders get created
silently.

The union rule (the important part)
───────────────────────────────────
Required fields are the UNION of a static floor (config.store_config.
BULK_ADDRESS_REQUIRED_FLOOR) and the live /checkout-fields response. The live
response may only ADD required fields; it may never remove one.

This is not defensive over-engineering. THWCFE (the checkout field editor
plugin) evaluates conditional-display rules when woocommerce_checkout_fields
runs. In a REST context there is no session and no cart, so a condition can
evaluate false and the field is stripped from the response even though the live
checkout renders it. The widget hits exactly this and works around it in
hooks/useCheckoutFields.ts:470-501 by re-injecting billing_field_type whenever
/order-types comes back non-empty. If this module trusted the live response
verbatim, a misfiring plugin conditional would silently disable the validation
gate — the precise failure this module exists to prevent.

Key shapes
──────────
All keys here are SHORT form keys (group prefix stripped): "first_name", not
"billing_first_name". That is the shape the widget's address panel posts back
inside __BULK_ADDR__, and the shape bulk_order_handler stores in its
billing_address / shipping_address blocks. _form_key() below mirrors formKey()
in hooks/useCheckoutFields.ts — keep the two in sync.

The "meta" group holds the CS custom fields (billing_field_type,
billing_project, project_rep). They are order META, not WooCommerce address
fields, so no address-level check would ever catch them. They live on the
billing block in the panel payload, so they are validated against `billing`.
"""

import re
import time
from typing import Optional

from chat_logger import get_logger
from config.store_config import BULK_ADDRESS_REQUIRED_FLOOR

logger = get_logger("miraq_chat")

# Cache TTL for the live /checkout-fields response.
_CACHE_TTL_SECONDS = 900  # 15 minutes

# ── Key normalisation (mirrors hooks/useCheckoutFields.ts) ────────────────────

# Custom fields whose names start with "billing_" but must keep the full name —
# stripping the prefix would make them ambiguous and break the __BULK_ADDR__
# consumer, which looks for these exact keys.
_BILLING_KEEP_PREFIX = frozenset({"billing_field_type", "billing_project"})

# THWCFE registers "<group>_company_name" instead of the standard
# "<group>_company". BOTH groups must be normalised to the same short key.
#
# Billing was missing here, so /checkout-fields' "billing_company_name" became
# the short key "company_name" on this side while the widget mapped it to
# "company". The rep would fill in Company Name, the payload would carry
# "company", and validation would look for "company_name", find it blank, and
# block the order with "Company Name cannot be edited here" — a field the card
# was in fact rendering, just under the other key.
_COMPANY_KEY_REMAP = {
    "billing_company_name": "company",
    "shipping_company_name": "company",
}

# Short keys that belong to the "meta" group rather than the address block they
# were registered under.
_META_KEYS = frozenset({"billing_field_type", "billing_project", "project_rep"})

# Display labels for the shopper-facing message. Anything unmapped falls back to
# a title-cased version of the key. Live labels from /checkout-fields override
# these when available (see _absorb_live_labels).
_DEFAULT_LABELS = {
    "first_name": "First name",
    "last_name": "Last name",
    "company": "Company name",
    "country": "Country",
    "address_1": "Address",
    "address_2": "Address line 2",
    "city": "City",
    "state": "State",
    "postcode": "Postcode",
    "phone": "Phone",
    "email": "Email",
    "billing_field_type": "Order Type",
    "billing_project": "Project Name",
    "project_rep": "Your Rep",
    "order_notes": "Order notes",
}

_live_labels: dict = {}

# Option VALUES (emails) registered on the project_rep select, harvested from
# the same /checkout-fields response the required-field union is built from.
#
# This is the authoritative "who is a rep" list: it is what the storefront
# checkout renders and what every existing _billing_project_rep value was
# chosen from. It is NOT the same set as /wp-json/custom-api/v1/reps, which
# returns users holding a role in WC_Chat_Security::rep_roles() — an address
# can carry a project_rep that is valid here and absent there, or vice versa.
#
# Empty means "not known yet / fetch failed", never "nobody is a rep" —
# callers must fail open rather than block ordering on a plugin outage.
_rep_option_values: set = set()

# email → display label, harvested from the SAME options dict as
# _rep_option_values above. WooCommerce serialises the select as value→label,
# so the emails are the keys and the human names are the values — and the
# values were previously read and thrown away.
#
# The names are what a user actually types ("how many did Ram order"), so
# without them rep extraction had to infer a person from sentence grammar
# alone ("did <X> order"), which is why a single missing verb form ("did Ram
# ordered") silently produced a store-wide report. Products never had this
# problem because they have a loaded vocabulary; this gives reps one too.
#
# Kept SEPARATE from _rep_option_values rather than replacing it: that set
# backs is_known_rep()'s auto-fill gate and its fail-open contract, which must
# not change. Empty here means the same thing it means there — "not fetched
# yet", never "there are no reps" — so every reader below must fall back to
# its existing behaviour rather than concluding a name is not a rep.
_rep_labels_by_email: dict = {}

# Cache is keyed by the custom-api base URL, which is this deployment's store
# identity. On a multi-tenant build where that URL is resolved per tenant rather
# than being a module constant, this key stays correct; if it ever becomes
# ambiguous, drop the cache rather than risk serving one store's field config to
# another. One extra HTTP call per bulk order is a cheap price.
_cache: dict = {"key": None, "value": None, "expires_at": 0.0}


def _form_key(wc_key: str, group: str) -> str:
    """WooCommerce field key → short form key. Mirrors formKey() in the widget."""
    if group == "billing" and wc_key in _BILLING_KEEP_PREFIX:
        return wc_key
    remapped = _COMPANY_KEY_REMAP.get(wc_key)
    if remapped:
        return remapped
    prefix = f"{group}_"
    stripped = wc_key[len(prefix):] if wc_key.startswith(prefix) else wc_key
    # Catch any other "<group>_company_name" spelling the theme may register.
    return "company" if stripped == "company_name" else stripped


def label_for(key: str) -> str:
    """Display label for a field key."""
    return _live_labels.get(key) or _DEFAULT_LABELS.get(key) or key.replace("_", " ").title()


def _blank_floor() -> dict:
    """Deep-ish copy of the configured floor, so callers can't mutate config."""
    return {
        "billing": list(BULK_ADDRESS_REQUIRED_FLOOR.get("billing", [])),
        "shipping": list(BULK_ADDRESS_REQUIRED_FLOOR.get("shipping", [])),
        "meta": list(BULK_ADDRESS_REQUIRED_FLOOR.get("meta", [])),
    }


def _absorb_live_labels(short_key: str, cfg: dict) -> None:
    label = (cfg or {}).get("label")
    if isinstance(label, str) and label.strip():
        _live_labels[short_key] = label.strip()


def _absorb_rep_options(short_key: str, cfg: dict) -> None:
    """Harvest project_rep's option values. WC serialises them value → label."""
    if short_key != "project_rep":
        return
    options = (cfg or {}).get("options")
    if not isinstance(options, dict):
        return
    values = {
        str(value).strip().lower()
        for value in options.keys()
        if str(value).strip()          # drop the blank "Select Rep" placeholder
    }
    # Same pass, same source: keep the LABELS too. A label that is blank or is
    # just the email repeated carries no name, so it is skipped rather than
    # stored — otherwise "r.ramnaresh.007@gmail.com" would enter the name
    # vocabulary as if it were something a person would type.
    labels = {}
    for value, label in options.items():
        email = str(value).strip().lower()
        name = str(label or "").strip()
        if not email or not name or name.lower() == email:
            continue
        labels[email] = name

    if values:
        _rep_option_values.clear()
        _rep_option_values.update(values)
    # Guarded independently of `values`: a fetch that returned options but no
    # usable labels must not wipe a directory a previous fetch populated.
    if labels:
        _rep_labels_by_email.clear()
        _rep_labels_by_email.update(labels)


def rep_directory() -> dict:
    """
    email → display name for every rep on the project_rep select.

    Empty means "not fetched yet / fetch failed", NEVER "there are no reps".
    Callers must treat empty as "I don't know" and fall back to whatever they
    did before, exactly as is_known_rep() fails open.
    """
    get_required_fields()          # cached; populates on first use
    return dict(_rep_labels_by_email)


def rep_name_tokens() -> set:
    """
    Lowercase word tokens from every rep display name, >= 3 chars.

    Used to protect these words from typo correction — "ram" must never be
    rewritten toward a catalog term the way "time" was rewritten to "tile".
    Protection only prevents REWRITING a token; it never stops that token from
    matching a product, so a rep surname that is also a product name (this
    store has both an "Adams" product and Adams-like surnames) is unaffected.
    """
    tokens = set()
    for name in rep_directory().values():
        for tok in re.split(r"[^a-z0-9]+", name.lower()):
            if len(tok) >= 3:
                tokens.add(tok)
    return tokens


def find_reps_in_text(text: str, catalog_words: Optional[set] = None) -> list:
    """
    Find rep display names occurring in `text`. Returns [(name, email), ...],
    longest name first so "Ram R" wins over a bare "Ram".

    This is the vocabulary-backed counterpart to the grammar-based
    `_REP_RE` extraction: it recognises a rep because the NAME is known, not
    because the sentence was phrased as "did <X> order". That makes it immune
    to verb form, word order and punctuation.

    COLLISION RULE — the reason `catalog_words` exists. A single-token match
    is accepted only when that token is NOT also a catalog term, so "order
    adams" stays a product order rather than becoming a query about a rep
    named Adams. A multi-token full-name match ("john adams") is always
    accepted: two words landing on a person's full name is not a coincidence.
    Pass the store's product/attribute vocabulary in; omitting it disables the
    guard, which is only safe when the caller has already established the
    message is not about products.
    """
    text_l = f" {re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).strip()} "
    if text_l.strip() == "":
        return []
    catalog_words = {w.lower() for w in (catalog_words or set())}

    hits = []
    for email, name in rep_directory().items():
        norm = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if not norm:
            continue
        toks = norm.split()
        if f" {norm} " in text_l:
            hits.append((len(toks), name, email))
            continue
        # Fall back to the surname/first-name alone, subject to the collision
        # rule above. Skipped entirely for 1-token names, which the exact
        # check above has already covered.
        if len(toks) > 1:
            for tok in toks:
                if len(tok) < 3 or tok in catalog_words:
                    continue
                if f" {tok} " in text_l:
                    hits.append((1, name, email))
                    break
        elif toks and toks[0] not in catalog_words:
            pass                     # already handled by the exact check

    hits.sort(key=lambda h: -h[0])
    seen, out = set(), []
    for _, name, email in hits:
        if email in seen:
            continue
        seen.add(email)
        out.append((name, email))
    return out


_ORDER_TYPE_CACHE = {"value": None, "key": None, "expires_at": 0.0}


def _fetch_order_types() -> Optional[list]:
    """GET /order-types. None on any failure — callers must fail OPEN."""
    try:
        from woo_client import woo_client
        from ecommerce import endpoints

        result = woo_client.execute(
            endpoints.fetch_order_types(
                description="Fetch order type options for bulk order parsing",
            )
        )
    except Exception as exc:
        logger.warning(f"[OrderTypes] fetch raised | error={exc}")
        return None

    data = result.get("data") if isinstance(result, dict) else None
    if not result.get("success") or not isinstance(data, list):
        logger.warning(f"[OrderTypes] fetch unsuccessful | error={result.get('error')!r}")
        return None
    return data


def order_type_options() -> list:
    """
    [{"value": "new_deal", "label": "New Deal"}, ...] for billing_field_type.

    Empty means "not fetched / fetch failed", NEVER "this field has no valid
    values" — same fail-open contract as is_known_rep().
    """
    from app_config import CUSTOM_API_BASE_URL

    now = time.time()
    if (
        _ORDER_TYPE_CACHE["value"] is not None
        and _ORDER_TYPE_CACHE["key"] == CUSTOM_API_BASE_URL
        and _ORDER_TYPE_CACHE["expires_at"] > now
    ):
        return list(_ORDER_TYPE_CACHE["value"])

    raw = _fetch_order_types()
    if raw is None:
        # Cache nothing on failure so the next turn retries, and return empty
        # so the caller falls open rather than rejecting every value.
        return []

    opts = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        value = str(row.get("value") or "").strip()
        label = str(row.get("label") or "").strip() or value
        if value:
            opts.append({"value": value, "label": label})

    _ORDER_TYPE_CACHE["value"] = list(opts)
    _ORDER_TYPE_CACHE["key"] = CUSTOM_API_BASE_URL
    _ORDER_TYPE_CACHE["expires_at"] = now + _CACHE_TTL_SECONDS
    logger.info(f"[OrderTypes] cached {len(opts)} option(s): {[o['label'] for o in opts]}")
    return list(opts)


def _norm_option(s: str) -> str:
    """Fold case, and treat _ - / as spaces.

    Needed on BOTH sides: the stored value is "new_deal" while the label is
    "New Deal", and "Presentation/Library" carries a slash. Folding all three
    separators lets a user type any of those shapes and land on one option.
    """
    return re.sub(r"[\s_\-/]+", " ", str(s or "").strip().lower()).strip()


def match_order_type(text: str) -> dict:
    """
    Resolve typed text to a billing_field_type VALUE.

    Returns {"status": ..., "value": ..., "label": ..., "candidates": [...]}
    where status is one of:
        "matched"    — exactly one option; `value` is what to store
        "ambiguous"  — several options matched; `candidates` lists their labels
        "unknown"    — no option matched; `candidates` lists every valid label
        "unvalidated"— option list unavailable; `value` is the raw text

    WHY this exists rather than storing what was typed: `_billing_field_type`
    holds the option VALUE ("new_deal"), not the label ("New Deal"). A raw
    typed string is non-empty, so it satisfies the required-field gate, but
    the widget's <select> has no matching <option> and renders BLANK — the
    exact bug already fixed once for project_rep. So free text here is not
    "usually right"; it is wrong every time.

    "unvalidated" deliberately passes the text through: if /order-types is
    unreachable we must not block ordering, matching is_known_rep()'s
    fail-open contract. It is logged so the pass-through is visible.
    """
    needle = _norm_option(text)
    if not needle:
        return {"status": "unknown", "value": "", "label": "", "candidates": []}

    opts = order_type_options()
    if not opts:
        logger.warning(
            f"[OrderTypes] options unavailable — accepting {text!r} unvalidated"
        )
        return {"status": "unvalidated", "value": str(text).strip(),
                "label": str(text).strip(), "candidates": []}

    all_labels = [o["label"] for o in opts]

    # Exact on label or value first — an exact hit must never be beaten by a
    # prefix hit on a different option.
    exact = [o for o in opts
             if needle in (_norm_option(o["label"]), _norm_option(o["value"]))]
    if len(exact) == 1:
        return {"status": "matched", "value": exact[0]["value"],
                "label": exact[0]["label"], "candidates": []}

    # Then prefix/containment, requiring EXACTLY ONE match. "existing" hits
    # only Existing Deal; "deal" hits both New Deal and Existing Deal and must
    # ask rather than guess — the same exactly-one rule used when repairing an
    # over-captured rep name.
    partial = [o for o in opts
               if _norm_option(o["label"]).startswith(needle)
               or _norm_option(o["value"]).startswith(needle)
               or needle in _norm_option(o["label"])]
    if len(partial) == 1:
        return {"status": "matched", "value": partial[0]["value"],
                "label": partial[0]["label"], "candidates": []}
    if len(partial) > 1:
        return {"status": "ambiguous", "value": "", "label": "",
                "candidates": [o["label"] for o in partial]}

    return {"status": "unknown", "value": "", "label": "", "candidates": all_labels}


def is_known_rep(email: str) -> bool:
    """
    True when `email` appears in the project_rep option list.

    Fails OPEN: when the option list could not be fetched, every non-empty
    email is accepted, because a plugin outage must not stop a rep from
    placing an order. Callers that use this to decide whether to AUTO-FILL
    project_rep therefore keep their old behaviour during an outage.
    """
    email = str(email or "").strip().lower()
    if not email:
        return False
    # Cheap on the hot path — cached for _CACHE_TTL_SECONDS. Called here so the
    # option list is populated even if this is the first lookup of the process,
    # before any validation pass has run.
    get_required_fields()
    if not _rep_option_values:
        return True
    return email in _rep_option_values


def _fetch_live_fields() -> Optional[dict]:
    """
    Fetch /checkout-fields. Returns the raw grouped dict, or None on any
    failure — a plugin outage must neither break bulk ordering nor disable the
    gate, so callers fall back to the floor.
    """
    try:
        # Imported lazily: this module is imported from handlers that are
        # themselves imported at request time, and woo_client pulls in app
        # config at import.
        from woo_client import woo_client
        from ecommerce import endpoints

        result = woo_client.execute(
            endpoints.fetch_checkout_fields(
                description="Fetch checkout fields for bulk address validation",
            )
        )
    except Exception as exc:
        logger.warning(f"[CheckoutFields] live fetch raised — using floor only | error={exc}")
        return None

    if not result.get("success") or not isinstance(result.get("data"), dict):
        logger.warning(
            "[CheckoutFields] live fetch unsuccessful — using floor only | "
            f"error={result.get('error')!r}"
        )
        return None

    return result["data"]


def get_required_fields(force_refresh: bool = False) -> dict:
    """
    Return {"billing": [...], "shipping": [...], "meta": [...]} — the union of
    the static floor and the live /checkout-fields required flags.

    Never returns fewer fields than the floor. Never raises.
    """
    from app_config import CUSTOM_API_BASE_URL

    now = time.time()
    cache_key = CUSTOM_API_BASE_URL
    if (
        not force_refresh
        and _cache["value"] is not None
        and _cache["key"] == cache_key
        and _cache["expires_at"] > now
    ):
        # Copy on the way out so a caller mutating the result can't poison the cache.
        return {k: list(v) for k, v in _cache["value"].items()}

    required = _blank_floor()
    live = _fetch_live_fields()

    if live:
        added = []
        for group in ("billing", "shipping"):
            raw_group = live.get(group)
            if not isinstance(raw_group, dict):
                continue
            for wc_key, cfg in raw_group.items():
                if not isinstance(cfg, dict):
                    continue
                short = _form_key(wc_key, group)
                _absorb_live_labels(short, cfg)
                _absorb_rep_options(short, cfg)
                if not cfg.get("required"):
                    continue
                # Route the CS custom fields to "meta" regardless of the address
                # group they were registered under.
                target = "meta" if short in _META_KEYS else group
                if short not in required[target]:
                    required[target].append(short)
                    added.append(f"{target}.{short}")
        # The "order" group carries order_comments / order notes, which are never
        # required for a bulk line. Deliberately ignored.
        if added:
            logger.info(
                f"[CheckoutFields] live response added {len(added)} required field(s) "
                f"beyond the floor | {', '.join(added)}"
            )
    else:
        logger.info("[CheckoutFields] falling back to static required-field floor")

    _cache["key"] = cache_key
    _cache["value"] = {k: list(v) for k, v in required.items()}
    _cache["expires_at"] = now + _CACHE_TTL_SECONDS

    return required


def validate_bulk_address(billing: dict, shipping: dict, required: dict) -> dict:
    """
    Check a single bulk line's address blocks against the required-field set.

    Returns:
        {"billing": {key: label, ...}, "shipping": {...}, "meta": {...}}

    All three groups empty means the line is valid. The "meta" group is checked
    against `billing`, because the CS custom fields live on the billing block in
    the panel payload (see _BILLING_FIELDS in handlers/bulk_order_handler.py).
    """
    billing = billing or {}
    shipping = shipping or {}
    required = required or {}

    def _missing(block: dict, keys) -> dict:
        out = {}
        for key in keys or []:
            value = block.get(key)
            if not str(value if value is not None else "").strip():
                out[key] = label_for(key)
        return out

    return {
        "billing": _missing(billing, required.get("billing")),
        "shipping": _missing(shipping, required.get("shipping")),
        "meta": _missing(billing, required.get("meta")),
    }


def has_errors(errors: dict) -> bool:
    """True when validate_bulk_address() found anything missing."""
    return any((errors or {}).get(group) for group in ("billing", "shipping", "meta"))


def format_missing_fields(errors: dict) -> str:
    """
    Human-readable one-liner for the bot message, e.g.
        "Billing: City, Postcode · Shipping: Address · Project Name"
    Meta fields are listed bare — "Order Type" reads better than "Meta: Order Type".
    """
    errors = errors or {}
    parts = []
    for group, prefix in (("billing", "Billing"), ("shipping", "Shipping")):
        labels = list((errors.get(group) or {}).values())
        if labels:
            parts.append(f"{prefix}: {', '.join(labels)}")
    meta_labels = list((errors.get("meta") or {}).values())
    if meta_labels:
        parts.append(", ".join(meta_labels))
    return " · ".join(parts)


def count_missing(errors: dict) -> int:
    """Total number of missing fields across all groups."""
    errors = errors or {}
    return sum(len(errors.get(group) or {}) for group in ("billing", "shipping", "meta"))