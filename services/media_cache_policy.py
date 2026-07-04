IMMUTABLE_PUBLIC_CACHE = "public, max-age=31536000, immutable"
CATALOG_PUBLIC_CACHE = "public, max-age=604800"
PRIVATE_ATTACHMENT_CACHE = "private, no-store"
DEFAULT_PRIVATE_CACHE = "private, max-age=3600"


def normalise_media_key(key: str) -> str:
    return "/".join(str(key or "").replace("\\", "/").strip("/").lower().split("/"))


def key_has_segment(key: str, segments: set[str]) -> bool:
    return any(segment in segments for segment in key.split("/") if segment)


def cache_control_for_key(key: str, content_type: str | None = None) -> str:
    normalized_key = normalise_media_key(key)
    normalized_type = str(content_type or "").lower()

    if normalized_key.startswith("static/audio_cache/") or "/static/audio_cache/" in f"/{normalized_key}/":
        return IMMUTABLE_PUBLIC_CACHE

    sensitive_segments = {
        "adjuntos",
        "attachments",
        "claims",
        "conversations",
        "messages",
        "pedido",
        "pedidos",
        "reclamos",
        "tickets",
        "uploads",
    }
    if key_has_segment(normalized_key, sensitive_segments):
        return PRIVATE_ATTACHMENT_CACHE

    public_segments = {
        "catalog",
        "catalogo",
        "catalogos",
        "market",
        "marketplace",
        "products",
        "productos",
        "public",
    }
    public_logo_segments = {"logos", "avatars", "brand", "brands"}
    if key_has_segment(normalized_key, public_segments | public_logo_segments):
        return CATALOG_PUBLIC_CACHE

    if normalized_type.startswith("text/") or normalized_type in {"application/json", "application/pdf"}:
        return DEFAULT_PRIVATE_CACHE

    return DEFAULT_PRIVATE_CACHE
