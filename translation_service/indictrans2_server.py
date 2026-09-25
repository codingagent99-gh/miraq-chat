"""
translation_service/indictrans2_server.py — AI4Bharat IndicTrans2 as a small HTTP service.

IndicTrans2 ships as Hugging Face models, not a server. This wraps them in a
LibreTranslate-shaped API so MiraQ's IndicTrans2Provider stays a thin client:

    GET  /health                -> {"status": "ok", "loaded": [...]}
    GET  /languages             -> [{"code": "mr", "name": "Marathi"}, ...]
    POST /translate             {"q": str | [str], "source": "mr", "target": "en"}
                                -> {"translatedText": str | [str]}

Only English <-> Indic is served (the two directions MiraQ needs). Codes are
ISO 639-1; the FLORES codes IndicTrans2 wants ("mar_Deva") are mapped here.

Run it in its OWN virtualenv — torch + transformers are large and must not
be installed into the chat backend's env. See indictrans2.config.js and
requirements-indictrans2.txt.

Env (read from the process environment, then the project .env):
    HF_TOKEN             Hugging Face read token — needed once, to download the gated models
    IT2_EN_INDIC_MODEL   default ai4bharat/indictrans2-en-indic-dist-200M
    IT2_INDIC_EN_MODEL   default ai4bharat/indictrans2-indic-en-dist-200M
    IT2_DEVICE           "cpu" | "cuda" (default: cuda if available)
    IT2_NUM_BEAMS        default 3   (5 = model-card quality, slower on CPU)
    IT2_MAX_LENGTH       default 256 tokens per sentence
    IT2_BATCH_SIZE       default 16 sentences per generate() call
    IT2_PRELOAD          default "true" — load both models at startup
    IT2_HOST / IT2_PORT  default 0.0.0.0 / 5013 (only used by `python indictrans2_server.py`)
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import nullcontext

from flask import Flask, jsonify, request

# Load the project .env (one level up from translation_service/) before
# anything touches Hugging Face, so HF_TOKEN and the IT2_* settings can live
# there instead of in the PM2 config. Values already set in the environment
# (e.g. PM2 env block) win over .env.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("indictrans2")

EN_INDIC_MODEL = os.getenv("IT2_EN_INDIC_MODEL", "ai4bharat/indictrans2-en-indic-dist-200M")
INDIC_EN_MODEL = os.getenv("IT2_INDIC_EN_MODEL", "ai4bharat/indictrans2-indic-en-dist-200M")
NUM_BEAMS = int(os.getenv("IT2_NUM_BEAMS", "3"))
MAX_LENGTH = int(os.getenv("IT2_MAX_LENGTH", "256"))
BATCH_SIZE = int(os.getenv("IT2_BATCH_SIZE", "16"))
PRELOAD = os.getenv("IT2_PRELOAD", "true").lower() in ("1", "true", "yes")

# ISO 639-1 (or 3-letter where none exists) -> (FLORES code, display name)
LANGS = {
    "en":  ("eng_Latn", "English"),
    "as":  ("asm_Beng", "Assamese"),
    "bn":  ("ben_Beng", "Bengali"),
    "brx": ("brx_Deva", "Bodo"),
    "doi": ("doi_Deva", "Dogri"),
    "gom": ("gom_Deva", "Konkani"),
    "gu":  ("guj_Gujr", "Gujarati"),
    "hi":  ("hin_Deva", "Hindi"),
    "kn":  ("kan_Knda", "Kannada"),
    "ks":  ("kas_Arab", "Kashmiri"),
    "mai": ("mai_Deva", "Maithili"),
    "ml":  ("mal_Mlym", "Malayalam"),
    "mni": ("mni_Beng", "Manipuri"),
    "mr":  ("mar_Deva", "Marathi"),
    "ne":  ("npi_Deva", "Nepali"),
    "or":  ("ory_Orya", "Odia"),
    "pa":  ("pan_Guru", "Punjabi"),
    "sa":  ("san_Deva", "Sanskrit"),
    "sat": ("sat_Olck", "Santali"),
    "sd":  ("snd_Arab", "Sindhi"),
    "ta":  ("tam_Taml", "Tamil"),
    "te":  ("tel_Telu", "Telugu"),
    "ur":  ("urd_Arab", "Urdu"),
}

_SENT_SPLIT = re.compile(r"(?<=[.!?।॥])\s+")

app = Flask(__name__)

_models: dict = {}
_load_lock = threading.Lock()
_gen_locks = {"en-indic": threading.Lock(), "indic-en": threading.Lock()}
_processor = None


def _device():
    import torch
    want = os.getenv("IT2_DEVICE", "").strip().lower()
    if want:
        return want
    return "cuda" if torch.cuda.is_available() else "cpu"


def _get_processor():
    global _processor
    if _processor is None:
        try:
            from IndicTransToolkit.processor import IndicProcessor
        except ImportError:  # older IndicTransToolkit releases
            from IndicTransToolkit import IndicProcessor
        _processor = IndicProcessor(inference=True)
    return _processor


def _get_model(direction: str):
    if direction in _models:
        return _models[direction]
    with _load_lock:
        if direction in _models:
            return _models[direction]
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        name = EN_INDIC_MODEL if direction == "en-indic" else INDIC_EN_MODEL
        device = _device()
        t0 = time.time()
        log.info("loading %s on %s ...", name, device)
        tok = AutoTokenizer.from_pretrained(name, trust_remote_code=True)
        kwargs = {"trust_remote_code": True}
        if device.startswith("cuda"):
            kwargs["torch_dtype"] = torch.float16
        model = AutoModelForSeq2SeqLM.from_pretrained(name, **kwargs).to(device)
        model.eval()
        _models[direction] = (tok, model, device)
        log.info("loaded %s in %.1fs", name, time.time() - t0)
        return _models[direction]


def _translate_sentences(sentences, src_iso: str, tgt_iso: str):
    import torch

    direction = "en-indic" if src_iso == "en" else "indic-en"
    tok, model, device = _get_model(direction)
    ip = _get_processor()
    src, tgt = LANGS[src_iso][0], LANGS[tgt_iso][0]

    out = []
    for i in range(0, len(sentences), BATCH_SIZE):
        chunk = sentences[i:i + BATCH_SIZE]
        batch = ip.preprocess_batch(chunk, src_lang=src, tgt_lang=tgt)
        inputs = tok(batch, truncation=True, padding="longest",
                     return_tensors="pt", return_attention_mask=True).to(device)
        with _gen_locks[direction], torch.inference_mode():
            gen = model.generate(**inputs, use_cache=True, min_length=0,
                                 max_length=MAX_LENGTH, num_beams=NUM_BEAMS,
                                 num_return_sequences=1)
        # Older tokenizer builds decode with the source vocab unless switched.
        ctx = tok.as_target_tokenizer() if hasattr(tok, "as_target_tokenizer") else nullcontext()
        with ctx:
            decoded = tok.batch_decode(gen.detach().cpu().tolist(), skip_special_tokens=True,
                                       clean_up_tokenization_spaces=True)
        out.extend(ip.postprocess_batch(decoded, lang=tgt))
    return out


def translate_items(items, src_iso: str, tgt_iso: str):
    """Translate each item; multi-sentence items are split, translated, rejoined."""
    flat, owners = [], []
    for idx, text in enumerate(items):
        for sent in _SENT_SPLIT.split((text or "").strip()):
            if sent.strip():
                flat.append(sent.strip())
                owners.append(idx)
    results = [[] for _ in items]
    if flat:
        for owner, t in zip(owners, _translate_sentences(flat, src_iso, tgt_iso)):
            results[owner].append(t)
    return [" ".join(r) if r else (items[i] or "") for i, r in enumerate(results)]


@app.get("/health")
def health():
    return jsonify({"status": "ok", "loaded": sorted(_models)})


@app.get("/languages")
def languages():
    return jsonify([{"code": c, "name": n, "targets": ["en"] if c != "en" else sorted(LANGS)}
                    for c, (_f, n) in LANGS.items()])


@app.post("/translate")
def translate():
    body = request.get_json(silent=True) or {}
    q, src, tgt = body.get("q"), str(body.get("source") or ""), str(body.get("target") or "")
    if src not in LANGS or tgt not in LANGS:
        return jsonify({"error": f"unsupported language pair {src!r}->{tgt!r}"}), 400
    if (src == "en") == (tgt == "en"):
        return jsonify({"error": "only English <-> Indic is supported"}), 400
    single = isinstance(q, str)
    items = [q] if single else q
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        return jsonify({"error": "q must be a string or a list of strings"}), 400

    t0 = time.time()
    try:
        out = translate_items(items, src, tgt)
    except Exception as e:
        log.exception("translate failed")
        return jsonify({"error": str(e)}), 500
    log.info("%s->%s | %d item(s) | %.0fms", src, tgt, len(items), (time.time() - t0) * 1000)
    return jsonify({"translatedText": out[0] if single else out})


if PRELOAD and os.getenv("IT2_SKIP_PRELOAD_FOR_TESTS") != "1":
    _get_model("indic-en")
    _get_model("en-indic")
    _get_processor()


if __name__ == "__main__":
    app.run(host=os.getenv("IT2_HOST", "0.0.0.0"), port=int(os.getenv("IT2_PORT", "5013")), threaded=True)