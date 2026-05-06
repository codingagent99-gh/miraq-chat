# WGC Tiles Store — Intent Classifier

An intent classifier for the [WGC Tiles Store](https://wgc.net.in/hn/) (WordPress/WooCommerce)
that maps natural language queries to WooCommerce REST API calls.

## Quick Start

```bash
pip install -r requirements.txt
python main.py
```

## Evaluate Accuracy

```bash
python -m training.evaluate
```

## Architecture

```
User Utterance → Classifier → Entity Extraction → API Builder → WooCommerce API
```

## Supported Intents

- **Product Discovery**: search, catalog, types, by visual/origin/collection
- **Attribute Filters**: finish, size, color, thickness, edge, application
- **Product Subtypes**: mosaics, trim, chip cards, variations
- **Discounts**: on-sale, clearance, bulk, coupons
- **Account**: wishlist, order tracking, order placement

## Store API Base

```
https://wgc.net.in/hn/wp-json/wc/v3/
```

---

## E-commerce Backend Abstraction

The `ecommerce/` package centralises **all WooCommerce URL strings** in a single
file (`ecommerce/woo_endpoints.py`), making a future backend swap (e.g. to
Shopify) a mechanical, one-file change.

### How it works

Every API call is constructed through a singleton `endpoints` object:

```python
from ecommerce import endpoints
from woo_client import woo_client

call   = endpoints.fetch_product(product_id=123)
result = woo_client.execute(call)
```

The `endpoints` object is selected at import time by the `ECOMMERCE_BACKEND`
environment variable (default: `"woocommerce"`).

### ECOMMERCE_BACKEND env var

| Value | Behaviour |
|---|---|
| `woocommerce` *(default)* | Uses `ecommerce/woo_endpoints.py` — calls WooCommerce REST & custom-plugin APIs |
| anything else | Raises `ValueError` at startup |

### Adding a Shopify backend

1. Create `ecommerce/shopify_endpoints.py` with a `ShopifyEndpoints` class that
   implements every method declared in `ecommerce/endpoints.py`
   (`EcommerceEndpoints` Protocol).
2. In `ecommerce/__init__.py`, add:
   ```python
   elif backend == "shopify":
       from ecommerce.shopify_endpoints import ShopifyEndpoints
       return ShopifyEndpoints()
   ```
3. Set `ECOMMERCE_BACKEND=shopify` in your environment.

No other files need to change.

### Two API surfaces (WooCommerce)

| `surface` value | Base URL prepended | Auth method |
|---|---|---|
| `"admin"` *(default)* | `WOO_BASE_URL` (`/wp-json/wc/v3`) | `?consumer_key=&consumer_secret=` query params |
| `"custom_plugin"` | `CUSTOM_API_BASE_URL` (`/wp-json/custom-api/v1`) | `X-Consumer-Key` / `X-Consumer-Secret` headers |

`woo_client.execute()` resolves the full URL and attaches the correct auth
for each surface automatically, keyed off `WooAPICall.surface`.  Callers
may pass relative paths (e.g. `/orders/99`) **or** already-absolute URLs
(backward compat — the client uses them as-is).

### Available endpoint functions

See `ecommerce/endpoints.py` for the full `EcommerceEndpoints` Protocol, and
`docs/api_mapping/` for the WooCommerce → Shopify mapping CSVs that serve as
the source of truth for endpoint equivalence.

Summary of exposed functions:

| Function | CSV row | Surface |
|---|---|---|
| `fetch_currency()` | 2.1 | admin |
| `list_attributes()` | 2.2 | custom_plugin |
| `list_categories(page, per_page)` | 2.3 | admin |
| `list_tags(page, per_page)` | 2.4 | admin |
| `list_published_products(page, per_page)` | 2.5 | admin |
| `fetch_product(product_id)` | 4.1 | admin |
| `fetch_variant(product_id, variant_id)` | 4.2 | admin |
| `list_variants(product_id, page, per_page)` | 4.3 | admin |
| `products_advanced(body)` | 4.4 | custom_plugin |
| `list_customer_orders(customer_id, page, per_page, **filters)` | 5.1 | admin |
| `fetch_order(order_id)` | 5.2 | admin |
| `check_stock(product_ids)` | 5.3 | custom_plugin |
| `create_order(payload)` | 5.4 | admin |
| `historical_product_search(body)` | 5.5 | custom_plugin |
| `fetch_customer(customer_id)` | 6.1/6.2 | admin |
| `update_customer(customer_id, payload)` | 6.3 | admin |
| `list_coupons(page, per_page)` | — | admin |
| `fetch_wishlist(customer_id)` | — | admin |
| `list_products_on_sale(page, per_page)` | — | admin |
| `list_attribute_terms(attribute_id)` | — | admin |
| `search_products(search_term, page, per_page)` | — | admin |
| `list_cs_orders(body)` | — | custom_plugin |

### Future migration notes

The following concerns are **out of scope** for this PR and will be addressed
in follow-up PRs:

- **Phase 3 — Response shape normalisation**: Currently, callers still parse
  WooCommerce-native response fields (e.g. `stock_status`, `sale_price`).
  A future PR will normalise return shapes so callers are backend-agnostic.

- **Phase 4 — Pagination abstraction**: WooCommerce uses `?page=N` + the
  `X-WP-TotalPages` response header.  Shopify uses cursor-based pagination via
  the `Link: <url>; rel="next"` response header and a `page_info` token.
  These pagination strategies are fundamentally different and require a
  dedicated abstraction layer before a live Shopify swap.
