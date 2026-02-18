"""
Session Management

In-memory session store for managing chat sessions.
"""

from typing import Dict

# In-memory session store
sessions: Dict[str, Dict] = {}


def get_session(session_id: str) -> Dict:
    """Get session by ID. Returns empty dict if not found."""
    return sessions.get(session_id, {})


def create_or_update_session(session_id: str, session_data: Dict) -> None:
    """Create or update a session."""
    sessions[session_id] = session_data


def session_exists(session_id: str) -> bool:
    """Check if a session exists."""
    return session_id in sessions
