"""
In-memory session store for chat sessions.

A background daemon thread runs every 15 minutes and evicts sessions
that have been inactive for longer than SESSION_TTL_SECONDS (default: 2 hours).
"""

import threading
import time
import logging
from datetime import datetime, timezone
from typing import Dict

logger = logging.getLogger("miraq_chat")

# ═══════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════

SESSION_TTL_SECONDS      = 2 * 60 * 60   # 2 hours inactive -> evict
CLEANUP_INTERVAL_SECONDS = 15 * 60       # run cleanup every 15 minutes

# ═══════════════════════════════════════════
# SESSION STORE (in-memory)
# ═══════════════════════════════════════════

# Structure per session:
# {
#   "history":         [...],
#   "user_context":    {...},
#   "created_at":      "ISO string",
#   "last_active":     float  <- UTC timestamp, updated on every request
#   "variation_cache": {
#       "<product_id>": {
#           "variations": [...],
#           "parent_raw": {...},
#       }
#   }
# }
sessions: Dict[str, Dict] = {}


def touch_session(session_id: str) -> None:
    """Update last_active timestamp. Call on every chat request."""
    if session_id and session_id in sessions:
        sessions[session_id]["last_active"] = datetime.now(timezone.utc).timestamp()


# ═══════════════════════════════════════════
# BACKGROUND CLEANUP THREAD
# ═══════════════════════════════════════════

def _cleanup_loop() -> None:
    """Evict sessions inactive longer than SESSION_TTL_SECONDS."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            now = datetime.now(timezone.utc).timestamp()
            expired = [
                sid
                for sid, data in list(sessions.items())
                if now - data.get("last_active", 0) > SESSION_TTL_SECONDS
            ]
            if expired:
                for sid in expired:
                    sessions.pop(sid, None)
                logger.info(
                    f"Session cleanup: evicted {len(expired)} expired session(s) "
                    f"| active_sessions={len(sessions)}"
                )
        except Exception as exc:
            logger.warning(f"Session cleanup error: {exc}")


_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()