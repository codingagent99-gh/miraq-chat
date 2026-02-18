"""
Suggestion Generator

Generates follow-up suggestions based on intent and context.
"""

from typing import List

from models import Intent, ExtractedEntities


def generate_suggestions(
    intent: Intent,
    entities: ExtractedEntities,
    products: List[dict],
) -> List[str]:
    """Generate follow-up suggestions based on context."""
    suggestions = []

    # Order-specific suggestions
    if intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY, Intent.REORDER):
        suggestions.append("Show my order history")
        suggestions.append("Reorder my last purchase")
        suggestions.append("Track my order")
        suggestions.append("Show me what's on sale")
        return suggestions[:4]

    if intent == Intent.QUICK_ORDER:
        suggestions.append("Show me all products")
        suggestions.append("What categories do you have?")
        suggestions.append("Show me quick ship products")
        suggestions.append("What's on sale?")
        return suggestions[:4]

    # Product-specific suggestions
    if products and len(products) == 1:
        p = products[0]
        name = p.get("name", "")
        base_name = name.split(" ")[0] if name else ""

        if "Chip Card" not in name:
            suggestions.append(f"Show me {base_name} Chip Card")
        if "Mosaic" not in name:
            suggestions.append(f"Show me {base_name} Mosaic")
        suggestions.append(f"What colors does {base_name} come in?")
        suggestions.append(f"What goes with {base_name}?")

    elif products and len(products) > 1:
        # Suggest browsing related
        if intent == Intent.CATEGORY_BROWSE and entities.category_name:
            suggestions.append(f"Show me more {entities.category_name} products")
        suggestions.append("Show me what's on sale")
        suggestions.append("Show me quick ship products")

    # General suggestions
    if intent == Intent.PRODUCT_SEARCH:
        suggestions.append("Show me all chip cards")
    if intent not in (Intent.CATEGORY_LIST, Intent.PRODUCT_CATALOG):
        suggestions.append("What categories do you have?")

    # Always include a fallback
    if not suggestions:
        suggestions = [
            "Show me all products",
            "What categories do you have?",
            "Show me what's on sale",
            "Quick ship tiles",
        ]

    return suggestions[:4]  # Max 4 suggestions
