from __future__ import annotations

from copy import deepcopy
from typing import Any


DEMO_PILLAR_CONTRACT_VERSION = "demo.pillars.v1"


_PILLARS: list[dict[str, Any]] = [
    {
        "key": "educacion",
        "label": "Colegios",
        "description": "Experiencias para colegios públicos, privados e institutos.",
        "default_rubro": "colegios",
        "default_tipo_chat": "pyme",
        "vertical": "educacion",
        "hero_prompt": "Elegí el tipo de institución para iniciar una conversación escolar.",
        "categories": [
            {
                "slug": "colegios",
                "label": "Colegios e instituciones educativas",
                "description": "Atención escolar para familias: inasistencias, oficina escolar, comunicados, pagos, certificados y casos con seguimiento.",
                "tipo_chat": "pyme",
                "vertical": "educacion",
                "subvertical": "colegio_general",
                "sample_prompts": [
                    "Quiero justificar una inasistencia.",
                    "Necesito consultar admisiones.",
                    "Te mando una foto del certificado médico.",
                ],
                "resources": [
                    {
                        "id": "catalogo_demo_colegios",
                        "label": "Catálogo demo colegios",
                        "kind": "pdf",
                        "url": "/api/v2/demo/catalog-assets/colegios/catalogo-demo-colegios.pdf",
                    },
                    {
                        "id": "lista_servicios_colegios",
                        "label": "Servicios y precios demo",
                        "kind": "pdf",
                        "url": "/api/v2/demo/catalog-assets/colegios/lista-precios-demo.pdf",
                    },
                ],
            },
            {
                "slug": "colegio_privado",
                "label": "Colegio privado",
                "tipo_chat": "pyme",
                "vertical": "educacion",
                "subvertical": "colegio_privado",
            },
            {
                "slug": "colegio_publico",
                "label": "Colegio público",
                "tipo_chat": "pyme",
                "vertical": "educacion",
                "subvertical": "colegio_publico",
            },
            {
                "slug": "instituto_capacitacion",
                "label": "Instituto o academia",
                "tipo_chat": "pyme",
                "vertical": "educacion",
                "subvertical": "instituto",
            },
        ],
    },
    {
        "key": "gobierno",
        "label": "Gobiernos",
        "description": "Atención ciudadana, reclamos, trámites, campañas y gestión pública.",
        "default_rubro": "municipio",
        "default_tipo_chat": "municipio",
        "vertical": "gobierno",
        "hero_prompt": "Elegí un organismo o campaña para simular atención ciudadana.",
        "categories": [
            {
                "slug": "municipio",
                "label": "Municipio",
                "tipo_chat": "municipio",
                "vertical": "gobierno",
                "subvertical": "municipio",
                "sample_prompts": [
                    "Quiero iniciar un reclamo por alumbrado.",
                    "Necesito saber cómo renovar la licencia.",
                    "Te mando la ubicación del problema.",
                ],
                "resources": [
                    {
                        "id": "catalogo_demo_gobiernos",
                        "label": "Guía demo gobiernos",
                        "kind": "pdf",
                        "url": "/api/v2/demo/catalog-assets/gobiernos/catalogo-demo-gobiernos.pdf",
                    },
                    {
                        "id": "lista_servicios_gobiernos",
                        "label": "Trámites y servicios demo",
                        "kind": "pdf",
                        "url": "/api/v2/demo/catalog-assets/gobiernos/lista-precios-demo.pdf",
                    },
                ],
            },
            {
                "slug": "concejo_deliberante",
                "label": "Concejo deliberante",
                "tipo_chat": "municipio",
                "vertical": "gobierno",
                "subvertical": "concejo_deliberante",
            },
            {
                "slug": "legislador",
                "label": "Legislador o diputado",
                "tipo_chat": "municipio",
                "vertical": "gobierno",
                "subvertical": "legislativo",
            },
            {
                "slug": "campana_electoral",
                "label": "Campaña electoral",
                "tipo_chat": "municipio",
                "vertical": "gobierno",
                "subvertical": "campana_electoral",
            },
        ],
    },
    {
        "key": "empresas",
        "label": "Empresas",
        "description": "Ventas, soporte, pedidos, catálogos y seguimiento comercial.",
        "default_rubro": "local_comercial_general",
        "default_tipo_chat": "pyme",
        "vertical": "pyme",
        "hero_prompt": "Elegí un rubro comercial para probar ventas y soporte con IA.",
        "categories": [
            {
                "slug": "local_comercial_general",
                "label": "Comercio general",
                "tipo_chat": "pyme",
                "vertical": "pyme",
                "subvertical": "retail",
                "sample_prompts": [
                    "Quiero ver catálogo y precios.",
                    "Necesito armar un pedido con envío.",
                    "¿Qué promociones tienen esta semana?",
                ],
                "resources": [
                    {
                        "id": "catalogo_demo_empresas",
                        "label": "Catálogo demo empresas",
                        "kind": "pdf",
                        "url": "/api/v2/demo/catalog-assets/empresas/catalogo-demo-empresas.pdf",
                    },
                    {
                        "id": "lista_precios_empresas",
                        "label": "Lista de precios demo",
                        "kind": "pdf",
                        "url": "/api/v2/demo/catalog-assets/empresas/lista-precios-demo.pdf",
                    },
                ],
            },
            {"slug": "bodega", "label": "Bodega", "tipo_chat": "pyme", "vertical": "pyme", "subvertical": "bebidas"},
            {"slug": "ferreteria", "label": "Ferretería", "tipo_chat": "pyme", "vertical": "pyme", "subvertical": "retail"},
            {"slug": "inmobiliaria", "label": "Inmobiliaria", "tipo_chat": "pyme", "vertical": "pyme", "subvertical": "servicios"},
            {"slug": "medico_general", "label": "Clínica o consultorio", "tipo_chat": "pyme", "vertical": "pyme", "subvertical": "salud"},
            {"slug": "seguros", "label": "Seguros", "tipo_chat": "pyme", "vertical": "pyme", "subvertical": "servicios"},
            {"slug": "logistica", "label": "Logística", "tipo_chat": "pyme", "vertical": "pyme", "subvertical": "operaciones"},
        ],
    },
]


_CATEGORY_LABELS_BY_SLUG = {
    str(category["slug"]): str(category["label"])
    for pillar in _PILLARS
    for category in pillar.get("categories") or []
}
_CATEGORY_LABELS_BY_SLUG.update(
    {
        "gobiernos_y_municipios": "Gobiernos y municipios",
        "legislador_o_diputado": "Legislador o diputado",
    }
)


_SECTOR_ALIASES = {
    "colegio": "educacion",
    "colegios": "educacion",
    "colegios_e_instituciones_educativas": "educacion",
    "educacion": "educacion",
    "education": "educacion",
    "escuela": "educacion",
    "escuelas": "educacion",
    "instituto": "educacion",
    "soluciones_para_colegios": "educacion",
    "soluciones_para_educacion": "educacion",
    "gobierno": "gobierno",
    "gobiernos": "gobierno",
    "municipio": "gobierno",
    "municipios": "gobierno",
    "publico": "gobierno",
    "sector_publico": "gobierno",
    "soluciones_para_sector_publico": "gobierno",
    "soluciones_para_gobiernos": "gobierno",
    "empresas": "empresas",
    "empresa": "empresas",
    "pyme": "empresas",
    "pymes": "empresas",
    "comercio": "empresas",
    "comercios": "empresas",
    "negocio": "empresas",
    "soluciones_para_empresas": "empresas",
}


def normalize_demo_sector(value: object) -> str:
    text = _slug(value)
    return _SECTOR_ALIASES.get(text, text)


def demo_pillars() -> list[dict[str, Any]]:
    return deepcopy(_PILLARS)


def demo_pillar_keys() -> list[str]:
    return [pillar["key"] for pillar in _PILLARS]


def default_rubro_for_sector(sector: str) -> str:
    normalized = normalize_demo_sector(sector)
    for pillar in _PILLARS:
        if pillar["key"] == normalized:
            return str(pillar["default_rubro"])
    return "local_comercial_general"


def sector_for_rubro(rubro: object) -> str | None:
    slug = _slug(rubro)
    if not slug:
        return None
    for pillar in _PILLARS:
        if slug == pillar["key"]:
            return str(pillar["key"])
        if slug == _slug(pillar.get("default_rubro")):
            return str(pillar["key"])
        for category in pillar.get("categories") or []:
            if slug == _slug(category.get("slug")):
                return str(pillar["key"])
    return None


def category_for_rubro(rubro: object) -> dict[str, Any] | None:
    slug = _slug(rubro)
    if not slug:
        return None
    for pillar in _PILLARS:
        for category in pillar.get("categories") or []:
            if slug == _slug(category.get("slug")):
                payload = deepcopy(category)
                payload.setdefault("pillar", pillar["key"])
                payload.setdefault("sector", pillar["key"])
                return payload
    return None


def category_label_for_rubro(rubro: object) -> str | None:
    """Return curated display copy without deriving labels from identifiers."""

    return _CATEGORY_LABELS_BY_SLUG.get(_slug(rubro))


def catalog_resources_for_rubro(rubro: object, sector: str | None = None) -> list[dict[str, Any]]:
    category = category_for_rubro(rubro)
    if category and isinstance(category.get("resources"), list):
        return deepcopy(category["resources"])

    default_rubro = default_rubro_for_sector(sector or sector_for_rubro(rubro) or "empresas")
    category = category_for_rubro(default_rubro)
    if category and isinstance(category.get("resources"), list):
        return deepcopy(category["resources"])
    return []


def curated_demo_rubros() -> list[dict[str, Any]]:
    rubros: list[dict[str, Any]] = []
    for pillar in _PILLARS:
        for category in pillar.get("categories") or []:
            rubros.append(
                {
                    "slug": category.get("slug"),
                    "key": category.get("slug"),
                    "label": category.get("label"),
                    "tipo_chat": category.get("tipo_chat") or pillar.get("default_tipo_chat"),
                    "tenant_slug": category.get("tenant_slug") or category.get("slug"),
                    "vertical": category.get("vertical") or pillar.get("vertical"),
                    "subvertical": category.get("subvertical"),
                    "sector": pillar.get("key"),
                    "pillar": pillar.get("key"),
                    "resources": deepcopy(category.get("resources") or []),
                    "sample_prompts": list(category.get("sample_prompts") or []),
                }
            )
    return rubros


def _slug(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("slug") or value.get("key") or value.get("id") or value.get("label")
    text = str(value or "").strip().lower()
    if not text:
        return ""
    replacements = (
        ("á", "a"),
        ("é", "e"),
        ("í", "i"),
        ("ó", "o"),
        ("ú", "u"),
        ("ñ", "n"),
    )
    for source, target in replacements:
        text = text.replace(source, target)
    return text.replace("-", "_").replace(" ", "_")
