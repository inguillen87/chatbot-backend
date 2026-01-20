from __future__ import annotations


def public_url(path_or_url: str, base_url: str) -> str:
    if not path_or_url:
        return ""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return base_url.rstrip("/") + "/" + path_or_url.lstrip("/")
