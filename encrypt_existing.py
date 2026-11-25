#!/usr/bin/env python3
"""
encrypt_existing.py - encrypt the shopper data already stored in every store's
database (or decrypt it back). One-time, after ENCRYPT_CUSTOMER_DATA=1.

    python encrypt_existing.py --dry-run              # count rows still plain
    python encrypt_existing.py --apply                # encrypt them
    python encrypt_existing.py --apply --shop tiles.myshopify.com
    python encrypt_existing.py --decrypt --dry-run    # count rows still encrypted
    python encrypt_existing.py --decrypt              # back to plain (turning off for good)
    python encrypt_existing.py --apply --vacuum       # then VACUUM FULL the touched tables

Safe while the app is running, and safe to run again:
  - Rows already in the target form are skipped (it reads the marker prefix).
  - Each row is updated only if it still holds the value that was read
    (compare-and-set). If a live chat changed it in between, the row is left
    for the next run and counted under "changed meanwhile".
  - Raw SQL, so updated_at is not touched (retention keeps counting from the
    shopper's last activity, not from today).
  - customer_visits and channel_links: if a shopper has two rows (one plain,
    one encrypted, from switching the setting), the newest is kept.

Before encrypting it checks CUSTOMER_DATA_KEY against the key the app has
used (control-plane table customer_data_encryption), and records it if this
is the first time, so the app will refuse to start without the key from now
on. It never encrypts with a different key than the app's.

Exit code 0 when nothing was left behind, 1 when some rows need another run.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text

import customer_crypto as cc
import customer_data_db as cdb

TEXT, ID, JSON = "text", "id", "json"


@dataclass
class Spec:
    table: str
    pk: str
    cols: List[Tuple[str, str]]
    unique: Optional[List[str]] = None     # columns of a unique key that includes a converted column
    newest: Optional[str] = None           # timestamp deciding which duplicate to keep


SPECS = [
    Spec("messages", "id", [("content", TEXT), ("metadata_json", JSON)]),
    Spec("conversations", "id", [("customer_id", ID), ("context_data", JSON)]),
    Spec("customer_visits", "customer_id", [("customer_id", ID)],
         unique=["customer_id"], newest="last_seen_at"),
    Spec("channel_links", "id", [("channel_user_id", ID), ("customer_id", ID), ("email_display", TEXT)],
         unique=["channel", "channel_user_id"], newest="linked_at"),
    Spec("channel_link_requests", "id", [("channel_user_id", ID)]),
    Spec("shopify_order_confirmations", "session_id", [("order_id", ID), ("order_number", TEXT)]),
]


# ── Which rows, and what they become ────────────────────────────────────────

def _needs(col: str, kind: str, decrypt: bool) -> str:
    if kind == JSON:
        enc = f"(jsonb_typeof({col}) = 'string' AND ({col} #>> '{{}}') LIKE 'enc1:%')"
        return enc if decrypt else (f"({col} IS NOT NULL AND jsonb_typeof({col}) <> 'null' "
                                    f"AND NOT {enc})")
    prefix = "enc1:" if kind == TEXT else "cid1:"
    if decrypt:
        return f"({col} LIKE '{prefix}%')"
    return f"({col} IS NOT NULL AND {col} <> '' AND {col} NOT LIKE '{prefix}%')"


def _where(spec: Spec, decrypt: bool) -> str:
    return "(" + " OR ".join(_needs(c, k, decrypt) for c, k in spec.cols) + ")"


def _convert(value, kind: str, decrypt: bool):
    if value is None:
        return None
    if decrypt:
        return {TEXT: cc.decrypt_text, ID: cc.decrypt_id, JSON: cc.decrypt_json}[kind](value)
    if kind == TEXT:
        return value if value == "" or value.startswith(cc.ENC_PREFIX) else cc.encrypt_text(value, force=True)
    if kind == ID:
        return value if value == "" or value.startswith(cc.ID_PREFIX) else cc.encrypt_id(value, force=True)
    if isinstance(value, str) and value.startswith(cc.ENC_PREFIX):
        return value
    return cc.encrypt_json(value, force=True)


# ── One table ───────────────────────────────────────────────────────────────

@dataclass
class Result:
    converted: int = 0
    merged: int = 0          # duplicate rows removed (kept the newest)
    changed_meanwhile: int = 0
    not_ours: int = 0        # looked encrypted but isn't (text a shopper typed)
    errors: List[str] = field(default_factory=list)


def _cas_update(conn, spec: Spec, pk, old: Dict, new: Dict) -> int:
    sets, conds, params = [], [f"{spec.pk} = :pk"], {"pk": pk}
    for i, (col, kind) in enumerate(spec.cols):
        if new[col] == old[col]:
            continue
        if kind == JSON:
            sets.append(f"{col} = CAST(:n{i} AS jsonb)")
            params[f"n{i}"] = json.dumps(new[col], ensure_ascii=False)
            conds.append(f"{col} = CAST(:o{i} AS jsonb)")
            params[f"o{i}"] = json.dumps(old[col], ensure_ascii=False)
        else:
            sets.append(f"{col} = :n{i}")
            params[f"n{i}"] = new[col]
            conds.append(f"{col} = :o{i}")
            params[f"o{i}"] = old[col]
    sql = f"UPDATE {spec.table} SET {', '.join(sets)} WHERE {' AND '.join(conds)}"
    return conn.execute(text(sql), params).rowcount


def _resolve_duplicate(conn, spec: Spec, pk, row: Dict, new: Dict) -> str:
    """Keep the newest of a shopper's two rows (one plain, one encrypted).

    'go': no twin, update this row.  'replaced': the twin was older and was
    deleted, update this row.  'dropped': this row was the older one and was
    deleted."""
    key_vals = {c: (new[c] if c in new else row[c]) for c in spec.unique}
    cond = " AND ".join(f"{c} = :k_{c}" for c in spec.unique)
    twin = conn.execute(
        text(f"SELECT {spec.pk}, {spec.newest} FROM {spec.table} WHERE {cond} AND {spec.pk} <> :pk"),
        {**{f"k_{c}": v for c, v in key_vals.items()}, "pk": pk}).first()
    if twin is None:
        return "go"
    mine = row.get(spec.newest)
    theirs = twin[1]
    if mine is not None and (theirs is None or mine >= theirs):
        conn.execute(text(f"DELETE FROM {spec.table} WHERE {spec.pk} = :p"), {"p": twin[0]})
        return "replaced"
    conn.execute(text(f"DELETE FROM {spec.table} WHERE {spec.pk} = :p"), {"p": pk})
    return "dropped"


def process_table(conn, spec: Spec, decrypt: bool, dry_run: bool, batch: int) -> Result:
    """Convert (or, with dry_run, only count) every row of one table."""
    if conn.in_transaction():  # e.g. left open by has_table(); each batch is its own
        conn.commit()
    res = Result()
    where = _where(spec, decrypt)

    cols = [c for c, _ in spec.cols]
    extra = [c for c in (spec.unique or []) + ([spec.newest] if spec.newest else [])
             if c not in cols and c != spec.pk]
    select_cols = ", ".join(dict.fromkeys([spec.pk] + cols + extra))
    last = None
    while True:
        params = {"n": batch}
        after = ""
        if last is not None:
            after, params["last"] = f" AND {spec.pk} > :last", last
        with conn.begin():
            rows = conn.execute(text(
                f"SELECT {select_cols} FROM {spec.table} WHERE {where}{after} "
                f"ORDER BY {spec.pk} LIMIT :n"), params).mappings().all()
            if not rows:
                break
            for row in rows:
                row = dict(row)
                pk = row[spec.pk]
                old = {c: row[c] for c in cols}
                try:
                    new = {c: _convert(old[c], k, decrypt) for c, k in spec.cols}
                except cc.CustomerCryptoError:
                    raise
                except Exception as e:  # malformed value: report, keep going
                    res.errors.append(f"{spec.table} {pk}: {type(e).__name__}")
                    continue
                if new == old:
                    res.not_ours += 1
                    continue
                if dry_run:
                    res.converted += 1
                    continue
                if spec.unique:
                    with conn.begin_nested():
                        outcome = _resolve_duplicate(conn, spec, pk, row, new)
                    if outcome != "go":
                        res.merged += 1
                    if outcome == "dropped":
                        continue
                try:
                    with conn.begin_nested():
                        n = _cas_update(conn, spec, pk, old, new)
                except Exception as e:  # e.g. a unique clash created by a live request
                    res.changed_meanwhile += 1
                    res.errors.append(f"{spec.table} {pk}: {type(e).__name__}")
                    continue
                if n == 1:
                    res.converted += 1
                else:
                    res.changed_meanwhile += 1
        last = rows[-1][spec.pk]
    return res


# ── Key check ───────────────────────────────────────────────────────────────

def check_key(apply_encrypt: bool) -> None:
    """Same key as the app, recorded the first time anything is encrypted."""
    if not cc.key_configured():
        sys.exit(f"{cc.KEY_ENV} is not set (.env)")
    cc._keys()  # malformed key -> CustomerCryptoError
    eng = cdb.control_engine()
    try:
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS customer_data_encryption ("
                "id integer PRIMARY KEY, canary text NOT NULL, "
                "first_enabled_at timestamptz NOT NULL)"))
            stored = conn.execute(text("SELECT canary FROM customer_data_encryption WHERE id = 1")).scalar()
            if stored is not None and not cc.canary_ok(stored):
                sys.exit(f"{cc.KEY_ENV} is not the key the app has been using. Nothing changed.")
            if stored is None and apply_encrypt:
                conn.execute(text(
                    "INSERT INTO customer_data_encryption (id, canary, first_enabled_at) "
                    "VALUES (1, :c, :t) ON CONFLICT (id) DO NOTHING"),
                    {"c": cc.new_canary(), "t": datetime.now(timezone.utc)})
                print("recorded the key check: the app now requires CUSTOMER_DATA_KEY to start")
    finally:
        eng.dispose()


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Encrypt (or decrypt) stored shopper data.")
    ap.add_argument("--apply", action="store_true", help="encrypt plain rows")
    ap.add_argument("--decrypt", action="store_true", help="decrypt rows back to plain")
    ap.add_argument("--dry-run", action="store_true",
                    help="only count (rows still plain; with --decrypt, rows still encrypted)")
    ap.add_argument("--shop", help="one store: shopify domain, site domain or db name")
    ap.add_argument("--batch", type=int, default=500, help="rows per transaction (default 500)")
    ap.add_argument("--vacuum", action="store_true",
                    help="VACUUM FULL each table that changed (locks it for a few seconds)")
    args = ap.parse_args()
    if args.apply and args.decrypt:
        ap.error("--apply and --decrypt are opposites; pick one")
    if not (args.apply or args.decrypt or args.dry_run):
        ap.error("say --dry-run, --apply or --decrypt")
    decrypt, dry_run = args.decrypt, args.dry_run

    if not dry_run:
        check_key(apply_encrypt=not decrypt)
        on = cc.encryption_on()
        if decrypt and on:
            print(f"warning: {cc.SWITCH_ENV}=1, so new data is still written encrypted. "
                  f"Set it to 0 and restart the app first if you are turning encryption off.")
        if not decrypt and not on:
            print(f"warning: {cc.SWITCH_ENV}=0, so new data is still written plain. "
                  f"Set it to 1 and restart the app, then run this again.")
    elif not cc.key_configured() and decrypt:
        sys.exit(f"{cc.KEY_ENV} is not set (.env)")

    verb = ("would decrypt" if decrypt else "still plain") if dry_run else ("decrypted" if decrypt else "encrypted")
    leftover = 0
    missing: list = []
    tenants = cdb.list_tenants(args.shop, include_inactive=True, missing=missing)
    for t in missing:
        print(f"{t.shop} ({t.db_name}, {t.status}): skipped, its database no longer exists")
    if not tenants:
        print("no matching store", file=sys.stderr)
        return 1
    for t in tenants:
        eng = cdb.engine_for(t.db_name)
        line = []
        try:
            with eng.connect() as conn:
                touched = []
                for spec in SPECS:
                    if not cdb.has_table(conn, spec.table):
                        continue
                    r = process_table(conn, spec, decrypt, dry_run, max(1, args.batch))
                    if r.converted or r.merged:
                        touched.append(spec.table)
                    part = f"{spec.table} {r.converted}"
                    if r.merged:
                        part += f" (+{r.merged} duplicates removed)"
                    if r.changed_meanwhile:
                        part += f" ({r.changed_meanwhile} changed meanwhile, run again)"
                    if r.not_ours:
                        part += f" ({r.not_ours} left as typed)"
                    line.append(part)
                    leftover += r.changed_meanwhile + len(r.errors)
                    for err in r.errors[:5]:
                        print(f"  ! {t.db_name}: {err}", file=sys.stderr)
                    if dry_run:
                        leftover += r.converted
            if args.vacuum and not dry_run and touched:
                with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    for table in touched:
                        conn.execute(text(f"VACUUM (FULL, ANALYZE) {table}"))
                line.append(f"vacuumed {', '.join(touched)}")
        except cc.CustomerCryptoError as e:
            print(f"{t.shop}: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            leftover += 1
            print(f"{t.shop} ({t.db_name}): FAILED | {type(e).__name__}: {e}", file=sys.stderr)
            continue
        finally:
            eng.dispose()
        print(f"{t.shop} ({t.db_name}): {verb}: " + "; ".join(line))

    if dry_run:
        print("nothing left" if leftover == 0 else f"{leftover} rows {verb}")
    return 0 if leftover == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
