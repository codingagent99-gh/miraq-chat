"""
models — Domain dataclasses, enums, and ORM models.

Re-exports everything so existing `from models import X` statements
work unchanged across the entire codebase.
"""

# ── DB models ──
from models.db_models import (
    db,
    DEFAULT_CONTEXT,
    Conversation,
    Message,
    Tenant,
    CatalogSnapshot,
)

# ── Domain models ──
from models.domain import (
    Intent,
    OrPair,
    ExtractedEntities,
    WooAPICall,
    ClassifiedResult,
)

# ── Catalog models (Phase 4a) ──
from models.catalog import (
    CatalogAttribute,
    CatalogAttributeTerm,
    CatalogCategory,
    CatalogTag,
)

# ── Shopify token (auto-refresh OAuth token storage) ──
from models.shopify_token import ShopifyToken

from models.chat_usage import ChatUsage, CustomerPlan
from models import db