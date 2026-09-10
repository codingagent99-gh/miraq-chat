"""
loadtest/locustfile.py — Concurrency test for the MiraQ single-store /chat endpoint.

Run:
    locust -f locustfile.py --host http://localhost:5000

Then in the web UI set: users=20, spawn rate=20 (all at once, not ramped).

Headless equivalent:
    locust -f locustfile.py --host http://localhost:5000 \
           --users 20 --spawn-rate 20 --run-time 3m --headless \
           --csv results/run1

READ loadtest/README.md FIRST — there is a hard 25-request/day store-level
cap that will 429 this test within seconds unless you disable it.
"""

import json
import random
import uuid

from locust import HttpUser, task, between, events

# ──────────────────────────────────────────────────────────────────────────
# Query mix
#
# Weighted to exercise DIFFERENT code paths, not one hot path. The point is
# to see which phase dominates latency, so hammering a single identical
# query (which would sit in whatever caches exist) tells you nothing.
#
# Replace these with real terms from YOUR catalog before running — generic
# words will fall through Phase 1 into the LLM fallback and skew the whole
# run toward the 10s-timeout path.
# ──────────────────────────────────────────────────────────────────────────

CATALOG_QUERIES = [
    # Phase 1 exact catalog hits — cheap, regex only. Should be your floor.
    "show me matte tiles",
    "white marble",
    "12x24 tiles",
    "porcelain floor tile",
    "mosaic backsplash",
]

FILTER_QUERIES = [
    # Multi-entity: attribute + category + price. Exercises consolidation,
    # OR-pair building, and the DB/Woo query path.
    "matte white tiles under $10",
    "quick ship porcelain 12x24",
    "gray mosaic tiles in stock",
    "large format tiles on sale",
]

TYPO_QUERIES = [
    # Hits utils/typo_correction.py (rapidfuzz Damerau-Levenshtein).
    # This is the path that now covers what Phase 3 embeddings used to.
    "marbel tiles",
    "porcelian floor",
    "mozaic backsplash",
]

VAGUE_QUERIES = [
    # Low-signal free text. These are the ones most likely to fall through
    # to the Step 1.5 LLM fallback (10s timeout + 1 retry) now that Phase 3
    # semantic auto-materialize is gone. Deliberately kept to a small share
    # of the mix — if this weight is too high you are load-testing your LLM
    # provider, not your Flask app.
    "something modern for a bathroom",
    "nice looking floor",
]

BROWSE_QUERIES = [
    # Catalog browse — DB-heavy, little parsing.
    "what categories do you have",
    "browse products",
    "show me best sellers",
]


class MiraQChatUser(HttpUser):
    """One simulated widget session."""

    # Real users read the reply before typing again. Zero wait time would
    # measure a hammering pattern no real traffic produces, and would
    # inflate the concurrency far past the 20 you asked for.
    wait_time = between(2, 6)

    def on_start(self):
        # Each simulated user gets its own session UUID, so they map to
        # distinct Conversation rows — sharing one would serialize them on
        # the same row and hide real concurrency.
        self.session_id = str(uuid.uuid4())

    def _post(self, message: str, name: str):
        payload = {
            "message": message,
            "page": 1,
            # Omit "platform" entirely — sending the wrong one is a hard 400.
            # Absent field is treated as an older widget and allowed through.
            "user_context": {"flow_state": "idle"},
        }
        headers = {
            "Content-Type": "application/json",
            "X-MiraQ-Session": self.session_id,
        }

        with self.client.post(
            "/chat",
            data=json.dumps(payload),
            headers=headers,
            name=name,                # group by query TYPE in the stats table
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                resp.failure("DAILY_LIMIT_REACHED — see README, test is invalid")
                return
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            try:
                body = resp.json()
            except Exception:
                resp.failure("non-JSON response")
                return

            if not body.get("success", False):
                # A well-formed "I couldn't find that" is NOT a failure — it
                # is a legitimate outcome we still want timed. Only flag
                # explicit error intents.
                if body.get("intent") == "error":
                    resp.failure(f"error intent: {body.get('bot_message', '')[:80]}")
                    return

            resp.success()

    # Weights approximate a real shopper mix: mostly concrete product
    # queries, a minority of typos, a small tail of vague text.
    @task(10)
    def catalog_query(self):
        self._post(random.choice(CATALOG_QUERIES), name="1-catalog-exact")

    @task(8)
    def filter_query(self):
        self._post(random.choice(FILTER_QUERIES), name="2-filter-multi")

    @task(4)
    def typo_query(self):
        self._post(random.choice(TYPO_QUERIES), name="3-typo-fuzzy")

    @task(3)
    def browse_query(self):
        self._post(random.choice(BROWSE_QUERIES), name="4-browse")

    @task(2)
    def vague_query(self):
        self._post(random.choice(VAGUE_QUERIES), name="5-vague-llm-path")


@events.quitting.add_listener
def _print_verdict(environment, **kwargs):
    """Print pass/fail against explicit thresholds rather than raw numbers."""
    stats = environment.stats.total
    if stats.num_requests == 0:
        print("\nNo requests completed — check the host and that the server is up.")
        return

    fail_ratio = stats.fail_ratio
    p95 = stats.get_response_time_percentile(0.95)
    p99 = stats.get_response_time_percentile(0.99)

    print("\n" + "=" * 62)
    print("VERDICT")
    print("=" * 62)
    print(f"  requests      : {stats.num_requests}")
    print(f"  failures      : {stats.num_failures} ({fail_ratio:.1%})")
    print(f"  median        : {stats.median_response_time} ms")
    print(f"  p95           : {p95} ms")
    print(f"  p99           : {p99} ms")
    print(f"  throughput    : {stats.total_rps:.2f} req/s")
    print("-" * 62)

    problems = []
    if fail_ratio > 0.01:
        problems.append(f"failure rate {fail_ratio:.1%} exceeds 1%")
    if p95 > 3000:
        problems.append(f"p95 {p95}ms exceeds 3000ms")
    if p99 > 10000:
        problems.append(
            f"p99 {p99}ms exceeds 10000ms — at/above LLM_TIMEOUT_SECONDS, "
            "suggests requests parked in the LLM fallback"
        )

    if problems:
        print("  NOT CLEAN at this concurrency:")
        for p in problems:
            print(f"    - {p}")
    else:
        print("  Thresholds met at this concurrency.")
    print("=" * 62)
    print("Now run analyze_logs.py against the server log — client-side")
    print("numbers cannot tell you WHY, only that something was slow.")
    print("=" * 62 + "\n")