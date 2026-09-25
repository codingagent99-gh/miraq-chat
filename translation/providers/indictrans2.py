"""
translation/providers/indictrans2.py — AI4Bharat IndicTrans2, self-hosted.

IndicTrans2 is a model, not a server. translation_service/indictrans2_server.py
wraps it in a small HTTP service with a LibreTranslate-shaped API
(/languages, /translate taking ISO codes), run under PM2 by
indictrans2.config.js. This class is only the client for that service.

It has no detector, so detect() is the offline script detector — which is
also better than LibreTranslate's at telling Marathi from Hindi.
"""

from __future__ import annotations

from typing import List, Sequence

from translation.base import Detection, HttpProvider, TranslationUnavailable, as_text_list
from translation.script_detect import detect_by_script

# The 22 scheduled Indian languages IndicTrans2 covers (en <-> each).
# Used when the service is down so tenant settings can still be validated.
INDICTRANS2_LANGUAGES = {
    "as", "bn", "brx", "doi", "gom", "gu", "hi", "kn", "ks", "mai", "ml",
    "mni", "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta", "te", "ur",
}


class IndicTrans2Provider(HttpProvider):
    name = "indictrans2"

    def supported_languages(self) -> set:
        return set(INDICTRANS2_LANGUAGES)

    def detect(self, text: str) -> List[Detection]:
        return detect_by_script(text)

    def translate_batch(self, texts: Sequence[str], source: str, target: str) -> List[str]:
        if not texts:
            return []
        data = self._request(
            "POST", "/translate", json={"q": list(texts), "source": source, "target": target},
        )
        result = as_text_list((data or {}).get("translatedText"), len(texts))
        if result is None:
            raise TranslationUnavailable("indictrans2: unexpected translatedText shape")
        return result
