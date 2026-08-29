from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _patterns(filename: str) -> set[str]:
    return {
        line.strip()
        for line in (ROOT / filename).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_runtime_upload_directories_are_excluded_from_vercel_container():
    docker_patterns = _patterns(".dockerignore")
    vercel_patterns = _patterns(".vercelignore")

    assert {
        "data/archivos/*",
        "data/archivos_tickets/*",
        "data/catalogos/*",
    }.issubset(docker_patterns)
    assert {
        "data/archivos/**",
        "data/archivos_tickets/**",
        "data/catalogos/**",
    }.issubset(vercel_patterns)
