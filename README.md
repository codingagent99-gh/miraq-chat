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

## Ecommerce endpoint normalization

The WooCommerce boundary now lives under `ecommerce/`.

| Endpoint | Normalized response shape |
|---|---|
| `fetch_product` / `products_advanced` | `id`, `name`, `type`, `price`, `original_price`, `in_stock`, `stock_quantity`, `options`, `variant_ids`, `_raw` |
| `fetch_variant` / `list_variants` | `id`, `price`, `original_price`, `in_stock`, `options`, `variation_label`, `_raw` |
| `fetch_order` / `list_customer_orders` / `create_order` | `id`, `number`, `status`, `currency_symbol`, `total`, `payment_method_label`, `created_at`, `paid_at`, `line_items`, `billing_address`, `shipping_address`, `_raw` |
| `fetch_customer` / `update_customer` | `id`, `first_name`, `last_name`, `email`, `default_address`, `addresses`, `_raw` |

Notes:
- `_raw` preserves the original Woo payload as an escape hatch for the Shopify migration.
- Phase 4 pagination remains deferred; list calls still use the existing page/per-page flow.
