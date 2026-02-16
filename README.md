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