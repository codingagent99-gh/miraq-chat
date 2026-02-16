"""
Labeled training data for all intents.
Can be used for rule-based testing or ML model training.
"""

TRAINING_DATA = [
    # ── Product Discovery ──
    {"utterance": "Show me all floor tiles",          "intent": "product_list"},
    {"utterance": "What wall tiles do you have?",     "intent": "product_list"},
    {"utterance": "Do you sell tiles?",               "intent": "product_list"},
    {"utterance": "Show me your tile catalog",        "intent": "product_catalog"},
    {"utterance": "What types of tiles do you offer?","intent": "product_types"},
    {"utterance": "I'm looking for bathroom tiles",   "intent": "product_list"},
    {"utterance": "Show me kitchen tiles",            "intent": "product_list"},
    {"utterance": "Show me Affogato",                 "intent": "product_search",   "entities": {"product_name": "Affogato"}},
    {"utterance": "Tell me about Akard",              "intent": "product_detail",   "entities": {"product_name": "Akard"}},
    {"utterance": "What goes with Affogato?",         "intent": "related_products", "entities": {"product_name": "Affogato"}},

    # ── Attribute Filters ──
    {"utterance": "What tile sizes do you have?",     "intent": "size_list"},
    {"utterance": "Show me large tiles",              "intent": "filter_by_size",      "entities": {"tile_size": '48"x48"'}},
    {"utterance": "Do you have small tiles?",         "intent": "filter_by_size",      "entities": {"tile_size": "12x24"}},
    {"utterance": "Show me matte finish tiles",       "intent": "filter_by_finish",    "entities": {"finish": "Matte"}},
    {"utterance": "Polished tiles",                   "intent": "filter_by_finish",    "entities": {"finish": "Polished"}},
    {"utterance": "Grey tone tiles",                  "intent": "filter_by_color",     "entities": {"color_tone": "Grey"}},
    {"utterance": "Show me 24x48 tiles",              "intent": "filter_by_size",      "entities": {"tile_size": '24"x48"'}},
    {"utterance": "6.5mm thick tiles",                "intent": "filter_by_thickness", "entities": {"thickness": "6.5mm"}},

    # ── Store-Specific ──
    {"utterance": "Marble look tiles",                "intent": "product_by_visual",  "entities": {"visual": "Marble"}},
    {"utterance": "Stone look products",              "intent": "product_by_visual",  "entities": {"visual": "Stone"}},
    {"utterance": "Made in Italy tiles",              "intent": "product_by_origin",  "entities": {"origin": "Italy"}},
    {"utterance": "Quick ship tiles",                 "intent": "product_quick_ship"},
    {"utterance": "2023 collection",                  "intent": "product_by_collection", "entities": {"collection_year": "2023"}},
    {"utterance": "Show me mosaic tiles",             "intent": "mosaic_products"},
    {"utterance": "Chip cards",                       "intent": "chip_card"},
    {"utterance": "What colors does Affogato come in?", "intent": "product_variations", "entities": {"product_name": "Affogato"}},

    # ── Discounts ──
    {"utterance": "Are there discounts available?",   "intent": "discount_inquiry"},
    {"utterance": "Do you offer bulk discounts?",     "intent": "bulk_discount"},
    {"utterance": "What tiles are on clearance?",     "intent": "clearance_products"},
    {"utterance": "Do you have any promotions?",      "intent": "promotions"},
    {"utterance": "Is there a coupon code?",          "intent": "coupon_inquiry"},

    # ── Account & Ordering ──
    {"utterance": "Can I save items for later?",      "intent": "save_for_later"},
    {"utterance": "Can I create a wishlist?",         "intent": "wishlist"},
    {"utterance": "How do I track my order?",         "intent": "order_tracking"},
    {"utterance": "Can I get status of my order?",    "intent": "order_status"},
    {"utterance": "Please order this item",           "intent": "place_order"},
]