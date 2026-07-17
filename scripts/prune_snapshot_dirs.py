"""
scripts/prune_snapshot_dirs.py — delete ORPHANED tenant snapshot directories.

An "orphan" is a directory under TENANT_SNAPSHOT_DIR whose name is NOT the
tenant_id of any row in the `tenants` table. These accumulate from:
  - old license_id-named dirs left behind by tenants deleted before the
    tenant_id rename (rename_snapshot_dirs.py only moves dirs that still have a
    matching tenant row; a deleted tenant's old dir is never touched),
  - snapshots of tenants that were torn down (DB dropped) but whose snapshot
    dir was never cleaned (the pre-fix teardown leak),
  - stray/legacy dirs (e.g. __default__ from single-tenant days).

Archived tenants: by DEFAULT their snapshots are KEPT, because the row still
exists (archived, not deleted). Pass --include-archived to also prune them —
an archived tenant's physical DB has been dropped, so its snapshot is dead
weight.

SAFETY MODEL
  - Dry-run by DEFAULT. Nothing is deleted unless you pass --apply.
  - Refuses to delete when the tenants table looks empty (0 valid tenant_ids)
    but snapshot dirs exist — that usually means the script is pointed at the
    wrong/empty database. Override with --force only if you are certain.
  - Only ever removes directories strictly inside the snapshot root; never the
    root itself, never stray files.
  - rmtree failures are logged and counted, never fatal — one locked dir does
    not stop the rest.

USAGE
    python scripts/prune_snapshot_dirs.py                      # dry-run: list orphans + sizes
    python scripts/prune_snapshot_dirs.py --include-archived   # dry-run, incl. archived tenants
    python scripts/prune_snapshot_dirs.py --apply              # actually delete orphans
    python scripts/prune_snapshot_dirs.py --apply --include-archived

Run from the project root (the same cwd the app runs from) so the snapshot
root resolves identically to tenant_snapshot_store.py. Recommended workflow:
run once with no flags, eyeball the list, then re-run with --apply.
"""

import argparse
import os
import shutil
import sys

# Make the app's modules importable when run as `python scripts/prune_snapshot_dirs.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _snapshot_root() -> str:
    """
    Mirrors tenant_snapshot_store's on-disk root. When TENANT_SNAPSHOT_DIR is
    unset the app uses os.path.join(os.getcwd(), ".tenant_snapshots"); running
    this script from the project root resolves to the same place. Confirm the
    value below matches tenant_snapshot_store.py before running with --apply.
    """
    return os.getenv("TENANT_SNAPSHOT_DIR", ".tenant_snapshots")


def _dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete orphaned dirs. Without this, dry-run only.")
    parser.add_argument("--include-archived", action="store_true",
                        help="Also prune snapshots of archived tenants (their DB is already dropped).")
    parser.add_argument("--force", action="store_true",
                        help="Override the empty-tenant-table safety refusal. Use with care.")
    args = parser.parse_args()

    from server import app          # adjust if the Flask app factory lives elsewhere
    from models import Tenant

    root = _snapshot_root()
    abs_root = os.path.abspath(root)

    print(f"prune_snapshot_dirs: root={root} (resolved: {abs_root}) | "
          f"apply={args.apply} | include_archived={args.include_archived}")

    if not os.path.isdir(abs_root):
        print(f"prune_snapshot_dirs: snapshot root does not exist — nothing to do.")
        return

    # ── Build the set of tenant_ids that must be KEPT ─────────────────────────
    with app.app_context():
        tenants = Tenant.query.all()
        if args.include_archived:
            valid = {str(t.tenant_id).lower() for t in tenants if t.status != "archived"}
            archived_count = sum(1 for t in tenants if t.status == "archived")
        else:
            valid = {str(t.tenant_id).lower() for t in tenants}
            archived_count = 0

    print(f"prune_snapshot_dirs: {len(tenants)} tenant row(s) in DB | "
          f"{len(valid)} keep-set tenant_id(s)"
          + (f" | {archived_count} archived treated as prunable" if args.include_archived else ""))

    # ── List on-disk snapshot directories ─────────────────────────────────────
    entries = [e for e in os.listdir(abs_root)
               if os.path.isdir(os.path.join(abs_root, e))]
    orphans = sorted(e for e in entries if e.lower() not in valid)
    kept = [e for e in entries if e.lower() in valid]

    print(f"prune_snapshot_dirs: {len(entries)} dir(s) on disk | "
          f"{len(kept)} match a tenant (keep) | {len(orphans)} orphaned")

    # ── Safety: empty keep-set but dirs present → likely wrong/empty DB ───────
    if not valid and entries and not args.force:
        print("\n⛔ REFUSING TO DELETE: the tenants keep-set is empty but "
              f"{len(entries)} snapshot dir(s) exist on disk.\n"
              "   This usually means the script is pointed at the wrong or an "
              "empty database.\n"
              "   If you are certain you want to delete ALL snapshot dirs, "
              "re-run with --force.")
        sys.exit(2)

    if not orphans:
        print("prune_snapshot_dirs: no orphaned directories — nothing to prune. ✅")
        return

    # ── Report orphans with sizes ─────────────────────────────────────────────
    total_bytes = 0
    print("\nOrphaned snapshot directories:")
    for name in orphans:
        p = os.path.join(abs_root, name)
        size = _dir_size_bytes(p)
        total_bytes += size
        action = "DELETE " if args.apply else "would delete"
        print(f"  {action} | {name}  ({_human(size)})")
    print(f"\nTotal reclaimable: {_human(total_bytes)} across {len(orphans)} dir(s)")

    if not args.apply:
        print("\nDry-run only — nothing deleted. Re-run with --apply to remove these.")
        return

    # ── Delete (best-effort, per-dir) ─────────────────────────────────────────
    deleted, freed, errors = 0, 0, 0
    for name in orphans:
        p = os.path.join(abs_root, name)
        # Defensive: never step outside the snapshot root, never the root itself.
        real = os.path.abspath(p)
        if real == abs_root or os.path.dirname(real) != abs_root:
            print(f"  SKIP (path guard) | {name}")
            continue
        size = _dir_size_bytes(p)
        try:
            shutil.rmtree(p)
            deleted += 1
            freed += size
            print(f"  ✅ deleted | {name}  ({_human(size)})")
        except Exception as e:
            errors += 1
            print(f"  ⚠️ error   | {name} | {e}")

    print(f"\nprune_snapshot_dirs: done | deleted={deleted} "
          f"freed={_human(freed)} errors={errors}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()