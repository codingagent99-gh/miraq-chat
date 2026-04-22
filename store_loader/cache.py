"""
store_loader/cache.py — Bounded LRU cache with TTL for variation data.
"""

import time
from collections import OrderedDict
from chat_logger import get_logger

logger = get_logger("miraq_chat")


class BoundedVariationCache:
    """
    LRU cache with max size and TTL for variation data.

    - max_size: max number of product_ids to cache (default 200)
    - ttl: seconds before an entry expires (default 1 hour)
    """

    def __init__(self, max_size: int = 200, ttl: int = 3600):
        self._cache: OrderedDict = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl

    def get(self, product_id: int):
        """Get cached variations. Returns None on miss or expiry."""
        entry = self._cache.get(product_id)
        if entry is None:
            return None
        if time.time() - entry["cached_at"] > self.ttl:
            del self._cache[product_id]
            return None
        self._cache.move_to_end(product_id)
        return entry["variations"]

    def __setitem__(self, product_id: int, variations: list):
        """Cache variations for a product_id."""
        if product_id in self._cache:
            self._cache.move_to_end(product_id)
        self._cache[product_id] = {
            "variations": variations,
            "cached_at": time.time(),
        }
        while len(self._cache) > self.max_size:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.debug(f"BoundedVariationCache: Evicted product_id={evicted_key} (capacity={self.max_size})")

    def __len__(self):
        return len(self._cache)

    def clear(self):
        self._cache.clear()

    def pop(self, product_id: int, default=None):
        entry = self._cache.pop(product_id, None)
        return entry["variations"] if entry else default