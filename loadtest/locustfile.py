"""
loadtest/locustfile.py — Product-search load test for the MiraQ /chat endpoint.


Run:
    locust -f locustfile.py --host http://localhost:5000

Headless:
    locust -f locustfile.py --host http://localhost:5000 \
           --users 20 --spawn-rate 20 --run-time 3m --headless \
           --csv results/run1

There is a hard 25-request/day store-level cap (@enforce_daily_limit on the
/chat route). It will 429 this test within seconds unless you disable it.
"""

import json
import random
import uuid
from collections import defaultdict

from locust import HttpUser, task, between, events

# ──────────────────────────────────────────────────────────────────────────
# Query pools — grouped by the code path each one exercises, not by topic.
#
# The point of the grouping is that the stats table tells you WHICH kind of
# search is slow. Lumping them together gives you one percentile and no idea
# which phase produced it.
# ──────────────────────────────────────────────────────────────────────────

PRODUCT_NAME = [
    # Guide 1.1 / 1.4. Resolved by product_by_name_lower in the loader, so
    # these should be the cheapest path in the run — no Woo round trip for
    # the name resolution itself.
    "Show me Adams",
    "Show me Ansel warm white",
]

CATEGORY = [
    # Guide 1.2 and 12. Category resolution off category_by_name_lower, then
    # a products-advanced-new call. "Show me Floor/Pool products" is the one
    # that exercises category_groups rather than a single slug, and
    # "give fabric" is bare and lowercase — no "show me" lead-in, which is a
    # different shape through Phase 1 than the rest.
    "Show me new releases",
    "Show me Exterior Tiles",
    "Show me Countertop products",
    "Show me panels products",
    "Show me Pavers",
    "Show me Floor/Pool products",
    "Show tile products",
    "Show floor tiles",
    "give fabric",
]

ATTRIBUTE = [
    # Guide 1.3 / 1.4 and 12. Category + tag, or category + attribute term.
    # These go through consolidation and attribute-filter building rather
    # than a plain category browse.
    "Show me tile products with minimalistic look",
    "Show me Blue color tiles",
    "Show me Blue color products",
    "Show me matte finish",
    "Show with colors blue",
    "Show me green mosaic products with matte finish",
    "Show me Minimalistic pavers",
    "Show wood pavers gray",
    "Show Porcelain pavers",
]

DIMENSION = [
    # Guide 12. Kept as their own bucket because the inch marks and fraction
    # sizes are the fragile part: routes/chat.py has an explicit carve-out
    # for translation corrupting dimension strings (12"X24" becoming
    # 12 "X24"), and catalog_parser has dimension-specific regex handling.
    # If one bucket regresses on its own, it will probably be this one.
    'Show me tile products with 12"x12" tile size',
    'Show wood pavers 1/2"',
    'Show wood pavers 3/8"',
    'Show me brown countertops size 60"x60"',
]

MULTI_FILTER = [
    # Guide 12. The heaviest shape: category + attribute + the quickship tag,
    # which is what builds OR-pairs and the widest filter payloads.
    #
    # "with quick quickship" below is verbatim from the guide, doubled word
    # included. It is listed there as a working query, so it is left exactly
    # as written — if you "fix" it you are no longer testing what the guide
    # documents.
    "Show paver products with bullnose with quickship",
    "Show all floor tile covebase with quickship",
    'Show paver products bullnose of 5/16" with quick quickship',
    "Show all paver products with anti-slip with quickship",
    "Give all floor mosaic with quickship",
    "Give me all wall tile with quickship",
    "Give me all tile wall mosaics",
]

# Guide 1.5. Exact string, matched case-insensitively in routes/chat.py before
# classification — a guaranteed reset, not a phrase the classifier happens to
# handle. Used below to recover the session, never counted as a search.
RESET_MESSAGE = "new search"

# Flow states a product search can legitimately leave behind. Anything else
# means the server is now waiting on a chip answer, and the NEXT message would
# be consumed as that answer instead of being run as a search.
CONTINUABLE_STATES = {"idle", "showing_results", "awaiting_anything_else"}

# name -> [total, empty] so the verdict can report which queries never
# returned products. A query that always comes back empty is not a failure,
# but it also is not testing the Woo/DB path, and that is worth knowing.
_RESULT_TALLY = defaultdict(lambda: [0, 0])


class MiraQSearchUser(HttpUser):
    """One simulated widget session doing product searches."""

    # Real users read the reply before typing again. Zero wait would measure a
    # hammering pattern no real traffic produces and would push the effective
    # concurrency well past the number of users you asked for.
    wait_time = between(2, 6)

    def on_start(self):
        # Own session UUID per simulated user, so they map to distinct
        # Conversation rows. Sharing one would serialize them on the same row
        # and hide the concurrency you are trying to measure.
        self.session_id = str(uuid.uuid4())

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _post(self, message: str, name: str) -> dict:
        """POST one message. Returns the parsed body, or {} on failure."""
        payload = {
            "message": message,
            "page": 1,
            # "platform" omitted on purpose — sending the wrong value is a hard
            # 400 (platform_mismatch). An absent field is treated as an older
            # widget and allowed through.
            "user_context": {},
        }
        headers = {
            "Content-Type": "application/json",
            "X-MiraQ-Session": self.session_id,
        }

        with self.client.post(
            "/chat",
            data=json.dumps(payload),
            headers=headers,
            name=name,
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                resp.failure("DAILY_LIMIT_REACHED — 25/day cap, test is invalid")
                return {}
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return {}
            try:
                body = resp.json()
            except Exception:
                resp.failure("non-JSON response")
                return {}

            if body.get("intent") == "error":
                resp.failure(f"error intent: {body.get('bot_message', '')[:80]}")
                return body

            resp.success()
            return body

    def _search(self, pool: list, name: str):
        """Run one product search, then make sure the session can run another.

        The reset afterwards is not cosmetic. The server reads flow state from
        the persisted Conversation row, NOT from the request's user_context —
        that field only controls a translation carve-out. So if a search ends
        in awaiting_filter_clarification or awaiting_refinement_choice, the
        next message is consumed as an answer to that chip and never reaches
        the search path at all. Without this, a long run quietly stops testing
        the thing it is named after.

        The reset is tagged separately, so it never lands in the search
        percentiles.
        """
        query = random.choice(pool)
        body = self._search_once(query, name)

        state = (body.get("flow_state") or "idle").lower()
        if state not in CONTINUABLE_STATES:
            self._post(RESET_MESSAGE, name="0-reset (not a search)")

    def _search_once(self, query: str, name: str) -> dict:
        body = self._post(query, name)
        tally = _RESULT_TALLY[name]
        tally[0] += 1
        if body and not body.get("products"):
            tally[1] += 1
        return body

    # ── Task mix ──────────────────────────────────────────────────────────
    #
    # Weighted toward category and attribute searches because that is what the
    # guide leads with and what shoppers actually type. Dimension and
    # multi-filter are kept present but smaller: they are the expensive shapes,
    # and over-weighting them would report a p95 no real traffic produces.

    @task(10)
    def category_search(self):
        self._search(CATEGORY, name="1-category")

    @task(8)
    def attribute_search(self):
        self._search(ATTRIBUTE, name="2-attribute")

    @task(5)
    def multi_filter_search(self):
        self._search(MULTI_FILTER, name="3-multi-filter-quickship")

    @task(4)
    def product_name_search(self):
        self._search(PRODUCT_NAME, name="4-product-name")

    @task(3)
    def dimension_search(self):
        self._search(DIMENSION, name="5-dimension")


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
    print("VERDICT — product search only")
    print("=" * 62)
    print(f"  requests      : {stats.num_requests}")
    print(f"  failures      : {stats.num_failures} ({fail_ratio:.1%})")
    print(f"  median        : {stats.median_response_time} ms")
    print(f"  p95           : {p95} ms")
    print(f"  p99           : {p99} ms")
    print(f"  throughput    : {stats.total_rps:.2f} req/s")
    print("-" * 62)

    # Empty-result audit. Not a pass/fail signal — a legitimately empty
    # catalog slice is a valid answer — but a bucket at 100% empty means those
    # queries never reached the Woo/DB path, so whatever latency they
    # contributed is not the latency you think you measured.
    print("  empty-result rate by bucket:")
    for name in sorted(_RESULT_TALLY):
        total, empty = _RESULT_TALLY[name]
        if total:
            flag = "  <-- never returned products" if empty == total else ""
            print(f"    {name:<28} {empty}/{total} empty{flag}")
    print("-" * 62)

    problems = []
    if fail_ratio > 0.01:
        problems.append(f"failure rate {fail_ratio:.1%} exceeds 1%")
    if p95 > 3000:
        problems.append(f"p95 {p95}ms exceeds 3000ms")
    if p99 > 10000:
        problems.append(
            f"p99 {p99}ms exceeds 10000ms — at/above LLM_TIMEOUT_SECONDS. "
            "Every query here is a documented working one, so this means "
            "Phase 1 missed and the request fell through to the LLM fallback, "
            "not that the query was unreasonable."
        )

    if problems:
        print("  NOT CLEAN at this concurrency:")
        for p in problems:
            print(f"    - {p}")
    else:
        print("  Thresholds met at this concurrency.")
    print("=" * 62)
    print("Now run analyze_log.py against the server log — client-side numbers")
    print("cannot tell you WHY, only that something was slow.")
    print("=" * 62 + "\n")