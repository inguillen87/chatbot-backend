"""Identify public process metadata that does not consume tenant context."""


def is_public_version_metadata_request(req) -> bool:
    return (
        req.path == "/api/version"
        and req.endpoint == "config_bp.get_version_info"
        and req.method in {"GET", "HEAD"}
    )
