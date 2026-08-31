"""
Single source of truth for which e-commerce backend this deployment runs.

WHY THIS MODULE EXISTS
----------------------
ECOMMERCE_BACKEND used to be read independently in THREE places
(app_config.py, store_loader/config.py, ecommerce/__init__.py), each with its
own silent `"woocommerce"` fallback. Every Shopify safety net in the codebase
keys off that one value:

  * woo_client.execute()             blocks all WooCommerce HTTP calls
  * bulk_order_parser.py (Step 0)    forces self-scoped ordering, no rep/company
  * routes/chat.py                   bounces the rep multi-recipient flow

so an unset or misspelt env var stood all three of them down at once and the
request proceeded against the wrong store, with nothing in the log but a 404
warning from a customer id the other platform had never heard of.

This is the same split-brain the frontend already hit with VITE_PLATFORM, and
the same remedy: one module owns the decision, everything else imports it.

Import ECOMMERCE_BACKEND from here. Do not call os.getenv for it anywhere else.

DEPLOYMENT NOTE — BREAKING: the value must now be set EXPLICITLY in every
environment, including local. There is deliberately no default; an unset or
unrecognised value raises at import time rather than resolving to WooCommerce
behind your back.
"""

import os
from dotenv import load_dotenv

load_dotenv()

VALID_ECOMMERCE_BACKENDS = frozenset({"woocommerce", "shopify"})

_raw_backend = os.getenv("ECOMMERCE_BACKEND")

if _raw_backend is None or not _raw_backend.strip():
    raise RuntimeError(
        "ECOMMERCE_BACKEND is not set. It must be one of: "
        + ", ".join(sorted(VALID_ECOMMERCE_BACKENDS))
        + ". There is deliberately no default -- defaulting to WooCommerce let "
        "a Shopify deployment silently serve a WooCommerce catalogue with every "
        "platform guard disabled. Set it in .env for every environment."
    )

ECOMMERCE_BACKEND = _raw_backend.strip().lower()

if ECOMMERCE_BACKEND not in VALID_ECOMMERCE_BACKENDS:
    raise RuntimeError(
        f"ECOMMERCE_BACKEND={_raw_backend!r} is not a recognised backend. "
        "Valid values: " + ", ".join(sorted(VALID_ECOMMERCE_BACKENDS)) + "."
    )

# Convenience aliases, so call sites read as intent rather than string compare.
IS_SHOPIFY     = ECOMMERCE_BACKEND == "shopify"
IS_WOOCOMMERCE = ECOMMERCE_BACKEND == "woocommerce"