#!/usr/bin/env python3
"""Filter and follow log files with optional coloring.

This utility helps developers and operators quickly inspect log files.
It can:

* Filter by log level (INFO, ERROR, etc.).
* Search for a substring using a regular expression.
* Show only the last N matching lines.
* Optionally ``follow`` the file like ``tail -f``.
* Colorize log levels for easier scanning.
"""

import argparse
import re
import time
from pathlib import Path
from typing import Iterable

from colorama import Fore, Style, init


def iter_lines(path: Path) -> Iterable[str]:
    """Yield lines from *path* safely handling encoding issues."""
    return path.read_text(errors="ignore").splitlines()


def colorize(line: str) -> str:
    """Return *line* wrapped in ANSI colors according to log level."""
    if "ERROR" in line:
        color = Fore.RED
    elif "WARNING" in line:
        color = Fore.YELLOW
    elif "INFO" in line:
        color = Fore.GREEN
    else:
        color = ""
    return f"{color}{line}{Style.RESET_ALL}" if color else line


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
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Follow the file for new lines (like tail -f)",
    )
    args = parser.parse_args()

    init(autoreset=True)

    path = Path(args.logfile)
    if not path.exists():
        raise SystemExit(f"Log file not found: {path}")

    def matches(line: str) -> bool:
        if args.level and args.level not in line:
            return False
        if args.contains and not re.search(args.contains, line):
            return False
        return True

    lines = list(iter_lines(path))
    if args.tail:
        lines = lines[-args.tail :]

    for line in lines:
        if matches(line):
            print(colorize(line))

    if args.follow:
        with path.open("r") as f:
            f.seek(0, 2)
            try:
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    if matches(line):
                        print(colorize(line), end="")
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
