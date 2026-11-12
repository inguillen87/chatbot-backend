"""Public receipt images belong to the verified organization, never the claim."""
from ipaddress import ip_address
import re
from urllib.parse import unquote, urlsplit, urlunsplit


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def public_whatsapp_receipt_image_url(
    value, *, public_base_url: str = "", allow_relative_static: bool = False
) -> str | None:
    """Validate public image URL shape without fetching or resolving its host.

    Profile writes require an absolute URL. Only legacy tenant settings may
    resolve an explicit static path against the caller's public HTTPS origin.
    """
    if not isinstance(value, str):
        return None
    if any(ord(char) < 32 for char in value):
        return None
    value = value.strip()
    if not value or len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        return None
    if value.startswith("/static/"):
        if not allow_relative_static:
            return None
        try:
            base = urlsplit(public_base_url)
        except ValueError:
            return None
        if base.path not in ("", "/") or base.query or base.fragment:
            return None
        value = urlunsplit((base.scheme, base.netloc, value, "", ""))
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        path = unquote(parsed.path)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or any(char.isspace() or ord(char) < 32 for char in path)
        or "\\" in path
        or not path.lower().endswith(_IMAGE_SUFFIXES)
        or any(part in (".", "..") for part in path.split("/"))
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal", ".invalid"))
    ):
        return None
    try:
        if not ip_address(host).is_global:
            return None
    except ValueError:
        if "." not in host or all(
            re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", part, re.I)
            for part in host.split(".")
        ):
            return None
    return value


def resolve_whatsapp_receipt_header(tenant_profile, *, public_base_url: str = "") -> str | None:
    """Select configured campaign/brand media without loading shared defaults.

    This validates a public-image URL shape, not remote availability or MIME.
    The caller supplies the tenant resolved from the authenticated provider
    sender and separately verifies the active messaging window.
    """
    tenant_id = getattr(tenant_profile, "id", None)
    if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id <= 0:
        return None
    config = getattr(tenant_profile, "configuracion", None)
    config = config if isinstance(config, dict) else {}
    header = config.get("whatsapp_receipt_header")
    if header is not None:
        if (
            not isinstance(header, dict)
            or set(header) not in ({"image_url"}, {"enabled", "image_url"})
            or header.get("enabled", True) is not True
        ):
            return None
        # A malformed explicit override cannot silently select another image
        # while the administrative read contract presents it as disabled.
        return public_whatsapp_receipt_image_url(
            header.get("image_url"),
        )
    candidates = [config.get("promo_image_url")]
    promo = config.get("promo_section")
    if isinstance(promo, dict):
        candidates.append(promo.get("image_url"))
    candidates.append(getattr(tenant_profile, "logo_url", None))
    for candidate in candidates:
        selected = public_whatsapp_receipt_image_url(
            candidate, public_base_url=public_base_url, allow_relative_static=True
        )
        if selected:
            return selected
    return None
