from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from scripts.release_checkout_identity import (
    MIGRATION_TREE_CONTRACT,
    checkout_identity,
)


REVISION = "a" * 40
TREE = b"100644 blob 0123456789abcdef\tmigrations/versions/a.py\0"


def _runner(*, dirty: bool = False):
    def run(command, **_kwargs):
        arguments = command[1:]
        if arguments == ["rev-parse", "HEAD"]:
            output = f"{REVISION}\n".encode()
        elif arguments[:2] == ["status", "--porcelain"]:
            output = b" M migrations/versions/a.py\n" if dirty else b""
        elif arguments[:2] == ["ls-tree", "-r"]:
            output = TREE
        else:  # pragma: no cover - makes command contract regressions obvious
            raise AssertionError(arguments)
        return SimpleNamespace(returncode=0, stdout=output)

    return run


def test_checkout_identity_hashes_committed_migration_tree_without_contents(
    tmp_path: Path,
) -> None:
    identity = checkout_identity(tmp_path, run_command=_runner())

    assert identity == {
        "source_revision": REVISION,
        "worktree_clean": True,
        "migration_versions_fingerprint_sha256": hashlib.sha256(
            MIGRATION_TREE_CONTRACT + TREE
        ).hexdigest(),
    }
    assert "a.py" not in str(identity)


def test_checkout_identity_marks_dirty_worktree(tmp_path: Path) -> None:
    identity = checkout_identity(tmp_path, run_command=_runner(dirty=True))

    assert identity["worktree_clean"] is False
