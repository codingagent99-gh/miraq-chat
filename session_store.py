"""
In-memory session store for chat sessions.
"""

from typing import Dict

# ═══════════════════════════════════════════
# SESSION STORE (in-memory for now)
# ═══════════════════════════════════════════

sessions: Dict[str, Dict] = {}
