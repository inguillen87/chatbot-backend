"""
Handles the flow for "trámites" (procedures).
"""
import time
from collections.abc import Mapping
from typing import Any

from services.google_search import google_search

CACHE = {}
CACHE_TTL = 3600  # 1 hour


def _value(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None) if source is not None else None


def _clean_label(value: Any, *, max_length: int = 160) -> str:
    return " ".join(str(value or "").split())[:max_length]


def _municipality_identity(ctx: Mapping[str, Any]) -> tuple[str, str]:
    """Return a tenant-scoped cache key and a configured search label.

    Municipality names must come from the resolved tenant context.  A neutral
    query is safer than silently applying Junin's identity to a different
    government, and the cache scope prevents one tenant's search result from
    being replayed to another tenant that asked the same question.
    """

    config = ctx.get("municipio_config_actual")
    if not isinstance(config, Mapping):
        config = ctx.get("municipio_config")
    if not isinstance(config, Mapping):
        config = {}

    tenant = ctx.get("tenant_profile") or ctx.get("tenant")
    user = ctx.get("user_obj")

    raw_tenant = ctx.get("tenant")
    tenant_slug = _clean_label(
        ctx.get("tenant_slug")
        or (raw_tenant if isinstance(raw_tenant, str) else None)
    )
    if not tenant_slug:
        tenant_slug = _clean_label(
            _value(tenant, "slug")
            or config.get("tenant_slug")
            or config.get("slug")
            or _value(user, "tenant_slug")
        )

    configured_name = _clean_label(
        config.get("nombre_municipio")
        or config.get("nombre")
        or config.get("municipality_name")
        or config.get("display_name")
    )
    tenant_name = _clean_label(_value(tenant, "nombre") or _value(tenant, "name"))
    owner_name = _clean_label(_value(user, "nombre_empresa"))

    city = _clean_label(config.get("ciudad") or config.get("city"))
    province = _clean_label(config.get("provincia") or config.get("province"))
    locality = ", ".join(part for part in (city, province) if part)

    label = configured_name or tenant_name or owner_name or locality
    scope_parts = [part.casefold() for part in (tenant_slug, label) if part]
    scope = "::".join(scope_parts) if scope_parts else "unscoped"
    return scope, label

def handle(msg: str, ctx: dict):
    """
    Handles the conversation flow for procedures.
    """
    # The `msg` is the raw text or the action_id, which we can use as the query.
    tramite_query = _clean_label(msg.lower().replace('_', ' '), max_length=240)
    tenant_scope, municipality_label = _municipality_identity(ctx or {})
    cache_key = (tenant_scope, tramite_query)

    # Check cache first
    cached_result = CACHE.get(cache_key)
    if cached_result and (time.time() - cached_result['timestamp']) < CACHE_TTL:
        search_results = cached_result['results']
    else:
        full_query = f"tramite {tramite_query}"
        if municipality_label:
            full_query += f" en {municipality_label}"
        search_results = google_search(full_query)
        if search_results:
            CACHE[cache_key] = {'results': search_results, 'timestamp': time.time()}

    if not search_results:
        return {
            "message_body": f"No encontré información sobre el trámite '{tramite_query}'.",
            "message_type": "text", "options_list": []
        }

    top_result = search_results[0]

    return {
        "message_body": (
            f"Encontré esto sobre '{tramite_query}':\n\n"
            f"**{top_result.get('title')}**\n"
            f"{top_result.get('snippet')}\n\n"
            f"Podés ver más en: {top_result.get('link')}"
        ),
        "message_type": "text",
        "options_list": [{"texto": "Ver en la web", "url": top_result.get('link')}]
    }
