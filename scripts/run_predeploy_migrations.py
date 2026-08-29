"""Render pre-deploy migration gate for the cutover writer fence."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence

from cutover_writer_fence import (
    background_writer_fence_report,
    cutover_writer_fence_enabled,
)


PREDEPLOY_COMPONENT = "render_predeploy_migration"


def main(
    *,
    run_command: Callable[..., object] = subprocess.run,
    command: Sequence[str] | None = None,
) -> int:
    """Skip every migration when fenced; otherwise preserve ``flask db upgrade``."""

    if cutover_writer_fence_enabled():
        print(json.dumps(background_writer_fence_report(PREDEPLOY_COMPONENT), sort_keys=True))
        return 0

    migration_command = list(command or (sys.executable, "-m", "flask", "db", "upgrade"))
    completed = run_command(migration_command, check=False)
    return int(getattr(completed, "returncode", 1))


if __name__ == "__main__":
    raise SystemExit(main())
