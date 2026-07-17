"""
scripts/rename_snapshot_dirs.py — one-time snapshot directory rename.

Run ONCE, after the Phase 2 migration has landed (every Tenant row has a
tenant_id). Existing snapshot dirs live at .tenant_snapshots/<license_id>/;
Phase 5 switches TenantRegistry to read/write snapshots keyed by
str(tenant_id) instead, so any existing on-disk snapshot needs to move to
its new path or it'll look like a cache miss (safe, just triggers a cold
WooCommerce fetch — but defeats the point of running this at all).

Idempotent: if <tenant_id> already exists, or <license_id> doesn't exist,
the row is skipped and logged. Safe to re-run.

Usage:
    python scripts/rename_snapshot_dirs.py --dry-run   # log only, no changes
    python scripts/rename_snapshot_dirs.py              # actually rename

Run this in the same maintenance window as the Phase 2 migration, against
the same DB the running app points at.
"""

import argparse
import os
import sys

# Make the app's modules importable when run as `python scripts/rename_snapshot_dirs.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _snapshot_root() -> str:
    """
    Mirrors tenant_snapshot_store's on-disk root. Adjust here if that
    module defines a different path — this script does not import it
    directly to avoid pulling in the full Flask app just to resolve a
    directory constant; confirm the value below matches
    tenant_snapshot_store.py before running.
    """
    return os.getenv("TENANT_SNAPSHOT_DIR", ".tenant_snapshots")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be renamed without touching the filesystem.",
    )
    args = parser.parse_args()

    from server import app  # adjust if the Flask app factory lives elsewhere
    from models import Tenant

    root = _snapshot_root()
    renamed, skipped_no_source, skipped_already_done, errors = 0, 0, 0, 0

    with app.app_context():
        tenants = Tenant.query.all()
        print(f"rename_snapshot_dirs: {len(tenants)} tenant row(s) | root={root} | dry_run={args.dry_run}")

        for tenant in tenants:
            tenant_id = str(tenant.tenant_id)
            license_id = tenant.license_id

            if not license_id:
                # Free tenant created after Phase 4 — never had a license_id-named
                # snapshot dir to begin with. Nothing to rename.
                continue

            old_path = os.path.join(root, license_id)
            new_path = os.path.join(root, tenant_id)

            if not os.path.isdir(old_path):
                skipped_no_source += 1
                continue

            if os.path.isdir(new_path):
                print(f"  SKIP (already renamed) | tenant_id={tenant_id} | new_path exists")
                skipped_already_done += 1
                continue

            print(f"  RENAME | {old_path} -> {new_path}")
            if not args.dry_run:
                try:
                    os.rename(old_path, new_path)
                    renamed += 1
                except OSError as e:
                    print(f"  ERROR renaming {old_path} -> {new_path}: {e}")
                    errors += 1
            else:
                renamed += 1  # counted as "would rename" under dry-run

    print(
        f"rename_snapshot_dirs: done | renamed={renamed} "
        f"skipped_no_source={skipped_no_source} skipped_already_done={skipped_already_done} "
        f"errors={errors}"
    )
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()