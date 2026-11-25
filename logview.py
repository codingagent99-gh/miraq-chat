#!/usr/bin/env python3
"""
logview.py - read MiraQ's log files decrypted, for debugging.

With ENCRYPT_CUSTOMER_DATA=1 each log line is written as
    [2026-11-25 10:41:07.214] [INFO] enc1:<token>
(time and level readable, the rest encrypted; chat_logger.py). This prints
the original lines. Plain lines (written while the switch was off, and
timing.txt) are printed as they are. Run it on the server, from the project
folder: it needs the project's .env and CUSTOMER_DATA_KEY.

    python logview.py chat --today                      # logs/<today>/chat.txt
    python logview.py chat --date 2026-11-24
    python logview.py chat --follow                     # like tail -f
    python logview.py orders --grep "1042"              # like grep -i
    python logview.py api --today --grep "orders.json" --tail 50
    python logview.py logs/2026-11-24/trace.txt         # any file
    pm2 logs miraq-multi --raw --nostream | python logview.py -
    python logview.py --encrypt-old [--dry-run]         # see below

--encrypt-old encrypts the plain lines left in older log files (written
before the switch was turned on). It skips today's folder, whose files the
running app still has open, and timing.txt / purge.log, which hold no shopper
data. Run it the day after switching on. Lines already encrypted are kept.

The output is plain shopper data: quote only what you need.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import customer_crypto as cc

LOG_DIR = Path(__file__).resolve().parent / "logs"
NAMES = {"chat": "chat.txt", "api": "api.txt", "orders": "orders.txt",
         "trace": "trace.txt", "timing": "timing.txt"}
NOT_SHOPPER_DATA = {"timing.txt", "purge.log"}

# [time] [LEVEL] enc1:...   or   [time] enc1:...   or a bare enc1:... line
_ENC_LINE = re.compile(r"^(?:\[[^\]]*\](?: \[[A-Z]+\])? )?(enc1:[A-Za-z0-9_\-=]+)\s*$")
_PREFIX = re.compile(r"^(\[[^\]]*\])(?: (\[[A-Z]+\]))?")
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def decrypt_line(line: str) -> str:
    """The original text of one log line (may span several lines, e.g. a traceback)."""
    m = _ENC_LINE.match(line.rstrip("\n"))
    if not m:
        return line.rstrip("\n")
    return cc.decrypt_text(m.group(1))


def _emit(text: str, grep: str | None) -> None:
    if grep and grep.lower() not in text.lower():
        return
    print(text, flush=True)


def view(path: Path, grep: str | None, tail: int | None) -> None:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    out = [decrypt_line(l) for l in lines]
    if grep:
        out = [t for t in out if grep.lower() in t.lower()]
    for t in out[-tail:] if tail else out:
        print(t)


def follow(name: str, grep: str | None) -> None:
    """tail -f across midnight: switches to the new day's file when it appears."""
    day, f = None, None
    try:
        while True:
            today = date.today().isoformat()
            if today != day:
                path = LOG_DIR / today / name
                if path.exists():
                    if f:
                        f.close()
                    f = open(path, encoding="utf-8", errors="replace")
                    if day is None:
                        f.seek(0, os.SEEK_END)  # start at the end, like tail -f
                    day = today
            line = f.readline() if f else ""
            if line:
                _emit(decrypt_line(line), grep)
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if f:
            f.close()


def encrypt_line(line: str) -> str:
    """Encrypt one plain line in the live format, keeping time/level readable."""
    body = line.rstrip("\n")
    if not body or _ENC_LINE.match(body):
        return line
    m = _PREFIX.match(body)
    prefix = " ".join(p for p in (m.group(1), m.group(2)) if p) + " " if m else ""
    return f"{prefix}{cc.encrypt_text(body, force=True)}\n"


def encrypt_old(dry_run: bool) -> int:
    if not cc.key_configured():
        print("CUSTOMER_DATA_KEY is not set", file=sys.stderr)
        return 2
    today = date.today().isoformat()
    files = lines = 0
    for day_dir in sorted(p for p in LOG_DIR.iterdir() if p.is_dir() and _DATE_DIR.match(p.name)):
        if day_dir.name >= today:
            continue  # the running app still appends to today's files
        for path in sorted(day_dir.glob("*.txt")):
            if path.name in NOT_SHOPPER_DATA or path.is_symlink():
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                old = f.readlines()
            new = [encrypt_line(l) for l in old]
            changed = sum(1 for a, b in zip(old, new) if a != b)
            if not changed:
                continue
            files += 1
            lines += changed
            print(f"{'would encrypt' if dry_run else 'encrypted'} {changed} lines | {path}")
            if dry_run:
                continue
            fd, tmp = tempfile.mkstemp(dir=day_dir, prefix=f".{path.name}.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    out.writelines(new)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)  # atomic: readers see the old or the new file
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
    print(f"{'would encrypt' if dry_run else 'encrypted'} {lines} lines in {files} files "
          f"(skipped today's folder {today})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Read MiraQ logs decrypted.")
    ap.add_argument("what", nargs="?",
                    help="chat | api | orders | trace | timing | a file path | - for stdin")
    ap.add_argument("--today", action="store_true", help="today's file (the default)")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--grep", help="only lines containing this text (case-insensitive)")
    ap.add_argument("--tail", type=int, help="only the last N lines")
    ap.add_argument("--follow", "-f", action="store_true", help="keep printing new lines")
    ap.add_argument("--encrypt-old", action="store_true",
                    help="encrypt plain lines in older log files (not today's)")
    ap.add_argument("--dry-run", action="store_true", help="with --encrypt-old: only report")
    args = ap.parse_args()

    if args.encrypt_old:
        return encrypt_old(args.dry_run)
    if not args.what:
        ap.error("say which log: chat, api, orders, trace, timing, a path, or -")

    if args.what == "-":
        for line in sys.stdin:
            _emit(decrypt_line(line), args.grep)
        return 0

    if args.what in NAMES:
        name = NAMES[args.what]
        if args.follow:
            follow(name, args.grep)
            return 0
        path = LOG_DIR / (args.date or date.today().isoformat()) / name
    else:
        path = Path(args.what)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    view(path, args.grep, args.tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
