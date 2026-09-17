"""
tenant_snapshot_store.py — Persists/rehydrates a tenant's built store data.

Persisted (expensive to (re)compute): raw catalog data (categories, tags,
products, all_attributes_raw, currency_symbol) — the WooCommerce fetch.

Rebuilt on load via build_all_lookups() (cheap, pure Python, no network
work): every derived lookup index and the BoundedVariationCache instance.

Adapted from the reference implementation: that version also persisted
semantic_tensors/semantic_keys/semantic_dictionary (a semantic vector model's
precomputed embeddings) via torch.save/torch.load. This codebase has no
vector model — it was removed from StoreLoader (see store_loader/__init__.py)
because it was the only GIL-serializing CPU work on the request path plus a
large per-worker memory cost, superseded by utils/typo_correction.py's fuzzy
matching. Carrying that handling here would reference StoreLoader attributes
that no longer exist. Dropped entirely, including the torch dependency.

SINGLE-SERVER TODAY, MULTI-SERVER LATER: LocalDiskSnapshotStore is the only
implementation. The interface (save/load/exists) is the seam — swap in an
S3-backed store behind the same three methods when multi-server lands;
nothing else in the codebase needs to change.
"""

from __future__ import annotations
import os
import json
import time
import shutil
from abc import ABC, abstractmethod
from typing import Optional

from chat_logger import get_logger

logger = get_logger("miraq_chat")

_SNAPSHOT_DIR = os.getenv("TENANT_SNAPSHOT_DIR", os.path.join(os.getcwd(), ".tenant_snapshots"))


class SnapshotStore(ABC):
    @abstractmethod
    def save(self, tenant_id: str, data: dict) -> None: ...
    @abstractmethod
    def load(self, tenant_id: str) -> Optional[dict]: ...
    @abstractmethod
    def exists(self, tenant_id: str) -> bool: ...
    @abstractmethod
    def delete(self, tenant_id: str) -> None: ...

class LocalDiskSnapshotStore(SnapshotStore):
    """
    Per tenant, under TENANT_SNAPSHOT_DIR:
      <tenant_id>/catalog.json  — raw data (JSON-safe)
      <tenant_id>/meta.json     — snapshot_built_at, for observability
    """

    def __init__(self, base_dir: str = _SNAPSHOT_DIR):
        self._base_dir = base_dir
        os.makedirs(self._base_dir, exist_ok=True)

    def _tenant_dir(self, tenant_id: str) -> str:
        d = os.path.join(self._base_dir, tenant_id)
        os.makedirs(d, exist_ok=True)
        return d

    def save(self, tenant_id: str, data: dict) -> None:
        d = self._tenant_dir(tenant_id)
        catalog_path = os.path.join(d, "catalog.json")
        meta_path = os.path.join(d, "meta.json")

        catalog = {
            "categories": data.get("categories", []),
            "tags": data.get("tags", []),
            "products": data.get("products", []),
            "all_attributes_raw": data.get("all_attributes_raw", []),
            "currency_symbol": data.get("currency_symbol", "$"),
            "expected_product_count": data.get("expected_product_count"),
        }

        # Write-then-rename: avoids a half-written snapshot if the process
        # dies mid-write (atomic on the same filesystem).
        tmp = catalog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(catalog, f, indent=2)   # indent=2 for readability
        os.replace(tmp, catalog_path)

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"snapshot_built_at": time.time()}, f, indent=2)

        logger.info(f"SnapshotStore: saved | tenant={tenant_id} | products={len(catalog['products'])}")

    def load(self, tenant_id: str) -> Optional[dict]:
        d = os.path.join(self._base_dir, tenant_id)
        catalog_path = os.path.join(d, "catalog.json")
        if not os.path.exists(catalog_path):
            return None
        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"SnapshotStore: load failed | tenant={tenant_id} | error={e}", exc_info=True)
            return None

    def exists(self, tenant_id: str) -> bool:
        return os.path.exists(os.path.join(self._base_dir, tenant_id, "catalog.json"))

    def delete(self, tenant_id: str) -> None:
        """
        Best-effort removal of a tenant's snapshot directory on teardown.
        Non-fatal by design: the caller has already dropped the physical DB by
        this point, so a leftover snapshot dir is cosmetic, not a correctness
        issue. Logs and returns on failure rather than raising — e.g. a Windows
        file lock must not fail an otherwise-complete teardown.

        Param is named tenant_id (not license_id like the other methods) because
        callers pass str(tenant_id) as the key.
        """
        d = os.path.join(self._base_dir, tenant_id)
        if not os.path.isdir(d):
            logger.info(f"SnapshotStore: delete — nothing to remove | tenant={tenant_id}")
            return
        try:
            shutil.rmtree(d)
            logger.info(f"SnapshotStore: deleted snapshot dir | tenant={tenant_id}")
        except Exception as e:
            logger.error(
                f"SnapshotStore: delete failed (non-fatal) | tenant={tenant_id} | error={e}",
                exc_info=True,
            )

def loader_to_snapshot_dict(loader) -> dict:
    """Extract exactly the fields save() persists from a live StoreLoader."""
    return {
        "categories": loader.categories,
        "tags": loader.tags,
        "products": loader.products,
        "all_attributes_raw": loader.all_attributes_raw,
        "currency_symbol": loader.currency_symbol,
        "expected_product_count": loader._expected_product_count,
    }


def apply_snapshot_to_loader(loader, snapshot: dict) -> None:
    """Populate a freshly-constructed loader from a snapshot, then rebuild
    the cheap lookup indexes. No WooCommerce fetch."""
    raw = {
        "categories":              snapshot.get("categories", []),
        "tags":                    snapshot.get("tags", []),
        "products":                snapshot.get("products", []),
        "all_attributes_raw":      snapshot.get("all_attributes_raw", []),
        "currency_symbol":         snapshot.get("currency_symbol", "$"),
        "_expected_product_count": snapshot.get("expected_product_count"),
    }

    from store_loader.lookup_builder import build_all_lookups
    build_all_lookups(loader, raw=raw)
    loader._validate_load()
    loader._last_loaded = time.time()
    loader._loaded_from_cache = True  # a snapshot is a form of cache, not a live fetch


# Process-wide singleton — swap this line for an S3-backed store when
# multi-server lands; nothing else needs to change.
snapshot_store: SnapshotStore = LocalDiskSnapshotStore()