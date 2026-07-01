"""
db_engine_registry.py — Per-tenant SQLAlchemy engine cache (keyed by db_name).

Kept SEPARATE from the loader registry on purpose: loaders consume RAM
(catalog + vectors) while engines consume Postgres connections. They evict
under different pressure and an evicted engine needs dispose() (returning its
pooled connections) whereas an evicted loader needs nothing. Two small
independent LRUs are simpler than one coupled cache.
"""

from __future__ import annotations
import os
import re
import threading
from collections import OrderedDict

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from chat_logger import get_logger

logger = get_logger("miraq_chat")

_MAX_RESIDENT_ENGINES = int(os.getenv("TENANT_ENGINE_LRU_SIZE", "35"))

# Tiny pools — many tenants share one Postgres instance, so per-tenant
# connection count must stay low.
_POOL_SIZE = 1
_MAX_OVERFLOW = 2
_POOL_RECYCLE = 1800  # seconds

# Postgres identifier safety: db names are interpolated into the DSN, so we
# validate them hard. (CREATE DATABASE in Phase 4 has the same requirement.)
_VALID_DB_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


class DBEngineRegistry:
    def __init__(self, base_dsn: str):
        """
        Args:
            base_dsn: a libpq DSN whose database name is swapped per tenant,
                      e.g. postgresql://user:pass@host:5432/  — the path is
                      replaced with each tenant's db_name.
        """
        self._base_dsn = base_dsn
        self._engines: "OrderedDict[str, Engine]" = OrderedDict()
        self._lock = threading.Lock()

    def _dsn_for(self, db_name: str) -> str:
        # Replace only the database path segment of the base DSN.
        head, _sep, _old = self._base_dsn.rpartition("/")
        return f"{head}/{db_name}"

    def get_engine(self, db_name: str) -> Engine:
        if not _VALID_DB_NAME.match(db_name or ""):
            raise ValueError(f"Unsafe/invalid tenant db_name: {db_name!r}")

        with self._lock:
            eng = self._engines.get(db_name)
            if eng is not None:
                self._engines.move_to_end(db_name)  # mark most-recently-used
                return eng

            eng = create_engine(
                self._dsn_for(db_name),
                pool_size=_POOL_SIZE,
                max_overflow=_MAX_OVERFLOW,
                pool_recycle=_POOL_RECYCLE,
                pool_pre_ping=True,
                future=True,
            )
            self._engines[db_name] = eng
            logger.info(f"DBEngineRegistry: created engine | db={db_name}")

            # Insert-triggered eviction: pop oldest past the cap, synchronously.
            while len(self._engines) > _MAX_RESIDENT_ENGINES:
                old_name, old_eng = self._engines.popitem(last=False)
                try:
                    old_eng.dispose()
                    logger.info(f"DBEngineRegistry: evicted+disposed | db={old_name}")
                except Exception as e:
                    logger.error(
                        f"DBEngineRegistry: dispose failed | db={old_name} | error={e}",
                        exc_info=True,
                    )
            return eng

    def dispose_all(self):
        with self._lock:
            for name, eng in self._engines.items():
                try:
                    eng.dispose()
                except Exception:
                    pass
            self._engines.clear()