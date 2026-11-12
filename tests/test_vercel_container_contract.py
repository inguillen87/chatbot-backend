from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERCEL_DOCKERFILE = ROOT / "Dockerfile.vercel"
RENDER_BUILD_SCRIPT = ROOT / "build.sh"
RUNTIME_SOURCE_ROOTS = (ROOT / "app.py", ROOT / "routes", ROOT / "services")


def _active_lines(path: Path) -> str:
    return "\n".join(
        line.split("#", 1)[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    )


def _runtime_python_files() -> list[Path]:
    files: list[Path] = []
    for root in RUNTIME_SOURCE_ROOTS:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))
    return files


def test_vercel_container_does_not_install_unused_ffmpeg() -> None:
    active_dockerfile = _active_lines(VERCEL_DOCKERFILE)

    assert not re.search(r"\bffmpeg\b", active_dockerfile, re.IGNORECASE), (
        "The Vercel web container intentionally excludes FFmpeg. Before adding it, "
        "introduce a reachable bounded-media runtime flow and its regression tests."
    )


def test_render_keeps_its_independent_ffmpeg_contract() -> None:
    active_render_build = _active_lines(RENDER_BUILD_SCRIPT)

    assert re.search(r"\bffmpeg\b", active_render_build, re.IGNORECASE), (
        "This Vercel-only optimization must not silently change Render's media runtime."
    )


def test_vercel_ffmpeg_exclusion_still_matches_reachable_runtime_code() -> None:
    dormant_helpers = {
        ROOT / "services" / "media_analysis.py",
        ROOT / "services" / "video_frame.py",
    }
    caller_pattern = re.compile(
        r"(?:from\s+services\.(?:media_analysis|video_frame)\s+import|"
        r"import\s+services\.(?:media_analysis|video_frame)|"
        r"\b(?:analyze_video_from_url|obtener_frame_png)\s*\()"
    )
    pydub_pattern = re.compile(r"(?:from\s+pydub\s+import|import\s+pydub\b)")
    callers: list[str] = []
    pydub_imports: list[str] = []

    for path in _runtime_python_files():
        source = path.read_text(encoding="utf-8")
        if path not in dormant_helpers and caller_pattern.search(source):
            callers.append(path.relative_to(ROOT).as_posix())
        if pydub_pattern.search(source):
            pydub_imports.append(path.relative_to(ROOT).as_posix())

    assert callers == [], (
        "A legacy FFmpeg helper became reachable; either keep provider-native media "
        f"handling or explicitly restore and validate FFmpeg. Callers: {callers}"
    )
    assert pydub_imports == [], (
        "pydub normally shells out to FFmpeg for common compressed formats; a new "
        f"runtime import requires an explicit container decision. Imports: {pydub_imports}"
    )
