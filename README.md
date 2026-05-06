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
User Utterance → Classifier → Entity Extraction → API Builder → EcommerceClient → WooCommerce API
```

## E-commerce Backend Abstraction

All HTTP interactions with the e-commerce backend are isolated behind the
`EcommerceClient` abstract interface defined in `ecommerce_client.py`.

### Current implementation: WooCommerce

`WooClient` (in `woo_client.py`) inherits from `EcommerceClient` and handles
all WooCommerce-specific concerns: authentication, request headers, response
normalisation, and logging.

A module-level singleton is available for the rest of the application:

```python
from woo_client import woo_client   # EcommerceClient instance
result = woo_client.execute(api_call)
```

### Selecting a backend

The active backend is chosen by the `ECOMMERCE_BACKEND` environment variable
(default: `woocommerce`):

```
ECOMMERCE_BACKEND=woocommerce   # currently the only supported value
```

Use the factory when you need a fresh client instance:

```python
from ecommerce_client import get_ecommerce_client
client = get_ecommerce_client()
```

### Adding a new backend (e.g. Shopify)

1. Create `shopify_client.py` with `ShopifyClient(EcommerceClient)` that
   implements `execute()` and `execute_all()`.
2. Add a branch in `get_ecommerce_client()` in `ecommerce_client.py`:

   ```python
   elif backend == "shopify":
       from shopify_client import ShopifyClient
       return ShopifyClient()
   ```

3. Set `ECOMMERCE_BACKEND=shopify` in the environment.

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