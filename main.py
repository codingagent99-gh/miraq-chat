"""
Main entry point: Loads store data → classifies → builds API calls.

STARTUP FLOW:
  1. Load .env credentials
  2. Fetch categories, tags, attributes from WooCommerce API
  3. Build keyword maps for category matching
  4. Ready to classify user queries
"""

import json
import sys
from classifier import classify
from api_builder import build_api_calls
from store_registry import set_store_loader, get_store_loader


def initialize():
    """
    ★ STARTUP: Fetch live category/tag/attribute data from WooCommerce.
    Must be called ONCE before classifying any queries.
    """
    from services.store_loader import StoreLoader

    loader = StoreLoader()

    try:
        loader.load_all()
        set_store_loader(loader)

        # Print what we loaded
        loader.print_categories()
        print()
        loader.print_keywords()
        print()

    except Exception as e:
        print(f"⚠️  Could not load store data: {e}")
        print("   Falling back to static registry (categories will not work)")
        print("   Make sure .env has valid WOO_CONSUMER_KEY and WOO_CONSUMER_SECRET")
        print()


def process(utterance: str):
    """Classify a single utterance and print results."""
    result = classify(utterance)
    calls = build_api_calls(result)

    print(f"\n{'━'*70}")
    print(f"💬  \"{utterance}\"")
    print(f"🎯  Intent:     {result.intent.value}")
    print(f"📊  Confidence: {result.confidence:.0%}")

    # Print relevant entities
    e = result.entities
    parts = []
    if e.product_name:    parts.append(f"product={e.product_name}")
    if e.product_slug:    parts.append(f"slug={e.product_slug}")
    if e.category_id:     parts.append(f"category={e.category_name} (id={e.category_id})")
    if e.category_slug:   parts.append(f"cat_slug={e.category_slug}")
    if e.visual:          parts.append(f"visual={e.visual}")
    if e.finish:          parts.append(f"finish={e.finish}")
    if e.tile_size:       parts.append(f"size={e.tile_size}")
    if e.color_tone:      parts.append(f"color={e.color_tone}")
    if e.thickness:       parts.append(f"thickness={e.thickness}")
    if e.origin:          parts.append(f"origin={e.origin}")
    if e.collection_year: parts.append(f"year={e.collection_year}")
    if e.quick_ship:      parts.append(f"quick_ship=True")
    if e.on_sale:         parts.append(f"on_sale=True")
    if e.tag_slugs:       parts.append(f"tags={e.tag_slugs}")
    if e.order_id:        parts.append(f"order_id={e.order_id}")
    if e.order_count:     parts.append(f"order_count={e.order_count}")
    if e.reorder:         parts.append(f"reorder={e.reorder}")
    if e.order_item_name: parts.append(f"order_item_name={e.order_item_name}")
    if parts:
        print(f"📦  Entities:   {', '.join(parts)}")

    for i, call in enumerate(calls):
        print(f"\n   🔗 API Call {i+1}:")
        print(f"      {call.method} {call.endpoint}")
        print(f"      Params: {json.dumps(call.params, indent=2)}")
        if call.body:
            print(f"      Body:   {json.dumps(call.body, indent=2)}")
        print(f"      → {call.description}")


if __name__ == "__main__":
    # ★ Step 1: Load store data from WooCommerce
    initialize()

    # ★ Step 2: Test all utterances
    tests = [
        # ── Category-Based (NEW — matched against live categories) ──
        "Show me all floor tiles",
        "What wall tiles do you have?",
        "I'm looking for bathroom tiles",
        "Show me kitchen tiles",
        "What categories do you have?",
        "List all categories",
        "Show me outdoor tiles",

        # ── Product Discovery ──
        "Do you sell tiles?",
        "Show me your tile catalog",
        "What types of tiles do you offer?",
        "Show me Affogato tiles",
        "Tell me about Akard",
        "What goes with Affogato?",

        # ── Attribute Filters ──
        "What tile sizes do you have?",
        "Show me large tiles",
        "Do you have small tiles?",
        "Show me matte finish tiles",
        "Show me polished tiles",
        "Grey tone tiles",
        "White tiles",
        "Show me 24x48 tiles",
        "6.5mm thick tiles",

        # ── Store-Specific ──
        "Show me marble look tiles",
        "Stone look products",
        "Terrazzo tiles",
        "Made in Italy tiles",
        "Quick ship tiles",
        "2023 collection",
        "Show me mosaic tiles",
        "Bullnose trim options",
        "Show me chip cards",
        "What colors does Affogato come in?",
        "Can I get a sample?",

        # ── Discounts ──
        "Are there discounts available?",
        "Do you offer bulk discounts?",
        "What tiles are on clearance?",
        "Do you have any promotions?",
        "Is there a coupon code?",

        # ── Account & Ordering ──
        "Can I save items for later?",
        "Can I create a wishlist?",
        "How do I track my order?",
        "Can I get status of my order #1234?",
        "Please order this item",

        # ── Order History & Reorder ──
        "show my order history",
        "what have I ordered before?",
        "my past orders",
        "show my last order",
        "what did I order last?",
        "my most recent order",
        "repeat my last order",
        "reorder my previous order",
        "order again",

        # ── Quick Order ──
        "order this item Ansel",
        "buy Allspice",
        "I want to order Waterfall tiles",
        "order Cairo",
        "purchase Divine",
    ]

    for t in tests:
        process(t)