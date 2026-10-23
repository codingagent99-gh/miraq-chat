"""
purge_logs.py - delete dated log folders older than the retention period.

chat_logger.py writes logs/YYYY-MM-DD/{chat,api,orders,timing}.txt and never
deletes anything. The privacy policy promises operational logs are kept for
about one month, so this script enforces that. Run it daily from cron, e.g.:

    30 2 * * * /home/apps/varchaswi/miraq-chat/.venv/bin/python \
        /home/apps/varchaswi/<project>/purge_logs.py >> \
        /home/apps/varchaswi/<project>/logs/purge.log 2>&1

Only direct sub-folders of logs/ whose name is a YYYY-MM-DD date are touched;
symlinks, other folders and loose files (such as purge.log) are left alone.

Usage:
    python purge_logs.py                 # keep 30 days
    python purge_logs.py --days 14       # keep 14 days
    python purge_logs.py --dry-run       # list what would be deleted
"""

import argparse
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_KEEP_DAYS = 30
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def purge(log_base: Path, keep_days: int = DEFAULT_KEEP_DAYS,
          dry_run: bool = False, today: date | None = None) -> list[Path]:
    """Delete dated folders older than `keep_days`; return the folders removed."""
    today = today or date.today()
    cutoff = today - timedelta(days=keep_days)
    removed: list[Path] = []
    if not log_base.is_dir():
        return removed
    for child in sorted(log_base.iterdir()):
        if child.is_symlink() or not child.is_dir() or not _DATE_DIR.match(child.name):
            continue
        try:
            folder_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_date <= cutoff:
            removed.append(child)
            if not dry_run:
                shutil.rmtree(child)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete log folders older than N days.")
    parser.add_argument("--days", type=int, default=DEFAULT_KEEP_DAYS,
                        help=f"days of logs to keep (default {DEFAULT_KEEP_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the folders that would be deleted, delete nothing")
    parser.add_argument("--log-dir", type=Path,
                        default=Path(__file__).resolve().parent / "logs",
                        help="logs directory (default: logs/ next to this script)")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")

    removed = purge(args.log_dir, args.days, args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] purge_logs: {verb} {len(removed)} folder(s) older than "
          f"{args.days} days in {args.log_dir}")
    for folder in removed:
        print(f"  {folder.name}")


if __name__ == "__main__":
    main()