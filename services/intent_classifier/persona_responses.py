"""
Persona Responses
Loads predefined responses from external JSON config file
"""

import json
import os
import random
from typing import List

# Load pre-defined responses from JSON config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config', 'predefined_persona_responses.json')

def _load_config():
    """Load responses from JSON file"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load responses.json: {e}")
        return {"persona_responses": {}, "out_of_scope_response": "I can only help with application-related questions."}

_config = _load_config()
PERSONA_RESPONSES = _config.get("persona_responses", {})
OUT_OF_SCOPE_RESPONSE = _config.get("out_of_scope_response", "I can only help with application-related questions.")


def get_persona_response(subtype: str) -> str:
    """Get a random response for persona subtype"""
    responses = PERSONA_RESPONSES.get(subtype, PERSONA_RESPONSES.get("unknown", ["Hello! How can I help?"]))
    return random.choice(responses)


def get_all_responses_for_subtype(subtype: str) -> List[str]:
    """Get all responses for a subtype"""
    return PERSONA_RESPONSES.get(subtype, PERSONA_RESPONSES.get("unknown", []))


def get_out_of_scope_response() -> str:
    """Get out-of-scope response"""
    return OUT_OF_SCOPE_RESPONSE


def reload_config():
    """Reload config from JSON (for runtime updates)"""
    global _config, PERSONA_RESPONSES, OUT_OF_SCOPE_RESPONSE
    _config = _load_config()
    PERSONA_RESPONSES = _config.get("persona_responses", {})
    OUT_OF_SCOPE_RESPONSE = _config.get("out_of_scope_response", "")

