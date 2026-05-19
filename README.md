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

### Parser pattern — backend-neutral return shapes

Every endpoint call constructor (e.g. `fetch_variant`) is paired with a
`parse_*` method (e.g. `parse_variant`) that maps the raw
`woo_client.execute()` response into a backend-neutral dict:

```python
from ecommerce import endpoints
from woo_client import woo_client

resp = woo_client.execute(endpoints.fetch_variant(product_id, variant_id))
if resp.get("success"):
    variant = endpoints.parse_variant(resp.get("data") or {})
    price   = variant["price"]        # str — sale_price or price or regular_price
    in_stock = variant["in_stock"]    # bool — True when stock_status == "instock"
    options  = variant["options"]     # dict — {attribute_name: option_value}
```

When a Shopify backend is added, `ShopifyEndpoints.parse_variant` will produce
the **same** neutral shape from Shopify's native response (mapping
`option1`/`option2`/`option3` → `options`, `inventory_quantity > 0` →
`in_stock`, etc.).  Callers do not change.

#### The `_raw` escape hatch

Every parser includes a `_raw` key with the original response.  Callers that
need a backend-specific field that has not yet been normalized can read it from
`_raw[...]` during the migration:

```python
variant = endpoints.parse_variant(resp.get("data") or {})
# Neutral field — works across all backends:
print(variant["price"])
# Not-yet-normalized Woo-specific field — use _raw during migration:
print(variant["_raw"].get("sku"))
```

Over time (future PRs), `_raw` access should drop to zero as all needed fields
are promoted to the neutral shape.

#### Available parsers

| Parser | Endpoint | Neutral keys added |
|---|---|---|
| `parse_product(response)` | `fetch_product` | `price: str`, `in_stock: bool` |
| `parse_variant(response)` | `fetch_variant` | `price: str`, `options: dict`, `in_stock: bool` |
| `parse_list_variants(response)` | `list_variants` | list of `parse_variant` dicts |
| `parse_order(response)` | `fetch_order` | `status: str`, `billing_address: dict`, `shipping_address: dict` |
| `parse_customer(response)` | `fetch_customer` | `default_address: dict`, `addresses: list[dict]` |
| `parse_list_published_products(response)` | `list_published_products` | `price: str`, `in_stock: bool` per item |

### Neutral catalog representation (Phase 4a — in progress)

`models/catalog.py` defines `CatalogAttribute`, `CatalogAttributeTerm`,
`CatalogCategory`, and `CatalogTag` — the canonical in-memory shape for
catalog data, populated by `store_loader/lookup_builder.py` alongside the
existing Woo-shaped indexes.

Consumers should prefer:
  - `loader.attribute_by_key` / `category_by_key` / `tag_by_key`
  - `loader.resolve_attribute(key)` / `resolve_attribute_term(attr_key, value)`
  - `loader.resolve_category(key)` / `resolve_tag(key)`

…over the legacy `attribute_by_slug` / `category_by_slug` / `tag_by_slug`
indexes, which will be removed in Phase 4c after all consumers are migrated
in Phases 4b.1–4b.8.

The `backend_ref` field on each catalog type is **opaque** to consumers.
Only `ecommerce/woo_endpoints.py` (and future `ecommerce/shopify_endpoints.py`)
should read its contents — that's where backend-specific identifiers (Woo's
`pa_*` taxonomy strings, integer IDs; Shopify's GIDs) get translated into
outgoing API calls.

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

- **Phase 4 — Pagination abstraction**: WooCommerce uses `?page=N` + the
  `X-WP-TotalPages` response header.  Shopify uses cursor-based pagination via
  the `Link: <url>; rel="next"` response header and a `page_info` token.
  These pagination strategies are fundamentally different and require a
  dedicated abstraction layer before a live Shopify swap.

- **Phase 5 — Shopify implementation**: Create `ecommerce/shopify_endpoints.py`
  with a `ShopifyEndpoints` class that implements every method — both
  call-constructors *and* parsers — declared in `ecommerce/endpoints.py`.

### Deferred normalizations (not in Phase 3)

The following response fields are not yet normalized in the parsers.  Callers
that depend on them should read from `_raw[...]` until a future PR covers them:

| Field / endpoint | Reason deferred |
|---|---|
| `billing.first_name`, `billing.email`, `billing.phone` (orders, customers) | Removing these from the neutral address shape would break the frontend `format_order_for_frontend` contract |
| `stock_status` string on `fetch_product` / `routes/products.py` | Sent directly to the frontend as-is; changing the key would break existing UI consumers |
| Stock status from `check_stock` / `products_advanced` (custom-plugin format) | Custom-plugin response shape (`data.products[].stock_status`) is deeply Woo-specific and needs Shopify exposure to design the neutral equivalent |
| `meta_data` arrays | Woo-specific; no Shopify equivalent yet defined |
