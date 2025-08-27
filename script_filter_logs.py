#!/usr/bin/env python3
"""Filter log files by level or keyword.

This script helps developers and operators quickly inspect log files.
It can filter by log level (INFO, ERROR, etc.), search for a substring
using a regular expression, and optionally show only the last N lines.
"""

import argparse
import re
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
        "--tail",
        type=int,
        help="Show only the last N matching lines",
    )
    args = parser.parse_args()

    path = Path(args.logfile)
    if not path.exists():
        raise SystemExit(f"Log file not found: {path}")

    lines = list(iter_lines(path))
    if args.tail:
        lines = lines[-args.tail :]

    for line in lines:
        if args.level and args.level not in line:
            continue
        if args.contains and not re.search(args.contains, line):
            continue
        print(line)


if __name__ == "__main__":
    main()
