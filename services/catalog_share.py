from __future__ import annotations

import re
from typing import Any, Dict, Optional

from flask import current_app, request

from services.pymes import tiene_archivo_catalogo


CATALOG_INTENT_PATTERNS = [
    re.compile(r"\bcat[aá]logo\b", re.IGNORECASE),
    re.compile(r"\bcat[aá]logo\s+completo\b", re.IGNORECASE),
    re.compile(r"\bcat[aá]logo\s+entero\b", re.IGNORECASE),
    re.compile(r"\bmandame\s+el\s+pdf\b", re.IGNORECASE),
    re.compile(r"\bdescargar\s+cat[aá]logo\b", re.IGNORECASE),
    re.compile(r"\bver\s+todo(s|as)?\s+los\s+(productos|beneficios)\b", re.IGNORECASE),
    re.compile(r"\blista\s+de\s+precios\b", re.IGNORECASE),
    re.compile(r"\btarifario\b", re.IGNORECASE),
]


def _extract_text(pregunta: Any) -> str:
    if isinstance(pregunta, dict):
        texto = pregunta.get("texto") or pregunta.get("message") or ""
        return str(texto)
    return str(pregunta or "")


def is_catalog_share_intent(pregunta: Any) -> bool:
    text = _extract_text(pregunta).strip().lower()
    if not text:
        return False
    return any(pattern.search(text) for pattern in CATALOG_INTENT_PATTERNS)


def _build_portal_url(tenant_slug: str) -> str:
    base_url = None
    if current_app:
        base_url = current_app.config.get("APP_BASE_URL")
    if not base_url and request:
        base_url = request.url_root.rstrip("/")
    if not base_url:
        base_url = "https://chatboc.ar"
    return f"{base_url}/{tenant_slug}"


def _build_catalog_view_url(tenant_slug: str) -> str:
    base_url = _build_portal_url(tenant_slug)
    return f"{base_url}/catalogo"


def _build_catalog_download_url(tenant_slug: str, fmt: str = "pdf") -> str:
    api_base = None
    if current_app:
        api_base = current_app.config.get("API_BASE_URL")
    if not api_base and request:
        api_base = request.url_root.rstrip("/")
    if not api_base:
        api_base = "https://api.chatboc.ar"
    return f"{api_base}/api/public/tenants/{tenant_slug}/catalog/download?format={fmt}"


def build_catalog_share_payload(
    owner_user: Any,
    channel: str | None = None,
) -> Optional[Dict[str, Any]]:
    if not owner_user:
        return None

    tenant_slug = getattr(owner_user, "tenant_slug", None)
    if not tenant_slug:
        return None

    view_url = _build_catalog_view_url(tenant_slug)

    download_url = _build_catalog_download_url(tenant_slug)
    download_url_json = _build_catalog_download_url(tenant_slug, fmt="json")
    has_pdf = bool(owner_user.id and tiene_archivo_catalogo(owner_user.id))

    nombre_tenant = (
        getattr(owner_user, "nombre_empresa", None)
        or getattr(owner_user, "name", None)
        or tenant_slug
    )
    message_body = (
        f"Acá tenés el catálogo completo de {nombre_tenant}.\n"
        f"Ver online: {view_url}\n"
        f"Descargar PDF: {download_url}"
    )

    options_list = [
        {"texto": "Ver online", "url": view_url},
        {"texto": "Descargar PDF", "url": download_url},
    ]

    return {
        "message_body": message_body,
        "options_list": options_list,
        "message_type": "interactive_buttons",
        "data": {
            "catalog_share": {
                "title": nombre_tenant,
                "text": message_body,
                "view_url": view_url,
                "download_url": download_url,
                "download_url_json": download_url_json,
                "also_send_pdf_as_media": bool(has_pdf and channel == "whatsapp"),
            }
        },
        "fuente": "catalog_share_intent",
    }


def maybe_handle_catalog_share(
    pregunta: Any,
    owner_user: Any,
    channel: str | None = None,
) -> Optional[Dict[str, Any]]:
    if not is_catalog_share_intent(pregunta):
        return None
    return build_catalog_share_payload(owner_user, channel=channel)
