"""
translation/script_detect.py — offline language guess from the writing script.

IndicTrans2 has no /detect, and LibreTranslate's detector regularly calls
Marathi "Hindi" (same Devanagari script). The script alone narrows the answer
to a small set of languages; for Devanagari a few very common function words
separate Marathi from Hindi well enough for chat messages.

Limits, on purpose:
  * Romanised Indian text ("mala tiles pahije") is Latin script and comes out
    as English. Telling romanised Marathi from English needs a real
    language-ID model — out of scope here.
  * Very short messages ("हो", "ठीक") carry no marker words; the caller
    resolves those with the conversation's sticky language or the tenant's
    default_language.
"""

from __future__ import annotations

import re
from typing import List

from translation.base import Detection

# Unicode block → languages written in it (ISO 639-1, plus 3-letter where no 2-letter exists).
_SCRIPTS = [
    ("devanagari", 0x0900, 0x097F, ["hi", "mr", "ne", "sa", "mai", "gom", "brx", "doi"]),
    ("bengali",    0x0980, 0x09FF, ["bn", "as", "mni"]),
    ("gurmukhi",   0x0A00, 0x0A7F, ["pa"]),
    ("gujarati",   0x0A80, 0x0AFF, ["gu"]),
    ("oriya",      0x0B00, 0x0B7F, ["or"]),
    ("tamil",      0x0B80, 0x0BFF, ["ta"]),
    ("telugu",     0x0C00, 0x0C7F, ["te"]),
    ("kannada",    0x0C80, 0x0CFF, ["kn"]),
    ("malayalam",  0x0D00, 0x0D7F, ["ml"]),
    ("arabic",     0x0600, 0x06FF, ["ur", "ks", "sd"]),
    ("olchiki",    0x1C50, 0x1C7F, ["sat"]),
]

_MR_MARKERS = {
    "आहे", "आहेत", "नाही", "मला", "तुम्ही", "आणि", "पण", "काय", "कसे", "कसा",
    "पाहिजे", "हवे", "हवा", "हवी", "आम्ही", "माझा", "माझी", "माझे", "तुमचा",
    "तुमची", "तुमचे", "किती", "कुठे", "होय", "नको", "द्या", "दाखवा", "मध्ये",
    "साठी", "चा", "ची", "चे", "ला", "ना",
}
_HI_MARKERS = {
    "है", "हैं", "नहीं", "मुझे", "आप", "और", "लेकिन", "क्या", "कैसे", "कैसा",
    "चाहिए", "हम", "मेरा", "मेरी", "मेरे", "आपका", "आपकी", "आपके", "कितना",
    "कितने", "कहाँ", "कहां", "हाँ", "हां", "दिखाओ", "दिखाइए", "में", "के",
    "लिए", "का", "की", "को", "से", "था", "थी",
}
_WORD_RE = re.compile(r"[\u0900-\u097F]+")


def _script_counts(text: str) -> dict:
    counts = {"latin": 0}
    for ch in text:
        cp = ord(ch)
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            counts["latin"] += 1
            continue
        for name, lo, hi, _langs in _SCRIPTS:
            if lo <= cp <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    return counts


def _devanagari_ranking(text: str) -> List[Detection]:
    words = _WORD_RE.findall(text)
    mr = sum(1 for w in words if w in _MR_MARKERS) + text.count("ळ")
    hi = sum(1 for w in words if w in _HI_MARKERS)
    if mr == hi:
        # No evidence either way: report both as equally (un)likely and let
        # the caller's sticky/default language decide.
        return [Detection("mr", 0.4), Detection("hi", 0.4)]
    total = mr + hi
    top, other = ("mr", "hi") if mr > hi else ("hi", "mr")
    conf = 0.55 + 0.4 * (max(mr, hi) / total)
    return [Detection(top, min(conf, 0.95)), Detection(other, 1 - min(conf, 0.95))]


def detect_by_script(text: str) -> List[Detection]:
    """Ranked guesses from the dominant script. Empty list when there are no letters."""
    counts = _script_counts(text or "")
    letters = sum(counts.values())
    if letters == 0:
        return []
    dominant = max(counts, key=counts.get)
    share = counts[dominant] / letters

    if dominant == "latin":
        return [Detection("en", 0.6 + 0.3 * share)]
    if dominant == "devanagari":
        return _devanagari_ranking(text)

    langs = next(l for n, _lo, _hi, l in _SCRIPTS if n == dominant)
    if len(langs) == 1:
        return [Detection(langs[0], 0.6 + 0.35 * share)]
    return [Detection(lang, 0.4) for lang in langs]


def script_languages(text: str) -> List[str]:
    """Every language that could have written this text, by script. Unranked."""
    counts = _script_counts(text or "")
    counts.pop("latin", None)
    if not counts or not any(counts.values()):
        return []
    dominant = max(counts, key=counts.get)
    return list(next(l for n, _lo, _hi, l in _SCRIPTS if n == dominant))
