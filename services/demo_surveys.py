from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

from services.demo_pillar_catalog import normalize_demo_sector


DEMO_SURVEY_CONTRACT_VERSION = "demo.surveys_votings.v1"
DEMO_SURVEY_PAGE_SIZE = 5
DEMO_SURVEY_RESPONSE_COUNT = 100
_SUPPORTED_SECTORS = {"gobierno", "educacion", "empresas"}


def _slug_part(value: Any, fallback: str = "demo") -> str:
    text = str(value or "").strip().lower()
    cleaned = []
    previous_dash = False
    for char in text:
        if char.isalnum():
            cleaned.append(char)
            previous_dash = False
        elif not previous_dash:
            cleaned.append("-")
            previous_dash = True
    slug = "".join(cleaned).strip("-")
    return slug or fallback


def _hash_int(value: str, digits: int = 10) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:digits], 16)


def _rng(*parts: Any) -> random.Random:
    seed = "|".join(str(part or "") for part in parts)
    return random.Random(_hash_int(seed, digits=8))


def _split_counts(total: int, labels: list[str], seed_key: str) -> list[dict[str, Any]]:
    if not labels:
        return []
    rng = _rng(seed_key)
    weights = [rng.randint(8, 40) for _ in labels]
    weight_total = sum(weights) or 1
    counts = [max(1, int(total * weight / weight_total)) for weight in weights]
    diff = total - sum(counts)
    index = 0
    while diff:
        pos = index % len(counts)
        if diff > 0:
            counts[pos] += 1
            diff -= 1
        elif counts[pos] > 1:
            counts[pos] -= 1
            diff += 1
        index += 1
    return [{"label": label, "count": count} for label, count in zip(labels, counts)]


def _templates_for_sector(sector: str) -> list[dict[str, Any]]:
    normalized = normalize_demo_sector(sector)
    if normalized == "educacion":
        return [
            {
                "id": "talleres-extra",
                "tipo": "votacion",
                "titulo": "Votacion de talleres extracurriculares",
                "descripcion": "Familias eligen que talleres conviene abrir primero.",
                "pregunta": "Que taller deberia abrir el colegio primero?",
                "opciones": ["Robotica", "Arte", "Deportes", "Ingles conversacional"],
            },
            {
                "id": "comunicacion-familias",
                "tipo": "encuesta",
                "titulo": "Encuesta de comunicacion con familias",
                "descripcion": "Mide canales preferidos y claridad de comunicados.",
                "pregunta": "Como preferis recibir los comunicados?",
                "opciones": ["WhatsApp", "Email", "App escolar", "Cuaderno digital"],
            },
            {
                "id": "biblioteca-horarios",
                "tipo": "votacion",
                "titulo": "Votacion de horario de biblioteca",
                "descripcion": "Consulta para extender servicios de biblioteca.",
                "pregunta": "Que horario seria mas util?",
                "opciones": ["Antes de clases", "Mediodia", "Despues de clases", "Sabados"],
            },
            {
                "id": "comedor-escolar",
                "tipo": "encuesta",
                "titulo": "Encuesta de comedor escolar",
                "descripcion": "Releva experiencia de familias con menu y organizacion.",
                "pregunta": "Que aspecto conviene mejorar primero?",
                "opciones": ["Menu", "Horarios", "Comunicacion", "Dietas especiales"],
            },
            {
                "id": "salida-educativa",
                "tipo": "votacion",
                "titulo": "Votacion de salida educativa",
                "descripcion": "Prioriza propuestas para la proxima salida escolar.",
                "pregunta": "Que salida preferis para el curso?",
                "opciones": ["Museo", "Reserva natural", "Teatro", "Universidad"],
            },
            {
                "id": "convivencia-escolar",
                "tipo": "encuesta",
                "titulo": "Encuesta de convivencia escolar",
                "descripcion": "Recoge percepciones anonimas para orientar acciones.",
                "pregunta": "Que accion ayudaria mas a la convivencia?",
                "opciones": ["Talleres", "Mediacion", "Charlas", "Mas tutorias"],
            },
        ]
    if normalized == "gobierno":
        return [
            {
                "id": "votacion-gestion-tdf",
                "tipo": "votacion",
                "titulo": "Votacion Ciudadana: Conectividad y Polos de Innovacion TDF",
                "descripcion": "Consulta publica sobre la expansion de fibra optica y polos tecnologicos en Ushuaia, Rio Grande y Tolhuin (Gestion Provincial TDF).",
                "pregunta": "Esta de acuerdo con priorizar la inversion provincial en conectividad digital y centros de innovacion?",
                "opciones": ["Si, totalmente prioritario", "No, priorizar otras areas"],
            },
            {
                "id": "encuesta-innovacion-tdf",
                "tipo": "encuesta",
                "titulo": "Encuesta Provincial: Transformacion Digital y Tramites Publicos TDF",
                "descripcion": "Relevamiento de la Agencia de Innovacion de Tierra del Fuego sobre digitalizacion de tramites, conectividad y atencion ciudadana.",
                "pregunta": "Que servicio publico digital considera mas urgente optimizar en su ciudad?",
                "opciones": ["Tramites provinciales 100% online", "Conectividad escolar y comunitaria", "Capacitaciones y Polos Tecnologicos", "Turnos de salud y hospitales"],
            },
            {
                "id": "encuesta-obras-tdf",
                "tipo": "votacion",
                "titulo": "Consulta de Obras e Infraestructura: Ushuaia y Rio Grande",
                "descripcion": "Priorizacion de infraestructura urbana y desarrollo productivo en Tierra del Fuego.",
                "pregunta": "Cual es la obra de infraestructura mas necesaria para su localidad?",
                "opciones": ["Ruta y accesos viales", "Infraestructura portuaria y logistica", "Vivienda y habitat sostenible", "Centros de formacion tecnologica"],
            },
            {
                "id": "prioridades-barriales",
                "tipo": "votacion",
                "titulo": "Votacion de prioridades barriales",
                "descripcion": "Vecinos priorizan reclamos y obras de los proximos 90 dias.",
                "pregunta": "Que tema deberia resolverse primero?",
                "opciones": ["Luminarias", "Bacheo", "Limpieza", "Espacios verdes"],
            },
            {
                "id": "servicios-municipales",
                "tipo": "encuesta",
                "titulo": "Encuesta de servicios municipales",
                "descripcion": "Mide satisfaccion por canal, zona y categoria de servicio.",
                "pregunta": "Como evaluas la atencion municipal?",
                "opciones": ["Muy buena", "Buena", "Regular", "Mala"],
            },
            {
                "id": "obras-90-dias",
                "tipo": "votacion",
                "titulo": "Consulta de obras a 90 dias",
                "descripcion": "Ordena obras chicas de alto impacto ciudadano.",
                "pregunta": "Que obra deberia avanzar antes?",
                "opciones": ["Veredas", "Plazas", "Desagues", "Senalizacion"],
            },
            {
                "id": "atencion-ciudadana",
                "tipo": "encuesta",
                "titulo": "Encuesta de atencion ciudadana",
                "descripcion": "Analiza tiempos de respuesta y claridad del seguimiento.",
                "pregunta": "Que canal te resulto mas util?",
                "opciones": ["WhatsApp", "Web", "Telefono", "Presencial"],
            },
            {
                "id": "presupuesto-participativo",
                "tipo": "votacion",
                "titulo": "Votacion de presupuesto participativo",
                "descripcion": "Simula seleccion de proyectos con resultados en vivo.",
                "pregunta": "Que proyecto deberia financiarse?",
                "opciones": ["Playon deportivo", "Iluminacion", "Punto verde", "Centro vecinal"],
            },
            {
                "id": "espacios-verdes",
                "tipo": "encuesta",
                "titulo": "Encuesta de espacios verdes",
                "descripcion": "Detecta zonas con mayor demanda de mantenimiento.",
                "pregunta": "Que mejora esperas en plazas?",
                "opciones": ["Juegos", "Limpieza", "Seguridad", "Arbolado"],
            },
        ]
    return [
        {
            "id": "preferencias-productos",
            "tipo": "encuesta",
            "titulo": "Encuesta de preferencias de productos",
            "descripcion": "Clientes indican que productos quieren ver primero.",
            "pregunta": "Que linea te interesa mas?",
            "opciones": ["Promos", "Novedades", "Combos", "Reposicion frecuente"],
        },
        {
            "id": "promo-semana",
            "tipo": "votacion",
            "titulo": "Votacion de promo de la semana",
            "descripcion": "La comunidad elige una promo para activar en demo.",
            "pregunta": "Que promo preferis?",
            "opciones": ["2x1", "Envio bonificado", "Descuento por cantidad", "Combo premium"],
        },
        {
            "id": "experiencia-compra",
            "tipo": "encuesta",
            "titulo": "Encuesta de experiencia de compra",
            "descripcion": "Releva atencion, entrega y claridad de catalogo.",
            "pregunta": "Que punto conviene mejorar?",
            "opciones": ["Catalogo", "Atencion", "Entrega", "Pagos"],
        },
        {
            "id": "entrega-envio",
            "tipo": "encuesta",
            "titulo": "Consulta de entrega y envio",
            "descripcion": "Mide horarios, zonas y preferencias de entrega.",
            "pregunta": "Como preferis recibir tu pedido?",
            "opciones": ["Retiro", "Envio en el dia", "Envio programado", "Coordinar por WhatsApp"],
        },
        {
            "id": "nuevo-producto",
            "tipo": "votacion",
            "titulo": "Votacion de nuevo producto",
            "descripcion": "Ayuda a decidir que producto sumar al catalogo.",
            "pregunta": "Que deberia entrar al catalogo?",
            "opciones": ["Linea economica", "Linea premium", "Accesorios", "Pack regalo"],
        },
        {
            "id": "atencion-ventas",
            "tipo": "encuesta",
            "titulo": "Encuesta de atencion de ventas",
            "descripcion": "Evalua calidad de respuesta comercial y seguimiento.",
            "pregunta": "Que tan clara fue la atencion?",
            "opciones": ["Excelente", "Buena", "Regular", "Necesito hablar con alguien"],
        },
    ]


def _sector_labels(sector: str) -> dict[str, str]:
    normalized = normalize_demo_sector(sector)
    if normalized == "educacion":
        return {
            "heading": "Encuestas y votaciones escolares",
            "menu_label": "Encuestas y votaciones",
            "menu_description": "Participa como familia y mira resultados demo con 100 respuestas.",
            "back_action": "menu_colegio",
        }
    if normalized == "gobierno":
        return {
            "heading": "Encuestas y votaciones ciudadanas",
            "menu_label": "Encuestas y votaciones",
            "menu_description": "Prueba sondeos ciudadanos con resultados y mapas demo.",
            "back_action": "menu_principal",
        }
    return {
        "heading": "Encuestas y votaciones de clientes",
        "menu_label": "Encuestas y votaciones",
        "menu_description": "Prueba feedback comercial sin confirmar ventas reales.",
        "back_action": "menu_principal",
    }


def _geo_labels(sector: str, tenant_slug: str = "") -> list[str]:
    normalized = normalize_demo_sector(sector)
    slug = _slug_part(tenant_slug)
    if "tdf" in slug or "tierra" in slug or "ushuaia" in slug or "fuego" in slug:
        return ["Ushuaia Centro", "Rio Grande Industrial", "Tolhuin", "Andorra", "Margen Sur"]
    if normalized == "educacion":
        return ["Inicial", "Primaria", "Secundaria", "Familias nuevas", "Egresados"]
    if normalized == "gobierno":
        return ["Centro", "Norte", "Este", "Oeste", "Barrios rurales"]
    return ["Mostrador", "WhatsApp", "Tienda online", "Mayoristas", "Recurrentes"]


def _coordinate_base(sector: str, tenant_slug: str) -> tuple[float, float]:
    normalized = normalize_demo_sector(sector)
    slug = _slug_part(tenant_slug)
    if "tdf" in slug or "tierra" in slug or "ushuaia" in slug or "fuego" in slug or "melella" in slug:
        return -54.8019, -68.3030  # Ushuaia / Tierra del Fuego
    if "rio-grande" in slug or "riogrande" in slug:
        return -53.7877, -67.7095  # Río Grande, Tierra del Fuego
    if "junin" in slug:
        return -33.1412, -68.4839  # Junín, Mendoza
    if normalized == "empresas":
        return -32.8895, -68.8458  # Mendoza Ciudad
    if normalized == "educacion":
        return -34.6037, -58.3816  # Buenos Aires
    return -33.1412, -68.4839


def _demo_public_state(*, is_live_vote: bool = True) -> dict[str, Any]:
    status = "live" if is_live_vote else "open"
    return {
        "contract_version": "surveys.public_state.v2",
        "status": status,
        "is_open": True,
        "accepts_responses": True,
        "is_live_vote": bool(is_live_vote),
        "results_visible": True,
        "comments_enabled": False,
        "opens_at": None,
        "closes_at": None,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "demo_mode": True,
    }


def _demo_survey_links(slug: str, public_base_url: str) -> dict[str, Any]:
    public_base = str(public_base_url or "https://www.chatboc.ar").rstrip("/")
    public_page_path = f"/e/{slug}"
    public_page_url = f"{public_base}{public_page_path}"
    qr_endpoint = f"/api/public/encuestas/v1/{slug}/qr?size=320"
    qr_url = f"{public_base}{qr_endpoint}"
    return {
        "contract_version": "surveys.links.v2",
        "public_token": slug,
        "public_page_path": public_page_path,
        "public_page_url": public_page_url,
        "share_url": public_page_url,
        "public_api_endpoint": f"/api/public/encuestas/v1/{slug}",
        "respond_endpoint": f"/api/public/encuestas/v1/{slug}/responder",
        "live_results_endpoint": f"/api/public/encuestas/v1/{slug}/live-results",
        "qr_endpoint": qr_endpoint,
        "qr_url": qr_url,
        "qr_image_url": qr_url,
        "legacy_public_api_endpoint": f"/api/public/encuestas/v1/{slug}",
        "legacy_live_results_endpoint": f"/api/public/encuestas/v1/{slug}/live-results",
    }


def _demo_share_contract(title: str, slug: str, public_base_url: str) -> dict[str, Any]:
    links = _demo_survey_links(slug, public_base_url)
    share_text = f"Participa en {title}: {links['public_page_url']}"
    whatsapp_url = f"https://wa.me/?text={quote_plus(share_text)}"
    return {
        "contract_version": "surveys.share.v2",
        "url": links["public_page_url"],
        "text": share_text,
        "whatsapp_text": share_text,
        "whatsapp_url": whatsapp_url,
        "copy": {
            "url": links["public_page_url"],
            "text": share_text,
        },
        "qr": {
            "target_url": links["public_page_url"],
            "image_url": links["qr_image_url"],
            "download_url": links["qr_image_url"],
            "endpoint": links["qr_endpoint"],
            "size": 320,
        },
        "channels": ["copy_link", "qr", "whatsapp"],
    }


def _demo_realtime_contract(slug: str, public_base_url: str) -> dict[str, Any]:
    links = _demo_survey_links(slug, public_base_url)
    return {
        "contract_version": "surveys.realtime.v2",
        "enabled": True,
        "demo_mode": True,
        "transports": ["polling"],
        "room": None,
        "socket": {
            "enabled": False,
            "reason": "demo_seeded_results_do_not_emit_socket",
            "path": "/api/socket.io",
            "join_event": "join",
            "join_payload": {"room": f"encuesta_{slug}"},
            "events": [
                {"name": "survey_update_v2", "contract_version": "surveys.live_results.v2"},
                {"name": "survey_update", "contract_version": "legacy"},
            ],
        },
        "polling": {
            "enabled": True,
            "href": links["live_results_endpoint"],
            "interval_ms": 8000,
            "fallback_after_ms": 15000,
        },
        "versioning": {
            "result_version": DEMO_SURVEY_RESPONSE_COUNT,
            "snapshot_version": f"demo:{slug}:{DEMO_SURVEY_RESPONSE_COUNT}",
            "result_version_field": "result_version",
            "snapshot_version_field": "snapshot_version",
        },
    }


def _demo_operational_next_steps(slug: str, public_base_url: str) -> dict[str, Any]:
    links = _demo_survey_links(slug, public_base_url)
    return {
        "contract_version": "surveys.operational_next_steps.v2",
        "status": "live",
        "items": [
            {
                "id": "share_public_link",
                "label": "Compartir enlace publico",
                "href": links["share_url"],
                "priority": 1,
            },
            {
                "id": "download_qr",
                "label": "Descargar QR",
                "href": links["qr_image_url"],
                "priority": 2,
            },
            {
                "id": "open_live_results",
                "label": "Abrir resultados demo",
                "href": links["live_results_endpoint"],
                "priority": 3,
            },
        ],
    }


def _results_for_template(template: dict[str, Any], *, sector: str, tenant_slug: str, slug: str) -> dict[str, Any]:
    total = DEMO_SURVEY_RESPONSE_COUNT
    option_counts = _split_counts(total, list(template.get("opciones") or []), f"{slug}:options")
    gender_counts = _split_counts(total, ["mujer", "varon", "otro_prefiere_no_decir"], f"{slug}:gender")
    age_counts = _split_counts(total, ["18-29", "30-44", "45-60", "60+"], f"{slug}:age")
    zone_counts = _split_counts(total, _geo_labels(sector, tenant_slug), f"{slug}:zone")
    channel_counts = _split_counts(total, ["whatsapp", "widget_chat", "web"], f"{slug}:channel")
    base_lat, base_lng = _coordinate_base(sector, tenant_slug)
    rng = _rng(slug, "heatmap")
    heatmap = []
    for zone in zone_counts:
        heatmap.append(
            {
                "label": zone["label"],
                "count": zone["count"],
                "lat": round(base_lat + rng.uniform(-0.035, 0.035), 6),
                "lng": round(base_lng + rng.uniform(-0.035, 0.035), 6),
                "weight": round(zone["count"] / total, 4),
            }
        )

    return {
        "contract_version": "demo.survey_results.v1",
        "seeded_responses": total,
        "total_respuestas": total,
        "options": option_counts,
        "segments": {
            "genero": gender_counts,
            "rango_edad": age_counts,
            "zona": zone_counts,
            "canal": channel_counts,
        },
        "heatmap_points": heatmap,
    }


def _build_demo_item(
    *,
    template: dict[str, Any],
    sector: str,
    tenant_slug: str,
    public_base_url: str,
) -> dict[str, Any]:
    normalized = normalize_demo_sector(sector)
    safe_tenant = _slug_part(tenant_slug or normalized)
    slug = f"demo-{normalized}-{safe_tenant}-{template['id']}"
    links = _demo_survey_links(slug, public_base_url)
    public_url = links["public_page_url"]
    share = _demo_share_contract(str(template.get("titulo") or "esta encuesta"), slug, public_base_url)
    share_text = share["text"]
    realtime = _demo_realtime_contract(slug, public_base_url)
    next_steps = _demo_operational_next_steps(slug, public_base_url)
    public_state = _demo_public_state(is_live_vote=template.get("tipo") == "votacion")
    results = _results_for_template(template, sector=normalized, tenant_slug=safe_tenant, slug=slug)
    return {
        "contract_version": "demo.survey_item.v1",
        "id": _hash_int(slug, digits=9),
        "slug": slug,
        "slug_publico": slug,
        "tenant_slug": safe_tenant,
        "sector": normalized,
        "tipo": template.get("tipo") or "encuesta",
        "titulo": template.get("titulo"),
        "title": template.get("titulo"),
        "descripcion": template.get("descripcion"),
        "description": template.get("descripcion"),
        "estado": "demo_publicada",
        "status": "demo_publicada",
        "public_state": public_state,
        "estado_publico": public_state,
        "demo_mode": True,
        "es_votacion_envivo": template.get("tipo") == "votacion",
        "mostrar_resultados_envivo": True,
        "permitir_comentarios": False,
        "question": template.get("pregunta"),
        "options": template.get("opciones") or [],
        "seed": {
            "contract_version": "demo.survey_seed.v1",
            "responses": DEMO_SURVEY_RESPONSE_COUNT,
            "personas_random": DEMO_SURVEY_RESPONSE_COUNT,
            "deterministic": True,
            "source": "backend_demo_contract",
        },
        "results": results,
        "analytics_summary": {
            "responses": DEMO_SURVEY_RESPONSE_COUNT,
            "top_option": max(results["options"], key=lambda item: item["count"]) if results.get("options") else None,
            "top_zone": max(results["segments"]["zona"], key=lambda item: item["count"]) if results.get("segments") else None,
        },
        "url_publica": public_url,
        "public_url": public_url,
        "share_url": public_url,
        "public_page_url": links["public_page_url"],
        "links": links,
        "share": share,
        "realtime": realtime,
        "operational_next_steps": next_steps,
        "next_steps": next_steps["items"],
        "qr_url": links["qr_url"],
        "qr_image_url": links["qr_image_url"],
        "whatsapp_share_text": share_text,
        "whatsapp_share_url": share["whatsapp_url"],
        "share_whatsapp_url": share["whatsapp_url"],
        "public_api_endpoint": f"/api/public/encuestas/v1/{slug}",
        "respond_endpoint": f"/api/public/encuestas/v1/{slug}/responder",
        "results_endpoint": f"/api/public/encuestas/v1/{slug}/live-results",
        "live_results_endpoint": links["live_results_endpoint"],
    }


def _all_demo_items(
    *,
    sector: str,
    tenant_slug: str,
    public_base_url: str = "https://www.chatboc.ar",
) -> list[dict[str, Any]]:
    normalized = normalize_demo_sector(sector)
    if normalized not in _SUPPORTED_SECTORS:
        normalized = "empresas"
    return [
        _build_demo_item(
            template=template,
            sector=normalized,
            tenant_slug=tenant_slug or normalized,
            public_base_url=public_base_url,
        )
        for template in _templates_for_sector(normalized)
    ]


def is_demo_survey_slug(slug: str | None) -> bool:
    normalized = str(slug or "").strip().lower()
    return normalized.startswith("demo-") and any(f"demo-{sector}-" in normalized for sector in _SUPPORTED_SECTORS)


def infer_demo_survey_context(slug: str | None) -> tuple[str, str] | None:
    normalized_slug = str(slug or "").strip().lower()
    if not is_demo_survey_slug(normalized_slug):
        return None
    for sector in _SUPPORTED_SECTORS:
        prefix = f"demo-{sector}-"
        if not normalized_slug.startswith(prefix):
            continue
        for template in _templates_for_sector(sector):
            suffix = f"-{template['id']}"
            if normalized_slug.endswith(suffix):
                tenant_slug = normalized_slug[len(prefix) : -len(suffix)]
                return sector, tenant_slug or sector
    return None


def build_demo_surveys_votings_contract(
    *,
    sector: str,
    tenant_slug: str,
    rubro: str | None = None,
    public_base_url: str = "https://www.chatboc.ar",
    page: int = 1,
    page_size: int = DEMO_SURVEY_PAGE_SIZE,
) -> dict[str, Any]:
    normalized = normalize_demo_sector(sector)
    if normalized not in _SUPPORTED_SECTORS:
        normalized = "empresas"
    labels = _sector_labels(normalized)
    safe_page_size = max(1, min(int(page_size or DEMO_SURVEY_PAGE_SIZE), DEMO_SURVEY_PAGE_SIZE))
    safe_page = max(1, int(page or 1))
    offset = (safe_page - 1) * safe_page_size
    all_items = _all_demo_items(
        sector=normalized,
        tenant_slug=tenant_slug or rubro or normalized,
        public_base_url=public_base_url,
    )
    visible_items = all_items[offset : offset + safe_page_size]
    has_more = offset + safe_page_size < len(all_items)
    query_sector = quote_plus(normalized)
    query_tenant = quote_plus(_slug_part(tenant_slug or rubro or normalized))
    return {
        "contract_version": DEMO_SURVEY_CONTRACT_VERSION,
        "enabled": True,
        "demo_mode": True,
        "source": "backend_demo_contract",
        "tenant_slug": tenant_slug,
        "sector": normalized,
        "rubro": rubro,
        "label": labels["menu_label"],
        "description": labels["menu_description"],
        "availability_rule": "always_visible_in_demo",
        "primary_action_enabled": True,
        "page": safe_page,
        "page_size": safe_page_size,
        "total_available": len(all_items),
        "has_more": has_more,
        "next_action_id": f"mostrar_menu_encuestas::{safe_page + 1}" if has_more else None,
        "previous_action_id": f"mostrar_menu_encuestas::{safe_page - 1}" if safe_page > 1 else None,
        "seed_policy": {
            "responses_per_item": DEMO_SURVEY_RESPONSE_COUNT,
            "personas_random": DEMO_SURVEY_RESPONSE_COUNT,
            "real_people": False,
            "deterministic": True,
        },
        "items": visible_items,
        "all_items": all_items,
        "primary_action": {
            "label": labels["menu_label"],
            "texto": labels["menu_label"],
            "description": labels["menu_description"],
            "action_id": "mostrar_menu_encuestas",
            "intent": "mostrar_menu_encuestas",
            "payload": {"demo_mode": True, "sector": normalized, "tenant_slug": tenant_slug},
        },
        "public_list_endpoint": f"/api/public/encuestas/v1?tenant_slug={query_tenant}&sector={query_sector}&demo_mode=1",
        "respond_endpoint_template": "/api/public/encuestas/v1/{survey_slug}/responder",
        "live_results_endpoint_template": "/api/public/encuestas/v1/{survey_slug}/live-results",
        "public_detail_endpoint_template": "/api/public/encuestas/v1/{survey_slug}",
        "frontend_contract": {
            "render_as": "survey_voting_module",
            "show_only_when_enabled": False,
            "empty_state_behavior": "render_demo_seeded_surveys",
            "page_size": safe_page_size,
            "supports_whatsapp_share": True,
            "supports_live_results": True,
        },
    }


def build_demo_public_survey_payload(
    slug: str,
    *,
    public_base_url: str = "https://www.chatboc.ar",
) -> dict[str, Any] | None:
    context = infer_demo_survey_context(slug)
    if not context:
        return None
    sector, tenant_slug = context
    for item in _all_demo_items(sector=sector, tenant_slug=tenant_slug, public_base_url=public_base_url):
        if item["slug"] != str(slug or "").strip().lower():
            continue
        question_id = f"q_{item['id']}"
        options = []
        for index, option in enumerate(item.get("options") or [], start=1):
            option_id = f"{question_id}_op_{index}"
            votes = next(
                (entry["count"] for entry in item["results"]["options"] if entry["label"] == option),
                0,
            )
            options.append({"id": option_id, "texto": option, "label": option, "votos": votes})
        payload = {
            **item,
            "contract_version": "encuestas.public.v1",
            "canonical_slug": item["slug"],
            "requested_slug": item["slug"],
            "slug_alias_used": False,
            "inicio_at": datetime.now(timezone.utc).isoformat(),
            "fin_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "requiere_identidad": False,
            "anonimo_permitido": True,
            "politica_unicidad": "demo_session",
            "preguntas": [
                {
                    "id": question_id,
                    "texto": item.get("question"),
                    "titulo": item.get("question"),
                    "tipo": "opcion_unica",
                    "obligatoria": True,
                    "opciones": options,
                }
            ],
            "resultados_envivo": build_demo_live_results_payload(item["slug"], public_base_url=public_base_url),
        }
        payload.pop("estado", None)
        return payload
    return None


def build_demo_live_results_payload(
    slug: str,
    *,
    public_base_url: str = "https://www.chatboc.ar",
) -> dict[str, Any] | None:
    context = infer_demo_survey_context(slug)
    if not context:
        return None
    sector, tenant_slug = context
    item = next(
        (
            candidate
            for candidate in _all_demo_items(sector=sector, tenant_slug=tenant_slug, public_base_url=public_base_url)
            if candidate["slug"] == str(slug or "").strip().lower()
        ),
        None,
    )
    if not item:
        return None
    question_id = f"q_{item['id']}"
    links = _demo_survey_links(item["slug"], public_base_url)
    share = _demo_share_contract(str(item.get("titulo") or "esta encuesta"), item["slug"], public_base_url)
    realtime = _demo_realtime_contract(item["slug"], public_base_url)
    next_steps = _demo_operational_next_steps(item["slug"], public_base_url)
    public_state = _demo_public_state(is_live_vote=bool(item.get("es_votacion_envivo")))
    opciones = [
        {
            "id": f"{question_id}_op_{index}",
            "texto": entry["label"],
            "votos": entry["count"],
        }
        for index, entry in enumerate(item["results"]["options"], start=1)
    ]
    return {
        "contract_version": "encuestas.live_results.v1",
        "ok": True,
        "demo_mode": True,
        "slug": item["slug"],
        "tenant_slug": tenant_slug,
        "sector": sector,
        "public_state": public_state,
        "estado_publico": public_state,
        "total_respuestas": DEMO_SURVEY_RESPONSE_COUNT,
        "seeded_responses": DEMO_SURVEY_RESPONSE_COUNT,
        "result_version": DEMO_SURVEY_RESPONSE_COUNT,
        "snapshot_version": f"demo:{item['slug']}:{DEMO_SURVEY_RESPONSE_COUNT}",
        "preguntas": {
            question_id: {
                "tipo": "opcion_unica",
                "texto": item.get("question"),
                "opciones": opciones,
            }
        },
        "segments": item["results"]["segments"],
        "heatmap": {
            "points": item["results"]["heatmap_points"],
            "source": "demo_seeded_responses",
        },
        "links": links,
        "share": share,
        "realtime": realtime,
        "operational_next_steps": next_steps,
        "next_steps": next_steps["items"],
        "render_contract": {
            "preferred_visualization": "live_vote_command_center",
            "supports": [
                "cards",
                "bars",
                "heatmap",
                "ai_summary",
                "polling_fallback",
                "qr_share",
                "admin_next_steps",
            ],
            "polling_interval_ms": 8000,
            "empty_state": "Todavia no hay respuestas para mostrar.",
        },
        "ui_actions": [
            {"id": "share_public_link", "label": "Compartir", "href": links["share_url"]},
            {"id": "download_qr", "label": "QR", "href": links["qr_image_url"]},
            {"id": "open_public_page", "label": "Abrir encuesta", "href": links["public_page_url"]},
        ],
    }


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _normalize_demo_option(value: Any, options: list[dict[str, Any]]) -> dict[str, Any] | None:
    if isinstance(value, dict):
        value = _first_present(
            value,
            "option_id",
            "opcion_id",
            "selected_option_id",
            "selectedOptionId",
            "opcion",
            "texto",
            "label",
            "value",
        )
    if value in (None, ""):
        return None

    raw_value = str(value).strip()
    normalized_value = raw_value.lower()
    for option in options:
        option_id = str(option.get("id") or "").strip()
        option_label = str(option.get("texto") or option.get("label") or "").strip()
        if normalized_value in {option_id.lower(), option_label.lower()}:
            return {
                "option_id": option_id,
                "option_label": option_label,
                "value": raw_value,
                "matched": True,
            }

    if raw_value.isdigit():
        option_index = int(raw_value) - 1
        if 0 <= option_index < len(options):
            option = options[option_index]
            option_id = str(option.get("id") or "").strip()
            option_label = str(option.get("texto") or option.get("label") or "").strip()
            return {
                "option_id": option_id,
                "option_label": option_label,
                "value": raw_value,
                "matched": True,
            }

    return {
        "option_id": None,
        "option_label": raw_value,
        "value": raw_value,
        "matched": False,
    }


def _normalize_demo_survey_answers(
    payload: dict[str, Any] | None,
    question: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    candidate_entries = _first_present(
        payload,
        "answers",
        "respuestas",
        "responses",
        "items",
        "respuesta",
    )
    if candidate_entries is None:
        candidate_entries = payload
    if isinstance(candidate_entries, dict):
        candidate_entries = [candidate_entries]
    if not isinstance(candidate_entries, list):
        candidate_entries = [{"value": candidate_entries}]

    question_id = str(question.get("id") or "").strip()
    question_text = str(question.get("texto") or question.get("titulo") or "").strip()
    options = list(question.get("opciones") or [])
    normalized_answers: list[dict[str, Any]] = []

    for entry in candidate_entries:
        if not isinstance(entry, dict):
            entry = {"value": entry}
        submitted_question_id = _first_present(
            entry,
            "question_id",
            "pregunta_id",
            "questionId",
            "preguntaId",
            "id_pregunta",
        )
        submitted_option = _first_present(
            entry,
            "option_id",
            "opcion_id",
            "selected_option_id",
            "selectedOptionId",
            "opcion_ids",
            "opcionIds",
            "option_ids",
            "optionIds",
            "opciones",
            "options",
            "opcion",
            "respuesta",
            "value",
            "choice",
        )
        submitted_options = (
            list(submitted_option)
            if isinstance(submitted_option, (list, tuple, set))
            else [submitted_option]
        )
        seen_option_keys: set[str] = set()
        for submitted_option_value in submitted_options:
            option = _normalize_demo_option(submitted_option_value, options)
            if not option:
                continue
            option_key = str(option["option_id"] or option["option_label"] or option["value"])
            if option_key in seen_option_keys:
                continue
            seen_option_keys.add(option_key)
            normalized_answers.append(
                {
                    "question_id": question_id,
                    "question_text": question_text,
                    "submitted_question_id": str(submitted_question_id or question_id),
                    "option_id": option["option_id"],
                    "option_label": option["option_label"],
                    "value": option["value"],
                    "matched": option["matched"],
                }
            )

    return normalized_answers


def build_demo_survey_response_ack(
    slug: str,
    payload: dict[str, Any] | None = None,
    *,
    public_base_url: str = "https://www.chatboc.ar",
) -> dict[str, Any] | None:
    public_payload = build_demo_public_survey_payload(slug, public_base_url=public_base_url)
    if not public_payload:
        return None

    question = (public_payload.get("preguntas") or [{}])[0]
    answers = _normalize_demo_survey_answers(payload, question)
    normalized_payload = {
        "slug": public_payload["slug"],
        "payload": payload or {},
        "answers": answers,
    }
    fingerprint_input = json.dumps(normalized_payload, sort_keys=True, default=str)
    request_fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:16]
    accepted = bool(answers)
    message = (
        "Voto demo registrado. Ahora podes ver los resultados en vivo."
        if accepted
        else "No se detecto una opcion valida para registrar el voto demo."
    )
    return {
        "contract_version": "demo.survey_response_ack.v1",
        "ok": True,
        "success": True,
        "accepted": accepted,
        "ignored": not accepted,
        "duplicate": False,
        "demo_mode": True,
        "slug": public_payload["slug"],
        "canonical_slug": public_payload["canonical_slug"],
        "sector": public_payload["sector"],
        "tenant_slug": public_payload["tenant_slug"],
        "respuesta_id": f"demo_resp_{request_fingerprint}",
        "request_id": f"demo_req_{request_fingerprint}",
        "message": message,
        "answer_count": len(answers),
        "answers": answers,
        "respuestas": answers,
        "seeded_responses_before": DEMO_SURVEY_RESPONSE_COUNT,
        "seeded_responses_after": DEMO_SURVEY_RESPONSE_COUNT + 1,
        "public_url": public_payload["public_url"],
        "public_page_url": public_payload.get("public_page_url"),
        "respond_endpoint": public_payload["respond_endpoint"],
        "results_endpoint": public_payload["results_endpoint"],
        "live_results_endpoint": public_payload.get("live_results_endpoint"),
        "qr_url": public_payload.get("qr_url"),
        "qr_image_url": public_payload.get("qr_image_url"),
        "links": public_payload.get("links"),
        "share": public_payload.get("share"),
        "realtime": public_payload.get("realtime"),
        "public_state": public_payload.get("public_state"),
        "estado_publico": public_payload.get("estado_publico"),
        "operational_next_steps": public_payload.get("operational_next_steps"),
        "next_steps": public_payload.get("next_steps") or [],
        "next_url": f"{public_payload['public_url']}?resultados=1",
        "results_url": f"{public_payload['public_url']}?resultados=1",
        "resultados_envivo": public_payload.get("resultados_envivo"),
        "analytics": {
            "accepted": accepted,
            "ignored": not accepted,
            "source": "demo_survey_response_ack",
        },
    }


def build_demo_survey_chat_menu(
    *,
    sector: str,
    tenant_slug: str,
    rubro: str | None = None,
    channel: str = "widget",
    public_base_url: str = "https://www.chatboc.ar",
    page: int = 1,
    page_size: int | None = None,
) -> dict[str, Any]:
    contract = build_demo_surveys_votings_contract(
        sector=sector,
        tenant_slug=tenant_slug,
        rubro=rubro,
        public_base_url=public_base_url,
        page=page,
        page_size=page_size or DEMO_SURVEY_PAGE_SIZE,
    )
    labels = _sector_labels(contract["sector"])
    normalized_channel = str(channel or "").strip().lower()
    is_whatsapp = normalized_channel in {"wa", "whatsapp", "twilio", "twilio_whatsapp"} or "whatsapp" in normalized_channel
    lines = [f"*{labels['heading']}*"]
    if is_whatsapp:
        lines.append(
            "Elegi una encuesta para votar. Primero registramos tu voto anonimo "
            "y despues ves resultados demo."
        )
    else:
        lines.append("Cada demo trae 100 respuestas sinteticas para ver resultados reales de UX.")
    for index, item in enumerate(contract.get("items") or [], start=1):
        title = item.get("titulo") or item.get("slug")
        public_url = item.get("public_url")
        share_url = item.get("whatsapp_share_url")
        if is_whatsapp:
            lines.append(f"{index}. {title}")
        else:
            lines.append(f"{index}. *{title}*")
        if item.get("descripcion") and not is_whatsapp:
            lines.append(f"   {item['descripcion']}")
        if public_url and not is_whatsapp:
            lines.append(f"   Abrir: {public_url}")
        if is_whatsapp and public_url:
            share_url = f"https://wa.me/?text={quote_plus(str(public_url))}"
        if share_url and not is_whatsapp:
            share_label = "Compartir" if is_whatsapp else "Compartir por WhatsApp"
            lines.append(f"   {share_label}: {share_url}")

    if is_whatsapp and contract.get("items"):
        lines.append("")
        lines.append("Responde con el numero de la encuesta o toca una opcion.")

    options: list[dict[str, Any]] = []
    for index, item in enumerate(contract.get("items") or [], start=1):
        title = str(item.get("titulo") or item.get("slug") or "Encuesta")[:36]
        slug = str(item.get("slug") or "").strip()
        if is_whatsapp:
            if slug:
                options.append({"texto": f"🗳️ Votar {index}", "action_id": f"chatboc_survey_open::{slug}"})
                options.append({"texto": f"📤 Compartir {index}", "action_id": f"chatboc_survey_share::{slug}"})
        else:
            if item.get("public_url"):
                options.append({"texto": f"Abrir {title}", "url": item["public_url"], "type": "url"})
            if item.get("whatsapp_share_url"):
                options.append({"texto": f"Compartir {title}", "url": item["whatsapp_share_url"], "type": "url"})

    if contract.get("previous_action_id"):
        options.append({"texto": "Ver anteriores", "action_id": contract["previous_action_id"]})
    if contract.get("next_action_id"):
        options.append({"texto": "Ver mas", "action_id": contract["next_action_id"]})
    options.append({"texto": "Volver", "action_id": labels["back_action"]})

    if is_whatsapp:
        cleaned_options: list[dict[str, Any]] = []
        vote_index = 1
        for option in options:
            action_id = str(option.get("action_id") or "")
            if action_id.startswith("chatboc_survey_share::"):
                continue
            if action_id.startswith("chatboc_survey_open::"):
                option = {**option, "texto": f"Votar encuesta {vote_index}"}
                vote_index += 1
            cleaned_options.append(option)
        options = cleaned_options

    return {
        "success": True,
        "message_body": "\n".join(lines).strip(),
        "message_type": "text" if is_whatsapp else "interactive_buttons",
        "options_list": options,
        "fuente": "demo_encuestas_menu_v1",
        "contract_version": "demo.encuestas_menu.v1",
        "pagination": {
            "page": contract["page"],
            "page_size": contract["page_size"],
            "has_more": contract["has_more"],
            "next_action_id": contract.get("next_action_id"),
            "previous_action_id": contract.get("previous_action_id"),
        },
        "surveys": contract.get("items") or [],
        "data": {"surveys_votings": contract},
    }
