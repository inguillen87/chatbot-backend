from __future__ import annotations

import hashlib
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


def _geo_labels(sector: str) -> list[str]:
    normalized = normalize_demo_sector(sector)
    if normalized == "educacion":
        return ["Inicial", "Primaria", "Secundaria", "Familias nuevas", "Egresados"]
    if normalized == "gobierno":
        return ["Centro", "Norte", "Este", "Oeste", "Barrios rurales"]
    return ["Mostrador", "WhatsApp", "Tienda online", "Mayoristas", "Recurrentes"]


def _coordinate_base(sector: str, tenant_slug: str) -> tuple[float, float]:
    normalized = normalize_demo_sector(sector)
    slug = _slug_part(tenant_slug)
    if "junin" in slug:
        return -34.5844, -60.9433
    if normalized == "empresas":
        return -32.8895, -68.8458
    if normalized == "educacion":
        return -34.6037, -58.3816
    return -34.5844, -60.9433


def _results_for_template(template: dict[str, Any], *, sector: str, tenant_slug: str, slug: str) -> dict[str, Any]:
    total = DEMO_SURVEY_RESPONSE_COUNT
    option_counts = _split_counts(total, list(template.get("opciones") or []), f"{slug}:options")
    gender_counts = _split_counts(total, ["mujer", "varon", "otro_prefiere_no_decir"], f"{slug}:gender")
    age_counts = _split_counts(total, ["18-29", "30-44", "45-60", "60+"], f"{slug}:age")
    zone_counts = _split_counts(total, _geo_labels(sector), f"{slug}:zone")
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
    public_base = str(public_base_url or "https://www.chatboc.ar").rstrip("/")
    public_url = f"{public_base}/e/{slug}"
    share_text = f"Participa en {template['titulo']}: {public_url}"
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
        "whatsapp_share_text": share_text,
        "whatsapp_share_url": f"https://wa.me/?text={quote_plus(share_text)}",
        "share_whatsapp_url": f"https://wa.me/?text={quote_plus(share_text)}",
        "public_api_endpoint": f"/api/public/encuestas/v1/{slug}",
        "respond_endpoint": f"/api/public/encuestas/v1/{slug}/responder",
        "results_endpoint": f"/api/public/encuestas/v1/{slug}/live-results",
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
        "total_respuestas": DEMO_SURVEY_RESPONSE_COUNT,
        "seeded_responses": DEMO_SURVEY_RESPONSE_COUNT,
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
    }


def build_demo_survey_response_ack(slug: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    context = infer_demo_survey_context(slug)
    if not context:
        return None
    request_fingerprint = hashlib.sha256(
        f"{slug}|{payload or {}}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "ok": True,
        "success": True,
        "demo_mode": True,
        "respuesta_id": f"demo_resp_{request_fingerprint}",
        "request_id": f"demo_req_{request_fingerprint}",
        "message": "Participacion demo registrada. Los resultados usan 100 personas sinteticas.",
        "seeded_responses_before": DEMO_SURVEY_RESPONSE_COUNT,
        "seeded_responses_after": DEMO_SURVEY_RESPONSE_COUNT + 1,
    }


def build_demo_survey_chat_menu(
    *,
    sector: str,
    tenant_slug: str,
    rubro: str | None = None,
    channel: str = "widget",
    public_base_url: str = "https://www.chatboc.ar",
    page: int = 1,
) -> dict[str, Any]:
    contract = build_demo_surveys_votings_contract(
        sector=sector,
        tenant_slug=tenant_slug,
        rubro=rubro,
        public_base_url=public_base_url,
        page=page,
    )
    labels = _sector_labels(contract["sector"])
    is_whatsapp = "whatsapp" in str(channel or "").lower()
    lines = [f"*{labels['heading']}*"]
    if is_whatsapp:
        lines.append("Incluye 100 respuestas demo y resultados en vivo.")
    else:
        lines.append("Cada demo trae 100 respuestas sinteticas para ver resultados reales de UX.")
    for index, item in enumerate(contract.get("items") or [], start=1):
        title = item.get("titulo") or item.get("slug")
        public_url = item.get("public_url")
        share_url = item.get("whatsapp_share_url")
        lines.append(f"{index}. *{title}*")
        if item.get("descripcion") and not is_whatsapp:
            lines.append(f"   {item['descripcion']}")
        if public_url:
            lines.append(f"   Abrir: {public_url}")
        if is_whatsapp and public_url:
            share_url = f"https://wa.me/?text={quote_plus(str(public_url))}"
        if share_url:
            share_label = "Compartir" if is_whatsapp else "Compartir por WhatsApp"
            lines.append(f"   {share_label}: {share_url}")

    options: list[dict[str, Any]] = []
    if not is_whatsapp:
        for item in contract.get("items") or []:
            title = str(item.get("titulo") or item.get("slug") or "Encuesta")[:36]
            if item.get("public_url"):
                options.append({"texto": f"Abrir {title}", "url": item["public_url"], "type": "url"})
            if item.get("whatsapp_share_url"):
                options.append({"texto": f"Compartir {title}", "url": item["whatsapp_share_url"], "type": "url"})

    if contract.get("previous_action_id"):
        options.append({"texto": "Ver anteriores", "action_id": contract["previous_action_id"]})
    if contract.get("next_action_id"):
        options.append({"texto": "Ver mas", "action_id": contract["next_action_id"]})
    options.append({"texto": "Volver", "action_id": labels["back_action"]})

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
