"""
timing_logger.py - Request timing hooks for miraq-chat.

Uses chat_logger.get_timing_logger() -- same daily-rotating-file pattern as
the existing chat/api/order loggers -- so timing lines land in their own
file without any new logging machinery:

    logs/<date>/timing.txt

Two lines per request:

    [2026-09-09 09:50:00.127] START | req=a1b2c3d4 | caller=127.0.0.1 | session=fcd7e7f3 | message="12x24 tiles"
    [2026-09-09 09:50:04.302] DONE  | req=a1b2c3d4 | caller=127.0.0.1 | session=fcd7e7f3 | message="12x24 tiles" | duration=4.17s | status=200

req is a short id, unique per request. Needed because caller + session alone
can't tell apart two concurrent identical messages from the SAME user/session
-- req always can, so it's the field to grep on when matching a START to its
DONE.

caller = request.remote_addr (the actual TCP peer). session = the
X-MiraQ-Session header if the client sends one (the load test does), else
session_id from the JSON body, else blank.

Off by default; turn on with:

    TIMING_LOG_ENABLED=true
"""

import os
import time
import uuid
from contextlib import contextmanager

from flask import g, has_request_context, request

from chat_logger import get_timing_logger

ENABLED = os.getenv("TIMING_LOG_ENABLED", "false").strip().lower() in ("true", "1", "yes")

# Set by _instrument_sqlalchemy so _pool_stats can read pool occupancy.
_ENGINE = None


# ── Stage accounting ─────────────────────────────────────────────────────────
# Each stage() block adds its elapsed time to a per-request bucket. The DONE
# line then reports the breakdown, so a slow request says WHY it was slow
# instead of just how long it took.
#
# Buckets are additive across repeated calls: a request making 3 Woo calls
# gets one woo= total, not three entries. "other" on the DONE line is
# whatever wasn't inside any instrumented stage.


def accumulate(bucket: str, seconds: float):
    """Add elapsed time to a named bucket for the current request."""
    if not ENABLED or not has_request_context():
        return
    try:
        buckets = g._t_buckets
    except AttributeError:
        buckets = g._t_buckets = {}
    buckets[bucket] = buckets.get(bucket, 0.0) + seconds


@contextmanager
def stage(name: str):
    """
    Time a block and attribute it to a bucket:

        with timing_logger.stage("classify"):
            result = parse_csv_message(...)

    Safe to use outside a request context (no-ops) and safe if the block
    raises -- the elapsed time is still recorded.
    """
    if not ENABLED:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        accumulate(name, time.perf_counter() - t0)


def _format_buckets():
    """Render the per-stage breakdown for the DONE line, slowest first."""
    buckets = dict(getattr(g, "_t_buckets", {}) or {})
    t0 = getattr(g, "_t0", None)
    if not buckets and t0 is None:
        return ""
    total = (time.perf_counter() - t0) if t0 is not None else 0.0
    accounted = sum(buckets.values())
    # Whatever wasn't inside an instrumented stage. If this dominates, the
    # bottleneck is in code that isn't wrapped yet -- which is itself the
    # useful signal.
    other = max(total - accounted, 0.0)
    parts = sorted(buckets.items(), key=lambda kv: -kv[1])
    rendered = " ".join(f"{k}={v:.2f}s" for k, v in parts if v >= 0.005)
    return f" | {rendered} other={other:.2f}s" if rendered else f" | other={other:.2f}s"


def _caller_and_session(body):
    caller = request.remote_addr or ""
    session = (
        request.headers.get("X-MiraQ-Session")
        or (body.get("session_id") if isinstance(body, dict) else None)
        or ""
    )
    return caller, session


def start_request():
    """Call from Flask before_request."""
    if not ENABLED:
        return
    g._t0 = time.perf_counter()
    g._t_req_id = uuid.uuid4().hex[:8]

    message = ""
    try:
        body = request.get_json(silent=True) or {}
        message = str(body.get("message", ""))[:120].replace("\n", " ")
    except Exception:
        body = {}
    g._t_message = message

    caller, session = _caller_and_session(body if isinstance(body, dict) else {})
    g._t_caller = caller
    g._t_session = session

    get_timing_logger().info(
        f'START | req={g._t_req_id} | caller={caller} | session={session} | message="{message}"'
    )


def end_request(response):
    """Call from Flask after_request. Returns the response unchanged."""
    if not ENABLED:
        return response

    # Guard against a double log line: if a handler raises, Flask's error
    # handler still returns a response, so after_request AND
    # teardown_request both fire for the same request.
    if getattr(g, "_t_logged", False):
        return response
    g._t_logged = True

    t0 = getattr(g, "_t0", None)
    duration = f"{time.perf_counter() - t0:.2f}s" if t0 is not None else "?"

    get_timing_logger().info(
        f'DONE  | req={getattr(g, "_t_req_id", "")} | caller={getattr(g, "_t_caller", "")} | '
        f'session={getattr(g, "_t_session", "")} | message="{getattr(g, "_t_message", "")}" | '
        f'duration={duration} | status={response.status_code}{_format_buckets()}{_pool_stats()}'
    )
    return response


def teardown(exc=None):
    """Flask teardown_request safety net, for when a handler raises."""
    if not ENABLED or exc is None:
        return
    if getattr(g, "_t_logged", False):
        return
    g._t_logged = True

    t0 = getattr(g, "_t0", None)
    duration = f"{time.perf_counter() - t0:.2f}s" if t0 is not None else "?"

    get_timing_logger().info(
        f'DONE  | req={getattr(g, "_t_req_id", "")} | caller={getattr(g, "_t_caller", "")} | '
        f'session={getattr(g, "_t_session", "")} | message="{getattr(g, "_t_message", "")}" | '
        f'duration={duration} | status=EXCEPTION{_format_buckets()}{_pool_stats()}'
    )


def _instrument_sqlalchemy(db):
    """
    Measure DB query time and record pool saturation, via SQLAlchemy engine
    events rather than by editing query code -- so every query in the app is
    covered without touching any of them.

    db_query = time actually spent executing statements.

    Pool wait is NOT measured directly: SQLAlchemy has no "about to wait for
    a connection" event to bracket, only "checkout" which fires after the
    wait is over. Instead the DONE line reports pool saturation at response
    time (see _pool_stats) -- if checkedout is pinned at the pool limit while
    requests are slow, contention for connections is the story.
    """
    try:
        from sqlalchemy import event
    except Exception:
        return None

    engine = db.engine

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._t_query_start = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        t0 = getattr(context, "_t_query_start", None)
        if t0 is not None:
            accumulate("db_query", time.perf_counter() - t0)

    global _ENGINE
    _ENGINE = engine
    return engine


def _pool_stats():
    """Pool occupancy at response time, e.g. ' | pool=3/20+30'."""
    if _ENGINE is None:
        return ""
    try:
        pool = _ENGINE.pool
        checked_out = pool.checkedout()
        size = pool.size()
        overflow = pool._max_overflow
        return f" | pool={checked_out}/{size}+{overflow}"
    except Exception:
        return ""


def init_app(app, db=None):
    """
    Attach the hooks. No-op unless TIMING_LOG_ENABLED is set.

    Pass `db` (the Flask-SQLAlchemy object) to also get db_query timings.
    """
    if not ENABLED:
        # Always print the status, not just when on -- a silent no-op here
        # is exactly what makes "why is nothing logging" hard to diagnose.
        print("[timing_logger] DISABLED (set TIMING_LOG_ENABLED=true before starting the server to turn on)")
        return
    app.before_request(start_request)
    app.after_request(end_request)
    app.teardown_request(teardown)

    if db is not None:
        try:
            with app.app_context():
                _instrument_sqlalchemy(db)
            print("[timing_logger] SQLAlchemy query timing attached")
        except Exception as exc:
            print(f"[timing_logger] WARNING: could not attach DB timing: {exc}")

    print("[timing_logger] ENABLED -> logs/<date>/timing.txt")