from config import _resolve_backend_version


def test_deployment_scoped_revision_wins_for_local_cli_container_release():
    assert (
        _resolve_backend_version(
            {
                "VERCEL": "1",
                "CHATBOC_DEPLOYMENT_REVISION": "cli-worktree-release-sha",
                "VERCEL_GIT_COMMIT_SHA": "stale-git-integration-sha",
                "BACKEND_VERSION": "stale-manual-version",
            }
        )
        == "cli-worktree-release-sha"
    )


def test_vercel_revision_wins_over_stale_manual_and_render_values():
    assert (
        _resolve_backend_version(
            {
                "VERCEL": "1",
                "VERCEL_GIT_COMMIT_SHA": "vercel-immutable-sha",
                "BACKEND_VERSION": "stale-manual-version",
                # Migrated Render variables must not mask the Vercel revision.
                "RENDER": "true",
                "RENDER_GIT_COMMIT": "stale-render-sha",
            }
        )
        == "vercel-immutable-sha"
    )


def test_render_revision_wins_over_stale_manual_value():
    assert (
        _resolve_backend_version(
            {
                "RENDER": "true",
                "RENDER_GIT_COMMIT": "render-immutable-sha",
                "BACKEND_VERSION": "stale-manual-version",
                # A commit-shaped variable alone is not a Vercel runtime signal.
                "VERCEL_GIT_COMMIT_SHA": "stale-vercel-sha",
            }
        )
        == "render-immutable-sha"
    )


def test_manual_backend_version_remains_local_fallback():
    assert (
        _resolve_backend_version(
            {
                "BACKEND_VERSION": "manual-local-version",
                "VERCEL_GIT_COMMIT_SHA": "unscoped-vercel-sha",
                "RENDER_GIT_COMMIT": "unscoped-render-sha",
            }
        )
        == "manual-local-version"
    )


def test_platform_without_commit_falls_back_to_manual_backend_version():
    assert (
        _resolve_backend_version(
            {
                "VERCEL_ENV": "preview",
                "VERCEL_GIT_COMMIT_SHA": "  ",
                "BACKEND_VERSION": "manual-preview-fallback",
            }
        )
        == "manual-preview-fallback"
    )
