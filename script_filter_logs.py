#!/usr/bin/env python3
"""Filter log files by level, keyword, or session id.

This script helps developers and operators quickly inspect log files.
It can filter by log level (INFO, ERROR, etc.), search for a substring
using a regular expression, optionally scope results to a session id,
and show context around each match. Basic statistics by level can also
be printed to spot noisy sections.
"""

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import Iterable


def iter_lines(path: Path) -> Iterable[str]:
    """Yield lines from *path* safely handling encoding issues."""
    return path.read_text(errors="ignore").splitlines()


def main() -> None:
    parser = argparse.ArgumentParser(description="Filter log files.")
    parser.add_argument("logfile", help="Path to the log file to inspect")
    parser.add_argument(
        "--level",
        help="Only show lines containing this log level (e.g., INFO, ERROR)",
    )
    parser.add_argument(
        "--contains",
        help="Regular expression to filter lines by message content",
    )
    parser.add_argument(
        "--session",
        help="Session ID, anon ID or UUID to filter by (simple substring match)",
    )
    parser.add_argument(
        "--tail",
        type=int,
        help="Show only the last N matching lines",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=0,
        help="Number of surrounding lines to show around each match",
    )
    parser.add_argument(
        "--number",
        action="store_true",
        help="Show line numbers for matched output",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print a summary of log levels for matching lines",
    )
    args = parser.parse_args()

    path = Path(args.logfile)
    if not path.exists():
        raise SystemExit(f"Log file not found: {path}")

    lines = list(iter_lines(path))
    if args.tail:
        lines = lines[-args.tail :]

    def matches(line: str) -> bool:
        if args.level and args.level.upper() not in line.upper():
            return False
        if args.contains and not re.search(args.contains, line):
            return False
        if args.session and args.session not in line:
            return False
        return True

    matching_indexes = [idx for idx, line in enumerate(lines) if matches(line)]

    if args.stats:
        level_pattern = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
        counter = Counter()
        for idx in matching_indexes:
            for level in level_pattern.findall(lines[idx]):
                counter[level] += 1
        print("Stats (matching lines only):")
        for level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            if counter[level]:
                print(f"  {level}: {counter[level]}")

    if not matching_indexes:
        return

    indexes_to_show = set()
    for idx in matching_indexes:
        start = max(0, idx - args.context)
        end = min(len(lines), idx + args.context + 1)
        indexes_to_show.update(range(start, end))

    for idx in sorted(indexes_to_show):
        prefix = f"{idx + 1}: " if args.number else ""
        print(f"{prefix}{lines[idx]}")


if __name__ == "__main__":
    main()
