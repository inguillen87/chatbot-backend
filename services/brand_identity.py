"""Organization brand identity for public surfaces (widget, portal).

Stored under ``configuracion['brand_identity']`` and only published for an
active Full-plan organization. The frontend renders it generically: palette,
gradient stops, typography, logos, bubble style and avatar style. Values are
validated strictly; anything outside the contract is rejected on write and
ignored on read, so a bad record can never break a public widget.
"""
from __future__ import annotations

import math
import re
from copy import deepcopy

KEY = "brand_identity"
CONTRACT = "public.brand_identity.v1"
FIELDS = {"palette", "gradient_stops", "text_color", "typography", "logo", "bubble_style", "avatar"}
TYPOGRAPHY_FIELDS = {"family", "fallback", "stylesheet_url", "letter_spacing"}
LOGO_FIELDS = {"on_light", "on_dark", "mark", "alt"}
BUBBLE_STYLES = {"default", "globa"}
AVATAR_STYLES = {"chatboc_bot", "brand_bot"}

_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_FAMILY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]{0,59}")
_FALLBACK = re.compile(r"[A-Za-z0-9 ,\"'-]{1,160}")
_FONT_STYLESHEET = re.compile(r"https://fonts\.googleapis\.com/css2\?[A-Za-z0-9=&:;@,.+%_-]{1,400}")
_ASSET_URL = re.compile(r"https://[A-Za-z0-9.-]{1,120}(?::[0-9]{2,5})?/[A-Za-z0-9/._~%-]{1,380}")
_LETTER_SPACING = re.compile(r"-?(?:0?\.[0-9]{1,3}|0)em")


class BrandIdentityError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _hex(value, field):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise BrandIdentityError(f"brand_identity_{field}_invalid")
    return value.upper()


def _text(value, pattern, field, *, max_len):
    if not isinstance(value, str) or len(value) > max_len or not pattern.fullmatch(value.strip()):
        raise BrandIdentityError(f"brand_identity_{field}_invalid")
    return value.strip()


def _unit(value, field):
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
        raise BrandIdentityError(f"brand_identity_{field}_invalid")
    return round(float(value), 4)


def normalize(value) -> dict:
    """Return the canonical brand identity or raise ``BrandIdentityError``."""
    if not isinstance(value, dict) or not value or not set(value) <= FIELDS or "palette" not in value:
        raise BrandIdentityError("brand_identity_invalid")
    palette = value["palette"]
    if not isinstance(palette, list) or not 2 <= len(palette) <= 6:
        raise BrandIdentityError("brand_identity_palette_invalid")
    result = {"palette": [_hex(color, "palette") for color in palette]}

    if "gradient_stops" in value:
        stops = value["gradient_stops"]
        if not isinstance(stops, list) or not 2 <= len(stops) <= 6:
            raise BrandIdentityError("brand_identity_gradient_invalid")
        normalized_stops = []
        for stop in stops:
            if not isinstance(stop, dict) or set(stop) != {"color", "x", "y"}:
                raise BrandIdentityError("brand_identity_gradient_invalid")
            normalized_stops.append({"color": _hex(stop["color"], "gradient"),
                                     "x": _unit(stop["x"], "gradient"), "y": _unit(stop["y"], "gradient")})
        result["gradient_stops"] = normalized_stops

    if "text_color" in value:
        result["text_color"] = _hex(value["text_color"], "text_color")

    if "typography" in value:
        typography = value["typography"]
        if not isinstance(typography, dict) or "family" not in typography or not set(typography) <= TYPOGRAPHY_FIELDS:
            raise BrandIdentityError("brand_identity_typography_invalid")
        normalized = {"family": _text(typography["family"], _FAMILY, "typography", max_len=60)}
        if "fallback" in typography:
            normalized["fallback"] = _text(typography["fallback"], _FALLBACK, "typography", max_len=160)
        if "stylesheet_url" in typography:
            normalized["stylesheet_url"] = _text(typography["stylesheet_url"], _FONT_STYLESHEET, "typography", max_len=480)
        if "letter_spacing" in typography:
            normalized["letter_spacing"] = _text(typography["letter_spacing"], _LETTER_SPACING, "typography", max_len=12)
        result["typography"] = normalized

    if "logo" in value:
        logo = value["logo"]
        if not isinstance(logo, dict) or not logo or not set(logo) <= LOGO_FIELDS or set(logo) == {"alt"}:
            raise BrandIdentityError("brand_identity_logo_invalid")
        normalized = {}
        for field in ("on_light", "on_dark", "mark"):
            if field in logo:
                normalized[field] = _text(logo[field], _ASSET_URL, "logo", max_len=512)
        if "alt" in logo:
            alt = logo["alt"]
            if not isinstance(alt, str) or not 1 <= len(alt.strip()) <= 120 or any(ch in alt for ch in "<>"):
                raise BrandIdentityError("brand_identity_logo_invalid")
            normalized["alt"] = alt.strip()
        result["logo"] = normalized

    if "bubble_style" in value:
        if value["bubble_style"] not in BUBBLE_STYLES:
            raise BrandIdentityError("brand_identity_bubble_style_invalid")
        result["bubble_style"] = value["bubble_style"]

    if "avatar" in value:
        avatar = value["avatar"]
        if not isinstance(avatar, dict) or set(avatar) != {"style"} or avatar["style"] not in AVATAR_STYLES:
            raise BrandIdentityError("brand_identity_avatar_invalid")
        result["avatar"] = {"style": avatar["style"]}
    return result


def stored_brand_identity(tenant):
    config = getattr(tenant, "configuracion", None)
    if not isinstance(config, dict) or KEY not in config:
        return None
    try:
        return normalize(config[KEY])
    except BrandIdentityError:
        return None


def public_brand_identity(tenant):
    """The published identity, only for an active Full-plan organization."""
    from services.plan_access import tenant_allows_workspace_branding

    if tenant_allows_workspace_branding(tenant) is not True:
        return None
    identity = stored_brand_identity(tenant)
    if identity is None:
        return None
    return {"contract_version": CONTRACT, **deepcopy(identity)}
