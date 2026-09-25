"""
translation/settings.py — the per-tenant translation settings.

Stored on the control-plane row as tenants.features["translation"]:

    {
      "enabled": true,
      "provider": "indictrans2",        # a name from translation/registry.py
      "languages": ["mr", "hi"],        # languages customers may write in (besides English)
      "default_language": "mr",         # tie-breaker when the text alone can't tell
      "translate_replies": true,        # answer in the customer's language
      "min_confidence": 0.5             # below this, a detection is ignored
    }

No key, or "enabled": false, means the chat is English-only for that tenant —
exactly the behaviour before translation existed. The JSONB column already
exists, so there is no migration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

FEATURE_KEY = "translation"
_FIELDS = {"enabled", "provider", "languages", "default_language",
           "translate_replies", "min_confidence"}


@dataclass
class TranslationSettings:
    enabled: bool = False
    provider: str = "libretranslate"
    languages: List[str] = field(default_factory=list)
    default_language: Optional[str] = None
    translate_replies: bool = True
    min_confidence: float = 0.5

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.languages)


def _norm_code(value) -> str:
    return str(value or "").strip().lower()


def from_features(features: Optional[dict]) -> TranslationSettings:
    """Lenient read for the hot path: bad stored values fall back to defaults."""
    raw = (features or {}).get(FEATURE_KEY)
    if not isinstance(raw, dict):
        return TranslationSettings()
    s = TranslationSettings()
    s.enabled = bool(raw.get("enabled", False))
    s.provider = _norm_code(raw.get("provider")) or s.provider
    langs = raw.get("languages") or []
    if isinstance(langs, str):
        langs = [langs]
    s.languages = [c for c in dict.fromkeys(_norm_code(l) for l in langs) if c and c != "en"]
    dl = _norm_code(raw.get("default_language"))
    s.default_language = dl if dl in s.languages else None
    s.translate_replies = bool(raw.get("translate_replies", True))
    try:
        s.min_confidence = min(max(float(raw.get("min_confidence", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        pass
    return s


def validate(payload: dict, *, base: Optional[TranslationSettings] = None,
             provider_names: List[str], supported_for) -> Tuple[Optional[TranslationSettings], List[str]]:
    """
    Strict parse for the admin endpoint. `base` given = PATCH (merge onto it),
    None = PUT (unspecified fields take defaults).

    `supported_for(provider_name)` returns that provider's language set, or an
    empty set when it can't be reached (then languages are not checked).
    Returns (settings, []) or (None, [errors]).
    """
    errors: List[str] = []
    if not isinstance(payload, dict):
        return None, ["body must be a JSON object"]

    unknown = set(payload) - _FIELDS
    if unknown:
        errors.append(f"unknown field(s): {', '.join(sorted(unknown))}")

    s = TranslationSettings(**(base.to_dict() if base else {}))

    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            errors.append("enabled must be true or false")
        else:
            s.enabled = payload["enabled"]

    if "translate_replies" in payload:
        if not isinstance(payload["translate_replies"], bool):
            errors.append("translate_replies must be true or false")
        else:
            s.translate_replies = payload["translate_replies"]

    if "provider" in payload:
        name = _norm_code(payload["provider"])
        if name not in provider_names:
            errors.append(f"provider must be one of: {', '.join(provider_names)}")
        else:
            s.provider = name

    if "languages" in payload:
        langs = payload["languages"]
        if isinstance(langs, str):
            langs = [langs]
        if not isinstance(langs, list):
            errors.append('languages must be a list of language codes, e.g. ["mr"]')
        else:
            s.languages = [c for c in dict.fromkeys(_norm_code(l) for l in langs) if c and c != "en"]

    if "default_language" in payload:
        s.default_language = _norm_code(payload["default_language"]) or None

    if "min_confidence" in payload:
        try:
            mc = float(payload["min_confidence"])
            if not 0.0 <= mc <= 1.0:
                raise ValueError
            s.min_confidence = mc
        except (TypeError, ValueError):
            errors.append("min_confidence must be a number between 0 and 1")

    if errors:
        return None, errors

    # Cross-field checks against the final merged settings.
    if s.enabled and not s.languages:
        errors.append("enabled is true but languages is empty")
    if s.default_language and s.default_language not in s.languages:
        errors.append("default_language must be one of languages")

    supported = supported_for(s.provider)
    if supported:
        bad = [l for l in s.languages if l not in supported]
        if bad:
            errors.append(
                f"provider {s.provider!r} does not support: {', '.join(bad)} "
                f"(supported: {', '.join(sorted(supported))})"
            )

    return (None, errors) if errors else (s, [])
