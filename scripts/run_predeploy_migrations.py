"""Render pre-deploy migration gate for the cutover writer fence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from cutover_writer_fence import (
    background_writer_fence_report,
    cutover_writer_fence_enabled,
)


PREDEPLOY_COMPONENT = "render_predeploy_migration"
PREDEPLOY_CONTRACT = "chatboc.render_predeploy.v1"


def _render_standby_mode_enabled(environ: Mapping[str, str]) -> bool:
    raw_value = environ.get("CHATBOC_RENDER_STANDBY_MODE")
    if raw_value is None:
        return False
    normalized = str(raw_value).strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return False
    return True


def _blocked(reason: str) -> int:
    print(
        json.dumps(
            {
                "contract": PREDEPLOY_CONTRACT,
                "status": "blocked",
                "reason": reason,
            },
            sort_keys=True,
        )
    )
    return 2


def main(
    *,
    run_command: Callable[..., object] = subprocess.run,
    command: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Skip while fenced; require direct Neon migrations for Render standby."""

    runtime_env = os.environ if environ is None else environ
    if cutover_writer_fence_enabled(runtime_env):
        print(json.dumps(background_writer_fence_report(PREDEPLOY_COMPONENT), sort_keys=True))
        return 0

    standby_mode = _render_standby_mode_enabled(runtime_env)
    if standby_mode:
        database_url = str(runtime_env.get("MIGRATIONS_DATABASE_URL") or "").strip()
        if not database_url:
            return _blocked("migrations_database_url_missing")
        try:
            # Imported only after the writer fence. A fenced deployment must
            # remain able to skip migrations without loading DB/Alembic code.
            from scripts.preflight_neon_cutover import (
                PreflightFailure,
                _validate_neon_direct_url,
            )

            _validate_neon_direct_url(database_url)
        except PreflightFailure as exc:
            return _blocked(exc.reason_code)

        migration_command = list(
            command or (sys.executable, "-m", "scripts.apply_migrations")
        )
    else:
        migration_command = list(
            command or (sys.executable, "-m", "flask", "db", "upgrade")
        )
    completed = run_command(migration_command, check=False)
    return int(getattr(completed, "returncode", 1))


if __name__ == "__main__":
    raise SystemExit(main())
