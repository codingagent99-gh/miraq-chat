"""
models — Domain dataclasses, enums, and ORM models.

Re-exports everything so existing `from models import X` statements
work unchanged across the entire codebase.
"""

# ── DB models ──
from models.db_models import (  # noqa: F401
    db,
    DEFAULT_CONTEXT,
    Conversation,
    Message,
)

# ── Domain models ──
from models.domain import (  # noqa: F401
    Intent,
    OrPair,
    ExtractedEntities,
    WooAPICall,
    ClassifiedResult,
)

# ── Catalog models (Phase 4a) ──
from models.catalog import (  # noqa: F401
    CatalogAttribute,
    CatalogAttributeTerm,
    CatalogCategory,
    CatalogTag,
)
