"""
parsers/address_parser.py

Detects a plausible address in a free-text user message and converts it
to a normalised address dict suitable for the PROPOSE_CHECKOUT_ADDRESS action.

The parser is intentionally lightweight — it looks for the structural
hallmarks of a postal address (street number + name, city, optional
postcode) rather than trying to parse every world address format.
"""

import re
from typing import Optional, Dict, Any


# ── Patterns ──────────────────────────────────────────────────────────────────

# UK postcode:  SW1A 2AA, NW1 6XE, EC4M 5UT, …
_UK_POSTCODE_RE = re.compile(
    r'\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b',
    re.IGNORECASE,
)

# US ZIP:  12345  or  12345-6789
_US_ZIP_RE = re.compile(r'\b\d{5}(?:-\d{4})?\b')

# Indian PIN code:  6-digit number
_IN_PIN_RE = re.compile(r'\b[1-9]\d{5}\b')

# Minimum-viable street: starts with a digit (house/flat number)
_STREET_NUMBER_RE = re.compile(r'\b\d+[A-Z]?\b', re.IGNORECASE)

# Common address-indicator words
_ADDRESS_KEYWORDS_RE = re.compile(
    r'\b(street|st|road|rd|avenue|ave|boulevard|blvd|lane|ln|drive|dr|'
    r'court|ct|place|pl|way|close|crescent|park|square|row|mews|'
    r'terrace|gardens|gate|hill|grove|house|flat|apt|apartment|floor|'
    r'building|bldg|suite|ste|unit|block|sector|nagar|colony|layout)\b',
    re.IGNORECASE,
)

# Trigger phrases that strongly suggest an address is being provided
_ADDRESS_TRIGGER_RE = re.compile(
    r'\b(ship\s+(it\s+)?to|deliver\s+(it\s+)?to|send\s+(it\s+)?to|'
    r'my\s+address\s+is|address\s+is|use\s+address|shipping\s+address)\b',
    re.IGNORECASE,
)


def extract_address(text: str) -> Optional[Dict[str, Any]]:
    """
    Attempt to extract a postal address from *text*.

    Returns a dict with whichever fields could be identified, or ``None``
    if no plausible address is found.

    Returned keys (all optional): ``address_1``, ``city``, ``postcode``,
    ``country`` (inferred from postcode format when possible).
    """
    if not text:
        return None

    has_trigger   = bool(_ADDRESS_TRIGGER_RE.search(text))
    has_keyword   = bool(_ADDRESS_KEYWORDS_RE.search(text))
    has_street_no = bool(_STREET_NUMBER_RE.search(text))

    uk_match = _UK_POSTCODE_RE.search(text)
    us_match = _US_ZIP_RE.search(text)
    in_match = _IN_PIN_RE.search(text)

    has_postcode = bool(uk_match or us_match or in_match)

    # Need at least one strong signal: trigger phrase OR (address keyword + number) OR postcode
    has_address_signal = has_trigger or (has_keyword and has_street_no) or has_postcode
    if not has_address_signal:
        return None

    # ── Try to pull apart the address from the trigger phrase ──
    remainder = text
    trigger_m = _ADDRESS_TRIGGER_RE.search(text)
    if trigger_m:
        # Everything after the trigger is the address
        remainder = text[trigger_m.end():].strip(" ,.-")

    # ── Split on comma — most addresses are comma-delimited ──
    parts = [p.strip() for p in remainder.split(",") if p.strip()]

    result: Dict[str, Any] = {}

    if parts:
        result["address_1"] = parts[0]
    if len(parts) >= 2:
        result["city"] = parts[1]
    if len(parts) >= 3:
        # Could be "state postcode" or just "postcode"
        third = parts[2].strip()
        tokens = third.split()
        if len(tokens) >= 2:
            result["state"] = tokens[0]
            result["postcode"] = " ".join(tokens[1:])
        else:
            result["postcode"] = third

    # Override postcode with the regex match if we found a cleaner one
    if uk_match and "postcode" not in result:
        result["postcode"] = uk_match.group(0).upper()
        result.setdefault("country", "GB")
    elif us_match and "postcode" not in result:
        result["postcode"] = us_match.group(0)
        result.setdefault("country", "US")
    elif in_match and "postcode" not in result:
        result["postcode"] = in_match.group(0)
        result.setdefault("country", "IN")

    if not result:
        return None

    return result


def address_summary(parsed: Dict[str, Any]) -> str:
    """Return a short human-readable summary of a parsed address dict."""
    parts = [
        parsed.get("address_1", ""),
        parsed.get("city", ""),
        parsed.get("state", ""),
        parsed.get("postcode", ""),
    ]
    return ", ".join(p for p in parts if p)
