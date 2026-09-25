"""
translation/ — per-tenant chat translation behind a swappable provider.

    base.py            TranslationProvider contract, circuit breaker, HTTP plumbing
    providers/         one module per backend (libretranslate, indictrans2)
    registry.py        provider name -> shared instance; add new backends here
    settings.py        tenants.features["translation"] schema + validation
    script_detect.py   offline script-based detection (Marathi vs Hindi)
    text.py            markdown-safe reply translation, placeholder protection, cache
    turn.py            prepare_inbound() + @translate_reply, used by routes/chat.py

Kept import-light on purpose: importing the package must not pull in Flask
request state or the providers.
"""
