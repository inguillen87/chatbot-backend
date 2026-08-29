"""Dependency-free, redacted identity for a Chatboc release checkout."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any


MIGRATION_TREE_CONTRACT = b"chatboc.migrations.versions.git-tree.v1\0"
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class CheckoutIdentityFailure(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _git_output(
    project_root: Path,
    *arguments: str,
    run_command: Callable[..., Any] = subprocess.run,
) -> bytes:
    try:
        completed = run_command(
            ["git", *arguments],
            cwd=project_root,
            check=False,
            capture_output=True,
        )
    except Exception as exc:
        raise CheckoutIdentityFailure("git_command_unavailable") from exc
    if int(getattr(completed, "returncode", 1)) != 0:
        raise CheckoutIdentityFailure("git_command_failed")
    stdout = getattr(completed, "stdout", b"")
    if isinstance(stdout, str):
        return stdout.encode("utf-8")
    if isinstance(stdout, bytes):
        return stdout
    raise CheckoutIdentityFailure("git_output_invalid")


def checkout_identity(
    project_root: Path,
    *,
    run_command: Callable[..., Any] = subprocess.run,
) -> dict[str, object]:
    """Return immutable source and migration-tree evidence without file data."""

    root = project_root.resolve()
    revision = _git_output(
        root,
        "rev-parse",
        "HEAD",
        run_command=run_command,
    ).decode("ascii", errors="strict").strip().lower()
    if not REVISION_PATTERN.fullmatch(revision):
        raise CheckoutIdentityFailure("git_revision_invalid")

    status = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=normal",
        run_command=run_command,
    )
    migration_tree = _git_output(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "HEAD",
        "--",
        "migrations/versions",
        run_command=run_command,
    )
    if not migration_tree:
        raise CheckoutIdentityFailure("migration_versions_tree_missing")

    fingerprint = hashlib.sha256(
        MIGRATION_TREE_CONTRACT + migration_tree
    ).hexdigest()
    return {
        "source_revision": revision,
        "worktree_clean": not bool(status.strip()),
        "migration_versions_fingerprint_sha256": fingerprint,
    }


__all__ = [
    "CheckoutIdentityFailure",
    "MIGRATION_TREE_CONTRACT",
    "checkout_identity",
]
