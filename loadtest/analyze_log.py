#!/usr/bin/env python3
"""
loadtest/analyze_logs.py — Post-run analysis of the MiraQ server log.

The locust client tells you THAT requests were slow. This tells you WHY, by
counting how often each expensive path fired.

Usage:
    python3 analyze_logs.py /path/to/server.log

    # compare a run before and after a code change
    python3 analyze_logs.py before.log --baseline after.log

The single most important number here is the LLM fallback rate. Removing
Phase 3 semantic auto-materialize means fewer queries resolve into concrete
filters, so more of them can fall through to Step 1.5 — which has a 10s
timeout plus one retry. If that rate climbed, your p99 will follow it.
"""

import argparse
import re
import sys
from collections import Counter

# Log markers, taken from the actual logger.info/warning call sites.
PATTERNS = {
    "requests_total": re.compile(r"POST /chat\b"),

    # Step 1.5 — the expensive one. 10s timeout + 1 retry.
    "llm_fallback_triggered": re.compile(r"Step 1\.5: LLM fallback triggered"),
    "llm_fallback_failed": re.compile(r"Step 1\.5: LLM fallback failed"),

    # Step 3.8 — second LLM call, fires on empty search results.
    "llm_retry_triggered": re.compile(r"Step 3\.8: LLM retry triggered"),
    "llm_retry_failed": re.compile(r"Step 3\.8: LLM retry failed"),

    # Fuzzy layer — this is what now carries the load Phase 3 used to share.
    "typo_corrections": re.compile(r"\[TypoFix\] applied (\d+) correction"),
    "typo_ambiguities": re.compile(r"\[TypoFix\] (\d+) ambiguous catalog tie"),

    # Should be ZERO after the Phase 3 removal. Any hit means a stale
    # process is still running the old code.
    "semantic_search_stale": re.compile(r"\[SemanticSearch\]"),

    # Infrastructure stress signals.
    "db_pool_timeout": re.compile(r"QueuePool limit|TimeoutError.*connection"),
    "woo_timeout": re.compile(r"ReadTimeout|ConnectTimeout|Max retries exceeded"),
    "daily_limit": re.compile(r"DAILY_LIMIT_REACHED"),
    "degraded": re.compile(r"DEGRADED|degraded"),
}

RESPONSE_TIME_RE = re.compile(r"response_time_ms['\"]?\s*[:=]\s*(\d+)")


def scan(path):
    counts = Counter()
    response_times = []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                for key, rx in PATTERNS.items():
                    if rx.search(line):
                        counts[key] += 1
                m = RESPONSE_TIME_RE.search(line)
                if m:
                    response_times.append(int(m.group(1)))
    except FileNotFoundError:
        print(f"Log file not found: {path}", file=sys.stderr)
        sys.exit(1)

    return counts, response_times


def pct(values, p):
    if not values:
        return 0
    s = sorted(values)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def report(path, counts, times, label="RUN"):
    total = counts["requests_total"] or len(times) or 1

    print("=" * 62)
    print(f"{label}: {path}")
    print("=" * 62)
    print(f"  chat requests seen        : {counts['requests_total']}")

    if times:
        print(f"  server-side p50 / p95 / p99: "
              f"{pct(times, .50)} / {pct(times, .95)} / {pct(times, .99)} ms")
        print(f"  slowest logged            : {max(times)} ms")

    print("\n  -- LLM path (the expensive one) --")
    llm1 = counts["llm_fallback_triggered"]
    llm2 = counts["llm_retry_triggered"]
    print(f"  Step 1.5 fallback fired   : {llm1}  ({llm1 / total:.1%} of requests)")
    print(f"  Step 1.5 failed/timed out : {counts['llm_fallback_failed']}")
    print(f"  Step 3.8 retry fired      : {llm2}  ({llm2 / total:.1%} of requests)")
    print(f"  Step 3.8 failed/timed out : {counts['llm_retry_failed']}")

    print("\n  -- Fuzzy correction layer --")
    print(f"  typo corrections applied  : {counts['typo_corrections']}")
    print(f"  typo ambiguity prompts    : {counts['typo_ambiguities']}")

    print("\n  -- Infrastructure --")
    print(f"  DB pool exhaustion        : {counts['db_pool_timeout']}")
    print(f"  Woo/HTTP timeouts         : {counts['woo_timeout']}")
    print(f"  429 daily limit           : {counts['daily_limit']}")
    print(f"  loader degraded events    : {counts['degraded']}")

    stale = counts["semantic_search_stale"]
    if stale:
        print(f"\n  !! [SemanticSearch] lines found: {stale}")
        print("     Phase 3 was removed — a stale process is running old code.")
        print("     Restart the server and re-run, the numbers are not valid.")

    print()
    return {"llm1_rate": llm1 / total, "llm2_rate": llm2 / total,
            "p99": pct(times, .99) if times else 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile")
    ap.add_argument("--baseline", help="a second log to compare against")
    args = ap.parse_args()

    counts, times = scan(args.logfile)
    cur = report(args.logfile, counts, times, label="RUN")

    if args.baseline:
        b_counts, b_times = scan(args.baseline)
        base = report(args.baseline, b_counts, b_times, label="BASELINE")

        print("=" * 62)
        print("COMPARISON")
        print("=" * 62)
        d1 = (cur["llm1_rate"] - base["llm1_rate"]) * 100
        d2 = (cur["llm2_rate"] - base["llm2_rate"]) * 100
        dp = cur["p99"] - base["p99"]
        print(f"  Step 1.5 rate change : {d1:+.1f} percentage points")
        print(f"  Step 3.8 rate change : {d2:+.1f} percentage points")
        print(f"  p99 change           : {dp:+d} ms")
        if d1 > 5:
            print("\n  Step 1.5 rate climbed materially. Expected direction after")
            print("  removing Phase 3 auto-materialize — queries that used to")
            print("  resolve via embeddings now reach the LLM instead. Worth")
            print("  checking WHICH queries, they may be recoverable as catalog")
            print("  synonyms rather than LLM calls.")
        print("=" * 62)


if __name__ == "__main__":
    main()