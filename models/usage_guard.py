# models/usage_guard.py

from functools import wraps
from flask import g
from models.chat_usage import ChatUsage

DAILY_FREE_LIMIT = 50

def enforce_daily_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Single source of truth: Tenant.plan (control-plane DB). The old
        # CustomerPlan check (per-tenant-DB, "no billing flow yet" per its
        # own docstring) predates multi-tenancy and was never kept in sync
        # with Tenant.plan/license_expires_at — a tenant's premium status
        # could disagree between the two with nothing reconciling them.
        # g.tenant is set by store_registry._resolve_tenant() before this
        # view runs (this route is not in _EXEMPT_PATHS).
        tenant = g.__dict__.get("tenant")
        if tenant and tenant.plan != "free":
            return f(*args, **kwargs)

        new_count, exceeded = ChatUsage.increment_and_check(limit=DAILY_FREE_LIMIT)
        if exceeded:
            ...  # unchanged
        return f(*args, **kwargs)
    return decorated