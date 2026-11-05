"""
replay_compare.py - send the same chat conversations to two MiraQ backends
(OLD = before these changes, NEW = after) and report every difference.

Each conversation runs in its own fresh session on each backend. Per turn it
compares: intent, flow state, product ids (in order), total results, actions
(type, product, variation, quantity), suggestions and the bot text.

Differences on the turns we changed on purpose are labelled EXPECTED:
  reorder          (WooCommerce: items go to the cart instead of an order)
  update_customer  (My Account links instead of changing the account)
Everything else should be SAME. Any other DIFF is a regression to look at.

Usage
    python replay_compare.py conversations.txt \
        --old https://old-backend.example.com --new https://new-backend.example.com \
        --license <license id> [--identity <X-MiraQ-Identity token>] [--out report.json]

conversations.txt: one message per line; a blank line starts a new
conversation; lines starting with # are comments. Pass --identity (a signed-in
customer's widget token) for reorder / address turns, otherwise they run as a
guest.

Both backends must point at the SAME store, and the store must not change
between the two runs (run them back to back).
"""

import argparse
import json
import re
import sys

import requests

EXPECTED_CHANGE_INTENTS = {"reorder", "update_customer"}


def load_conversations(path):
    convs, cur = [], []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("#"):
                continue
            if not line:
                if cur:
                    convs.append(cur)
                    cur = []
                continue
            cur.append(line)
    if cur:
        convs.append(cur)
    return convs


def _norm_text(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def summarise(resp):
    """The parts of a /chat response that define behaviour."""
    products = resp.get("products") or []
    actions = []
    for a in resp.get("actions") or []:
        p = a.get("payload") or {}
        actions.append({k: p.get(k) for k in ("product_id", "variation_id", "variant_id", "quantity", "variation")
                        if p.get(k) is not None} | {"type": a.get("type")})
    return {
        "intent": resp.get("intent"),
        "flow_state": resp.get("flow_state"),
        "product_ids": [p.get("id") for p in products if isinstance(p, dict)],
        "total": (resp.get("pagination") or {}).get("total_items"),
        "orders": [o.get("id") for o in (resp.get("orders") or []) if isinstance(o, dict)],
        "actions": actions,
        "suggestions": resp.get("suggestions") or [],
        "text": _norm_text(resp.get("bot_message")),
    }


def run(base, path, headers, messages, platform):
    out, session_id, flow_state = [], None, None
    for msg in messages:
        body = {"message": msg, "session_id": session_id, "page": 1, "platform": platform,
                "user_context": {"flow_state": flow_state} if flow_state else {}}
        try:
            r = requests.post(base.rstrip("/") + path, json=body, headers=headers, timeout=60)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code >= 400 and not data:
                data = {"intent": f"HTTP {r.status_code}", "bot_message": r.text[:200]}
        except Exception as exc:  # network / JSON error: record it as the turn's result
            data = {"intent": "ERROR", "bot_message": str(exc)}
        session_id = data.get("session_id") or session_id
        flow_state = data.get("flow_state") or flow_state
        out.append(summarise(data))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("conversations")
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--license", required=True)
    ap.add_argument("--identity", default="")
    ap.add_argument("--platform", default="woocommerce")
    ap.add_argument("--path", default="/chat")
    ap.add_argument("--out", default="replay_report.json")
    args = ap.parse_args()

    headers = {"Content-Type": "application/json", "X-MiraQ-License-Id": args.license}
    if args.identity:
        headers["X-MiraQ-Identity"] = args.identity

    report, counts = [], {"SAME": 0, "EXPECTED": 0, "DIFF": 0}
    for n, conv in enumerate(load_conversations(args.conversations), 1):
        old = run(args.old, args.path, headers, conv, args.platform)
        new = run(args.new, args.path, headers, conv, args.platform)
        for i, msg in enumerate(conv):
            o, w = old[i], new[i]
            changed = sorted(k for k in o if o[k] != w[k])
            if not changed:
                verdict = "SAME"
            elif {o["intent"], w["intent"]} & EXPECTED_CHANGE_INTENTS:
                verdict = "EXPECTED"
            else:
                verdict = "DIFF"
            counts[verdict] += 1
            report.append({"conversation": n, "turn": i + 1, "message": msg, "verdict": verdict,
                           "changed": changed, "old": o, "new": w})
            if verdict != "SAME":
                print(f"[{verdict}] conv {n} turn {i + 1}: {msg!r} -> changed {', '.join(changed)}")
                for k in changed:
                    print(f"    {k}:\n      old: {o[k]!r}\n      new: {w[k]!r}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nSAME {counts['SAME']} | EXPECTED {counts['EXPECTED']} | DIFF {counts['DIFF']} -> {args.out}")
    sys.exit(1 if counts["DIFF"] else 0)


if __name__ == "__main__":
    main()