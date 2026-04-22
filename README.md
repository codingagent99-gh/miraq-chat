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

## Feature Flags

| Variable | Default | Description |
|---|---|---|
| `HEADLESS_CHECKOUT_ENABLED` | `false` | Gates the headless WooCommerce Store API checkout migration. When `true` (or `"1"` / `"yes"`), checkout-only actions (`OPEN_CHECKOUT_PANEL`, `PROPOSE_CHECKOUT_ADDRESS`, `UPDATE_CART_ITEM`, `REMOVE_CART_ITEM`) are included in the `actions[]` array on every chat response, allowing the React widget to drive the full checkout flow against `/wc/store/v1`. Cart-level actions (`ADD_TO_CART`, `OPEN_CART_PANEL`) are always emitted regardless of this flag, since they mirror existing frontend behaviour. The flag defaults to `false` for backward compatibility and will be removed once the migration is complete in a later PR. |

## Store API Base

```
https://wgc.net.in/hn/wp-json/wc/v3/
```