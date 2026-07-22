"""
tenant_snapshot_store.py — Persists/rehydrates a tenant's built store data.

Persisted (expensive to (re)compute): raw catalog data (categories, tags,
products, all_attributes_raw, currency_symbol) and precomputed semantic
vectors (semantic_tensors, semantic_keys, semantic_dictionary) — the
WooCommerce fetch and the embedding-model encode pass.

Rebuilt on load via build_all_lookups() (cheap, pure Python, no network or
GPU work): every derived lookup index and the BoundedVariationCache instance.
See chat for why — short version: their pickling-safety isn't established
without models/catalog.py and store_loader/cache.py, so rebuilding from
already-fetched raw data sidesteps that risk for a few milliseconds' cost.

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

import torch

from chat_logger import get_logger

logger = get_logger("miraq_chat")

_SNAPSHOT_DIR = os.getenv("TENANT_SNAPSHOT_DIR", os.path.join(os.getcwd(), ".tenant_snapshots"))


class SnapshotStore(ABC):
    @abstractmethod
    def save(self, tenant_id: str, data: dict) -> None:
    @abstractmethod
    def load(self, tenant_id: str) -> Optional[dict]:
    @abstractmethod
    def exists(self, tenant_id: str) -> bool:
    @abstractmethod
    def delete(self, tenant_id: str) -> None: ...

class LocalDiskSnapshotStore(SnapshotStore):
    """
    Per tenant, under TENANT_SNAPSHOT_DIR:
      <tenant_id>/catalog.json  — raw data + semantic_keys/dictionary (JSON-safe)
      <tenant_id>/vectors.pt    — semantic_tensors (torch.save)
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
        vectors_path = os.path.join(d, "vectors.pt")
        meta_path = os.path.join(d, "meta.json")

        catalog = {
            "categories": data.get("categories", []),
            "tags": data.get("tags", []),
            "products": data.get("products", []),
            "all_attributes_raw": data.get("all_attributes_raw", []),
            "currency_symbol": data.get("currency_symbol", "$"),
            "expected_product_count": data.get("expected_product_count"),
            "semantic_keys": data.get("semantic_keys", []),
            "semantic_dictionary": data.get("semantic_dictionary", {}),
        }

        # Write-then-rename: avoids a half-written snapshot if the process
        # dies mid-write (atomic on the same filesystem).
        tmp = catalog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(catalog, f, indent=2)   # indent=2 for readability
        os.replace(tmp, catalog_path)

        tensors = data.get("semantic_tensors")
        if tensors is not None:
            tmp_v = vectors_path + ".tmp"
            torch.save(tensors, tmp_v)
            os.replace(tmp_v, vectors_path)

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"snapshot_built_at": time.time()}, f, indent=2)

        logger.info(f"SnapshotStore: saved | tenant={tenant_id} | products={len(catalog['products'])}")
        
    def load(self, tenant_id: str) -> Optional[dict]:
        d = os.path.join(self._base_dir, tenant_id)
        catalog_path = os.path.join(d, "catalog.json")
        vectors_path = os.path.join(d, "vectors.pt")
        if not os.path.exists(catalog_path):
            return None
        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                catalog = json.load(f)
            catalog["semantic_tensors"] = torch.load(vectors_path) if os.path.exists(vectors_path) else None
            return catalog
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
        file lock on vectors.pt must not fail an otherwise-complete teardown.

        Param is named tenant_id (not license_id like the other methods) because
        since the tenant_id re-key, callers pass str(tenant_id) as the key.
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
        "semantic_keys": loader.semantic_keys,
        "semantic_dictionary": loader.semantic_dictionary,
        "semantic_tensors": loader.semantic_tensors,
    }


def apply_snapshot_to_loader(loader, snapshot: dict) -> None:
    """Populate a freshly-constructed loader from a snapshot, then rebuild
    the cheap lookup indexes. No WooCommerce fetch, no embedding encode."""
    from store_loader.lookup_builder import build_all_lookups

    loader.categories = snapshot.get("categories", [])
    loader.tags = snapshot.get("tags", [])
    loader.products = snapshot.get("products", [])
    loader.all_attributes_raw = snapshot.get("all_attributes_raw", [])
    loader.currency_symbol = snapshot.get("currency_symbol", "$")
    loader._expected_product_count = snapshot.get("expected_product_count")
    loader.semantic_keys = snapshot.get("semantic_keys", [])
    loader.semantic_dictionary = snapshot.get("semantic_dictionary", {})
    loader.semantic_tensors = snapshot.get("semantic_tensors")

    build_all_lookups(loader)
    loader._validate_load()
    loader._last_loaded = time.time()
    loader._loaded_from_cache = True  # a snapshot is a form of cache, not a live fetch


# Process-wide singleton — swap this line for an S3-backed store when
# multi-server lands; nothing else needs to change.
snapshot_store: SnapshotStore = LocalDiskSnapshotStore()