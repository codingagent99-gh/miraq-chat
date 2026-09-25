"""
routes/translation_admin.py — view and change a tenant's translation settings.

    GET    /admin/translation/providers                       providers, their languages, reachability
    GET    /admin/translation/tenants/<tenant>                current settings
    PUT    /admin/translation/tenants/<tenant>                replace settings (missing fields -> defaults)
    PATCH  /admin/translation/tenants/<tenant>                change only the fields sent
    DELETE /admin/translation/tenants/<tenant>                remove settings (tenant back to English-only)
    POST   /admin/translation/tenants/<tenant>/test           {"text": "..."} dry-run with that tenant's settings

<tenant> is the licence id, or the tenant_id UUID.

Writes tenants.features["translation"] on the control-plane row. Takes effect
on the tenant's next chat message — the row is re-read on every request, and
nothing is cached, so no restart or cache refresh is needed.

The /admin/translation/ prefix is exempt from store_registry's tenant
resolution (see _EXEMPT_PREFIXES): the tenant is named in the URL, and
changing a language must not trigger a catalogue load for a cold tenant.

AUTH: none, by decision. These routes are open to anyone who can reach the
server. Restrict them at the proxy (nginx allow-list / internal-only
listener) before this is internet-facing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from chat_logger import get_logger
from models import db, Tenant
from translation.base import TranslationUnavailable
from translation.registry import get_provider, provider_names
from translation.settings import FEATURE_KEY, from_features, validate
from translation.text import translate_markdown, translate_texts
from translation.turn import pick_input_language

logger = get_logger("miraq_admin")

translation_admin_bp = Blueprint("translation_admin", __name__, url_prefix="/admin/translation")


def _find_tenant(key: str):
    key = (key or "").strip()
    if not key:
        return None
    tenant = Tenant.query.filter_by(license_id=key).first()
    if tenant is None:
        try:
            tenant = Tenant.query.get(uuid.UUID(key))
        except ValueError:
            tenant = None
    return tenant


def _supported_for(name: str) -> set:
    provider = get_provider(name)
    return provider.supported_languages() if provider else set()


def _view(tenant) -> dict:
    raw = (tenant.features or {}).get(FEATURE_KEY)
    settings = from_features(tenant.features)
    return {
        "success": True,
        "license_id": tenant.license_id,
        "tenant_id": str(tenant.tenant_id),
        "translation": settings.to_dict(),
        "active": settings.active,
        "stored": raw,  # exactly what is on the row (null = never configured)
    }


def _save(tenant, settings_dict) -> None:
    features = dict(tenant.features or {})
    if settings_dict is None:
        features.pop(FEATURE_KEY, None)
    else:
        settings_dict = dict(settings_dict, updated_at=datetime.now(timezone.utc).isoformat())
        features[FEATURE_KEY] = settings_dict
    tenant.features = features  # reassign: plain JSONB has no in-place change tracking
    db.session.commit()


@translation_admin_bp.route("/providers", methods=["GET"])
def list_providers():
    out = []
    for name in provider_names():
        p = get_provider(name)
        out.append({**p.health(), "languages": sorted(p.supported_languages())})
    return jsonify({"success": True, "providers": out}), 200


@translation_admin_bp.route("/tenants/<tenant_key>", methods=["GET"])
def get_settings(tenant_key):
    tenant = _find_tenant(tenant_key)
    if tenant is None:
        return jsonify({"success": False, "error": "unknown tenant"}), 404
    return jsonify(_view(tenant)), 200


@translation_admin_bp.route("/tenants/<tenant_key>", methods=["PUT", "PATCH"])
def update_settings(tenant_key):
    tenant = _find_tenant(tenant_key)
    if tenant is None:
        return jsonify({"success": False, "error": "unknown tenant"}), 404

    payload = request.get_json(silent=True)
    base = from_features(tenant.features) if request.method == "PATCH" else None
    settings, errors = validate(
        payload, base=base, provider_names=provider_names(), supported_for=_supported_for,
    )
    if errors:
        return jsonify({"success": False, "errors": errors}), 400

    _save(tenant, settings.to_dict())
    logger.info(
        f"translation settings {request.method} | license_id={tenant.license_id!r} | "
        f"{settings.to_dict()}"
    )
    return jsonify(_view(tenant)), 200


@translation_admin_bp.route("/tenants/<tenant_key>", methods=["DELETE"])
def delete_settings(tenant_key):
    tenant = _find_tenant(tenant_key)
    if tenant is None:
        return jsonify({"success": False, "error": "unknown tenant"}), 404
    _save(tenant, None)
    logger.info(f"translation settings removed | license_id={tenant.license_id!r}")
    return jsonify(_view(tenant)), 200


@translation_admin_bp.route("/tenants/<tenant_key>/test", methods=["POST"])
def test_settings(tenant_key):
    """
    Dry run: detect + translate `text` the way a chat turn for this tenant
    would, and translate `reply` (default: a sample bot line) back. Nothing is
    stored. Use it to check the provider is up and the language is right.
    """
    tenant = _find_tenant(tenant_key)
    if tenant is None:
        return jsonify({"success": False, "error": "unknown tenant"}), 404
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    reply = str(body.get("reply") or "I found **3 products** matching your search. Would you like to see more?")
    if not text:
        return jsonify({"success": False, "error": "send {\"text\": \"...\"}"}), 400

    settings = from_features(tenant.features)
    if not settings.active:
        return jsonify({"success": False, "error": "translation is not enabled for this tenant",
                        "translation": settings.to_dict()}), 400
    provider = get_provider(settings.provider)

    detections = [{"language": d.language, "confidence": round(d.confidence, 3)}
                  for d in provider.detect(text)]
    lang = pick_input_language(text, settings, provider, sticky=None)
    result = {"success": True, "provider": settings.provider, "detections": detections,
              "input_lang": lang or None}
    try:
        if lang and lang != "en":
            result["to_english"] = translate_texts(provider, [text], lang, "en")[0]
            result["reply_translated"] = translate_markdown(provider, reply, "en", lang)
        result["reply_english"] = reply
    except TranslationUnavailable as e:
        result.update(success=False, error=f"provider unavailable: {e}")
        return jsonify(result), 502
    return jsonify(result), 200
