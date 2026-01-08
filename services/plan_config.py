"""Centralized subscription plan configuration and helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class PlanMetadata:
    """Metadata describing a subscription plan."""

    key: str
    name: str
    price_ars: Optional[int]
    order: int
    message_limit: Optional[int]
    summary: str
    features: List[str] = field(default_factory=list)
    technologies: List[str] = field(default_factory=list)
    preapproval_plan_id: Optional[str] = None
    badge: Optional[str] = None
    highlight: Optional[str] = None
    cta_label: Optional[str] = None
    public: bool = True

    def to_public_dict(self) -> Dict[str, object]:
        """Return a sanitized dictionary representation for API responses."""

        price_formatted = format_price(self.price_ars)
        limit_label = format_limit(self.message_limit)
        return {
            "key": self.key,
            "name": self.name,
            "price_ars": self.price_ars,
            "price_formatted": price_formatted,
            "message_limit": self.message_limit,
            "message_limit_formatted": limit_label,
            "summary": self.summary,
            "features": list(self.features),
            "technologies": list(self.technologies),
            "preapproval_plan_id": self.preapproval_plan_id,
            "badge": self.badge,
            "highlight": self.highlight,
            "cta_label": self.cta_label,
        }


def format_price(value: Optional[int]) -> str:
    """Return an ARS-friendly string for the provided price."""

    if value is None:
        return "Consultar"
    if value == 0:
        return "Sin costo"
    return f"${value:,.0f}".replace(",", ".")


def format_limit(limit: Optional[int]) -> str:
    """Human readable label for the interaction limit."""

    if limit is None:
        return "Interacciones ilimitadas"
    return f"Hasta {limit} interacciones/mes"


# --- Plan Catalog ---------------------------------------------------------

_PLAN_CATALOG: Dict[str, PlanMetadata] = {
    "gratis": PlanMetadata(
        key="gratis",
        name="Plan Demo",
        price_ars=0,
        order=2,
        message_limit=50,
        summary="Ideal para probar el asistente con un catálogo reducido y chats de prueba.",
        features=[
            "50 interacciones mensuales en el webchat",
            "Flujos base de reclamos y pedidos",
            "Exportación de leads en CSV",
        ],
        technologies=[
            "Chat web responsivo",
            "Panel de métricas básico",
            "Embeddings Qdrant para catálogo demo",
        ],
        badge="demo",
        highlight="Perfecto para testear",
        cta_label="Hablar con ventas",
    ),
    "pro": PlanMetadata(
        key="pro",
        name="Plan Pro",
        price_ars=300_000,
        order=1,
        message_limit=250,
        summary="Automatización comercial y de soporte con 250 interacciones inteligentes al mes.",
        features=[
            "250 mensajes IA por mes entre WhatsApp, webchat y email",
            "Campañas segmentadas y CRM sincronizado",
            "Carga de catálogos PDF/Excel con respuesta inmediata",
            "Agendamiento de visitas y recordatorios automáticos",
        ],
        technologies=[
            "WhatsApp Business oficial",
            "Embeddings vectoriales + búsquedas semánticas",
            "Procesamiento de documentos (PDF, Excel, imágenes)",
            "Dashboards en tiempo real para ventas y soporte",
        ],
        preapproval_plan_id="2c9380849764e81a01976585767f0040",
        badge="popular",
        highlight="Incluye marketing automatizado",
        cta_label="Suscribirme al Pro",
    ),
    "full": PlanMetadata(
        key="full",
        name="Plan Full",
        price_ars=350_000,
        order=0,
        message_limit=None,
        summary="Interacciones ilimitadas y todo el stack omnicanal para escalar operaciones.",
        features=[
            "Mensajes ilimitados y campañas omnicanal",
            "Integraciones API + workflows avanzados",
            "Atención multimodal con voz, imagen y video",
            "Dashboards ejecutivos y reportes automatizados",
        ],
        technologies=[
            "LLM orquestado con herramientas propietarias",
            "Visión computarizada y análisis multimodal",
            "Speech-to-text y text-to-speech en tiempo real",
            "Webhooks y API de eventos para integraciones externas",
        ],
        preapproval_plan_id="2c9380849763daeb0197658791ee00b1",
        badge="ilimitado",
        highlight="Stack completo de IA",
        cta_label="Quiero el Full",
    ),
}


MERCADOPAGO_PLAN_LOOKUP: Dict[str, str] = {
    meta.preapproval_plan_id: meta.key
    for meta in _PLAN_CATALOG.values()
    if meta.preapproval_plan_id
}
"""Map Mercado Pago preapproval IDs to internal plan keys."""


def get_plan_metadata(plan_key: Optional[str]) -> Optional[PlanMetadata]:
    """Return metadata for the requested plan."""

    if not plan_key:
        return None
    normalized = normalize_plan_key(plan_key)
    return _PLAN_CATALOG.get(normalized)


def normalize_plan_key(plan_key: Optional[str]) -> str:
    """Normalize plan keys from legacy aliases to catalog keys."""

    if not plan_key:
        return ""

    normalized = str(plan_key).strip().lower()
    if normalized in {"free", "plan_free", "plan_gratis"}:
        return "gratis"
    if normalized in {"full", "plan_full"}:
        return "full"
    if normalized in {"pro", "plan_pro"}:
        return "pro"
    return normalized


def serialize_plan_catalog(public_only: bool = True) -> List[Dict[str, object]]:
    """Return the list of plans sorted by the configured order."""

    plans: Iterable[PlanMetadata]
    plans = sorted(_PLAN_CATALOG.values(), key=lambda meta: meta.order)
    if public_only:
        plans = [plan for plan in plans if plan.public]
    return [plan.to_public_dict() for plan in plans]


def serialize_plan_for_response(plan: PlanMetadata | str | None) -> Dict[str, object]:
    """Helper used by API routes to include plan metadata in responses."""

    if isinstance(plan, PlanMetadata):
        meta = plan
    else:
        meta = get_plan_metadata(plan)
    if not meta:
        return {}
    payload = meta.to_public_dict()
    payload.update(
        {
            "is_unlimited": meta.message_limit is None,
            "price_label": payload["price_formatted"],
            "limit_label": payload["message_limit_formatted"],
        }
    )
    return payload


def apply_plan_to_user(
    user,
    plan_key: str,
    *,
    status: Optional[str] = None,
    preapproval_id: Optional[str] = None,
):
    """Assign the given plan to the user updating limits and metadata.

    Returns the applied :class:`PlanMetadata` for convenience.
    """

    normalized_plan_key = normalize_plan_key(plan_key)
    metadata = get_plan_metadata(normalized_plan_key)
    if metadata is None:
        raise ValueError(f"Plan desconocido: {plan_key}")

    normalized_key = metadata.key
    setattr(user, "plan", normalized_key)
    if hasattr(user, "preapproval_id"):
        setattr(user, "preapproval_id", preapproval_id)
    if hasattr(user, "plan_status"):
        setattr(user, "plan_status", status)

    limit = metadata.message_limit
    # Respetar None para indicar ilimitado.
    if hasattr(user, "limite_preguntas"):
        setattr(user, "limite_preguntas", limit)
    if limit is None and getattr(user, "preguntas_usadas", None) is None:
        setattr(user, "preguntas_usadas", 0)

    return metadata


__all__ = [
    "PlanMetadata",
    "MERCADOPAGO_PLAN_LOOKUP",
    "apply_plan_to_user",
    "format_limit",
    "format_price",
    "get_plan_metadata",
    "normalize_plan_key",
    "serialize_plan_catalog",
    "serialize_plan_for_response",
]
