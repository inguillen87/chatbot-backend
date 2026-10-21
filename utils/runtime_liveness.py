"""Identify the process-only public liveness route without resolving identity."""


def is_public_liveness_request(req) -> bool:
    """Keep dependency checks and all neighboring application routes separate."""

    return (
        req.path == "/health"
        and req.endpoint == "health"
        and req.method in {"GET", "HEAD"}
    )
