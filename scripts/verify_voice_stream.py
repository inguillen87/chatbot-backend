"""Run the deterministic realtime voice verification suites.

Provider-backed Twilio/OpenAI certification is a separate staging gate. This
script deliberately runs the offline contract and security suites and returns
their real exit status instead of simulating the application with module stubs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VOICE_TESTS = (
    "tests/test_voice_stream_service.py",
    "tests/test_voice_stream_security.py",
    "tests/test_voice_consent_lifecycle.py",
    "tests/test_voice_realtime_routes.py",
)


def main() -> int:
    command = [sys.executable, "-m", "pytest", "-q", *VOICE_TESTS]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
