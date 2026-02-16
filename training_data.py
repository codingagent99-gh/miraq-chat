"""
Training data: maps example utterances to intents + entities.
Can be used for rule-based matching OR as labeled data for ML training.
"""

TRAINING_DATA = [
    # ───── Product Discovery & Search ─────
    {
        "utterance": "Show me all floor tiles",
        "intent": "product_category_browse",
        "entities": {"category": "floor-tiles", "product_type": "tiles"},
    },
    {
        "utterance": "What wall tiles do you have?",
        "intent": "product_category_browse",
        "entities": {"category": "wall-tiles", "product_type": "tiles"},
    },
    {
        "utterance": "Do you sell tiles?",
        "intent": "product_list",
        "entities": {"product_type": "tiles"},
    },
    {
        "utterance": "Show me your tile catalog",
        "intent": "product_catalog",
        "entities": {"product_type": "tiles"},
    },
    {
        "utterance": "What types of tiles do you offer?",
        "intent": "product_types",
        "entities": {"product_type": "tiles"},
    },
    {
        "utterance": "I'm looking for bathroom tiles",
        "intent": "product_category_browse",
        "entities": {"category": "bathroom-tiles", "room": "bathroom"},
    },
    {
        "utterance": "Show me kitchen tiles",
        "intent": "product_category_browse",
        "entities": {"category": "kitchen-tiles", "room": "kitchen"},
    },

    # ───── Size & Dimensions ─────
    {
        "utterance": "What tile sizes do you have?",
        "intent": "size_list",
        "entities": {"product_type": "tiles"},
    },
    {
        "utterance": "Show me large tiles",
        "intent": "size_filter",
        "entities": {"size": "large", "product_type": "tiles"},
    },
    {
        "utterance": "Do you have small tiles?",
        "intent": "size_filter",
        "entities": {"size": "small", "product_type": "tiles"},
    },
    {
        "utterance": "What are large format tiles?",
        "intent": "size_filter",
        "entities": {"size": "large-format", "product_type": "tiles"},
    },

    # ───── Discounts & Promotions ─────
    {
        "utterance": "Are there discounts available?",
        "intent": "discount_inquiry",
        "entities": {"on_sale": True},
    },
    {
        "utterance": "Do you offer bulk discounts?",
        "intent": "bulk_discount",
        "entities": {},
    },
    {
        "utterance": "What tiles are on clearance?",
        "intent": "clearance_products",
        "entities": {"tag": "clearance", "on_sale": True},
    },
    {
        "utterance": "Do you have any promotions?",
        "intent": "promotions",
        "entities": {"on_sale": True},
    },
    {
        "utterance": "Is there a coupon code?",
        "intent": "coupon_inquiry",
        "entities": {},
    },

    # ───── Account & Ordering ─────
    {
        "utterance": "Can I save items for later?",
        "intent": "save_for_later",
        "entities": {},
    },
    {
        "utterance": "Can I create a wishlist?",
        "intent": "wishlist",
        "entities": {},
    },
    {
        "utterance": "How do I track my order?",
        "intent": "order_tracking",
        "entities": {},
    },
    {
        "utterance": "Can I get status of my order?",
        "intent": "order_status",
        "entities": {},
    },
    {
        "utterance": "Please order this item",
        "intent": "place_order",
        "entities": {},
    },
]