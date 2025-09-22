import os
import unicodedata
from typing import Iterator, Pattern, Union

from flask import Blueprint, jsonify, request, current_app
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from datetime import datetime, timedelta
from utils.time_utils import get_local_now
from utils.permissions import require_role
from routes.crm import _obtener_clientes
from services.municipio_responder import TODAS_LAS_CATEGORIAS_UNICAS
from routes.tramites import listar_tramites, obtener_tramite
from sqlalchemy import func
from models import Conversacion, MunicipioTicket, User, db
from routes.ticket import TICKET_ALLOWED_STATES
from config import ALLOWED_ORIGINS as DEFAULT_ALLOWED_ORIGINS
from services.municipal_stats import build_stats_for_municipio, StatsFilters

municipal_bp = Blueprint('municipal_legacy', __name__, url_prefix='/municipal')


AllowedOrigin = Union[str, Pattern[str]]


def _iter_allowed_origins() -> Iterator[AllowedOrigin]:
    """Yield the configured CORS origins in priority order."""

    env_value = os.getenv("CORS_ALLOWED_ORIGINS")
    if env_value is not None:
        stripped = env_value.strip()
        if stripped == "*":
            yield "*"
            return

        seen = set()
        for raw in stripped.split(","):
            candidate = raw.strip().rstrip("/")
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            yield candidate
        if seen:
            return

    for origin in DEFAULT_ALLOWED_ORIGINS:
        yield origin


def _resolve_cors_origin(origin: str | None) -> tuple[str | None, bool]:
    """Return the header value and whether credentials are allowed."""

    if not origin:
        return None, False

    normalized = origin.rstrip("/")
    for allowed in _iter_allowed_origins():
        if allowed == "*":
            return "*", False
        if isinstance(allowed, str):
            if normalized == allowed.rstrip("/"):
                return origin, True
        elif hasattr(allowed, "match") and allowed.match(origin):
            return origin, True

    return None, False


def _merge_header_values(response, header_name, values):
    """Ensure the given response header contains the provided comma-separated values."""

    existing = response.headers.get(header_name, "")
    items = [item.strip() for item in existing.split(",") if item.strip()]

    updated = list(items)
    for value in values:
        if value not in updated:
            updated.append(value)

    if updated:
        response.headers[header_name] = ", ".join(updated)


@municipal_bp.after_request
def apply_cors_headers(response):
    """Apply permissive CORS defaults for municipal endpoints."""

    allowed_origin, allow_credentials = _resolve_cors_origin(
        request.headers.get("Origin")
    )

    if allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin

        if allowed_origin != "*":
            _merge_header_values(response, "Vary", ["Origin"])
            if allow_credentials:
                response.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            response.headers.pop("Access-Control-Allow-Credentials", None)

        _merge_header_values(
            response,
            "Access-Control-Allow-Headers",
            [
                "Authorization",
                "Content-Type",
                "Origin",
                "Accept",
                "X-Entity-Token",
                "X-Chat-Session-Id",
                "X-Anon-Id",
                "Anon-Id",
            ],
        )

        _merge_header_values(
            response,
            "Access-Control-Allow-Methods",
            ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
    else:
        response.headers.pop("Access-Control-Allow-Credentials", None)

    return response


_STATS_RANGE_OPTIONS = [
    {"id": "last_7_days", "label": "Últimos 7 días"},
    {"id": "last_30_days", "label": "Últimos 30 días"},
    {"id": "this_month", "label": "Este mes"},
    {"id": "this_year", "label": "Este año"},
    {"id": "today", "label": "Hoy"},
]


def _normalize_filter_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower().replace("_", " ").replace("-", " ")
    return " ".join(normalized.split())


def _parse_str_list(args, key: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in args.getlist(key):
        if raw:
            trimmed = raw.strip()
            if trimmed:
                values.append(trimmed)
    for raw in args.getlist(f"{key}[]"):
        if raw:
            trimmed = raw.strip()
            if trimmed:
                values.append(trimmed)
    if not values:
        raw = args.get(key)
        if raw:
            values.extend(part.strip() for part in raw.split(",") if part.strip())
    if not values:
        return ()

    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        lower = value.lower()
        if lower not in seen:
            seen.add(lower)
            unique.append(value)
    return tuple(unique)


def _parse_int_list(args, key: str) -> tuple[int, ...]:
    result: list[int] = []
    seen: set[int] = set()
    for raw in _parse_str_list(args, key):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _parse_date_param(value: str | None, tzinfo, *, is_end: bool = False) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzinfo)
    if "T" not in raw and is_end:
        parsed = parsed + timedelta(days=1)
    return parsed


def _resolve_range_datetimes(value: str | None, now: datetime) -> tuple[datetime | None, datetime | None]:
    normalized = _normalize_filter_text(value)
    if not normalized:
        return None, None

    if "7" in normalized and ("dia" in normalized or "day" in normalized):
        return now - timedelta(days=7), now
    if "30" in normalized and ("dia" in normalized or "day" in normalized):
        return now - timedelta(days=30), now
    if "mes" in normalized:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (start + timedelta(days=32)).replace(day=1)
        return start, next_month
    if "ano" in normalized or "year" in normalized:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        next_year = start.replace(year=start.year + 1)
        return start, next_year
    if "hoy" in normalized or "today" in normalized:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if "ayer" in normalized or "yesterday" in normalized:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    return None, None


def _build_stats_filters_from_request(args) -> tuple[StatsFilters | None, datetime | None, datetime | None]:
    current_now = get_local_now()
    tzinfo = current_now.tzinfo
    fecha_inicio, fecha_fin = _resolve_range_datetimes(args.get("rango"), current_now)

    explicit_inicio = _parse_date_param(args.get("fecha_inicio"), tzinfo)
    if explicit_inicio:
        fecha_inicio = explicit_inicio

    explicit_fin = _parse_date_param(args.get("fecha_fin"), tzinfo, is_end=True)
    if explicit_fin:
        fecha_fin = explicit_fin

    estados = _parse_str_list(args, "estado")
    categorias = _parse_str_list(args, "categoria")
    distritos = _parse_str_list(args, "distrito")
    canales = _parse_str_list(args, "canal")
    agentes = _parse_int_list(args, "agente_id")
    if not agentes:
        agentes = _parse_int_list(args, "agente")

    filters = StatsFilters(
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        estados=estados or None,
        categorias=categorias or None,
        distritos=distritos or None,
        canales=canales or None,
        agentes=agentes or None,
    )

    if filters.is_empty():
        filters = None

    return filters, fecha_inicio, fecha_fin


def _dedupe_sorted(values, fallback: str) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        label = (value or "").strip()
        if not label:
            label = fallback
        key = label.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(label)
    cleaned.sort()
    return cleaned


@municipal_bp.route('/usuarios', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_usuarios(current_user):
    tag = request.args.get('tag')
    q = request.args.get('q')
    marketing = request.args.get('acepta_marketing')
    sort = request.args.get('sort')
    order = request.args.get('order')
    limit = request.args.get('limit')
    offset = request.args.get('offset')
    return jsonify(
        _obtener_clientes(
            current_user,
            tag,
            q=q,
            acepta_marketing=marketing,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    )

@municipal_bp.route('/categorias', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def municipal_categorias(current_user):
    return jsonify(TODAS_LAS_CATEGORIAS_UNICAS)

@municipal_bp.route('/estados', methods=['GET', 'OPTIONS'])
def municipal_estados():
    """Devuelve la lista pública de estados permitidos para tickets municipales."""

    if request.method == 'OPTIONS':
        return "", 204

    return jsonify({"estados": TICKET_ALLOWED_STATES})

@municipal_bp.route('/stats', methods=['GET', 'OPTIONS'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats(current_user):
    """Estadísticas profesionales del municipio del usuario."""

    if request.method == 'OPTIONS':
        return "", 204

    municipio_id = getattr(current_user, "municipio_id", None)
    filters, _, _ = _build_stats_filters_from_request(request.args)

    if filters:
        datos = build_stats_for_municipio(municipio_id, filters=filters)
    else:
        datos = build_stats_for_municipio(municipio_id)

    return jsonify(datos)

@municipal_bp.route('/stats/filters', methods=['GET', 'OPTIONS'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats_filters(current_user):
    municipio_id = getattr(current_user, "municipio_id", None)

    if not municipio_id:
        return jsonify(
            {
                "categorias": [],
                "estados": sorted(TICKET_ALLOWED_STATES),
                "rangos": _STATS_RANGE_OPTIONS,
                "distritos": [],
                "canales": [],
            }
        )

    categorias_query = (
        db.session.query(MunicipioTicket.categoria)
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.categoria.isnot(None),
            MunicipioTicket.categoria != "",
        )
        .distinct()
    )
    categorias = _dedupe_sorted((row[0] for row in categorias_query), "Sin categoría")

    distritos_query = (
        db.session.query(MunicipioTicket.distrito)
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.distrito.isnot(None),
            MunicipioTicket.distrito != "",
        )
        .distinct()
    )
    distritos = _dedupe_sorted((row[0] for row in distritos_query), "Sin distrito")

    canales_query = (
        db.session.query(MunicipioTicket.canal_ingreso)
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.canal_ingreso.isnot(None),
            MunicipioTicket.canal_ingreso != "",
        )
        .distinct()
    )
    canales = _dedupe_sorted((row[0] for row in canales_query), "Sin especificar")

    return jsonify(
        {
            "categorias": categorias,
            "estados": sorted(TICKET_ALLOWED_STATES),
            "rangos": _STATS_RANGE_OPTIONS,
            "distritos": distritos,
            "canales": canales,
        }
    )

@municipal_bp.route('/tramites', methods=['GET'])
def municipal_tramites():
    return listar_tramites()

@municipal_bp.route('/tramites/<string:nombre>', methods=['GET'])
def municipal_tramite(nombre):
    return obtener_tramite(nombre)

@municipal_bp.route('/tickets/map_data', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_tickets_map_data(current_user):
    """
    Devuelve datos de tickets municipales con ubicación para el municipio del
    usuario actual, optimizados para mostrar en un mapa. Se puede filtrar por
    estado (p.ej. ``abierto`` o ``cerrado``); si no se especifica, se incluyen
    todos los estados.
    """
    from services.ticket_service import servicio_tickets  # Importación local

    municipio_id_del_admin = current_user.municipio_id
    if not municipio_id_del_admin:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    estado = request.args.get("estado")
    tickets_con_ubicacion = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket="municipio",
        municipio_id=municipio_id_del_admin,
        estado=estado,
    )
    return jsonify(tickets_con_ubicacion)


@municipal_bp.route('/tickets/locations', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_tickets_locations(current_user):
    """
    Devuelve una lista de coordenadas de tickets para el mapa de calor.
    Formato: [{ "lat": lat, "lng": lng }]
    """
    from services.ticket_service import servicio_tickets

    municipio_id_del_admin = current_user.municipio_id
    if not municipio_id_del_admin:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    locations = servicio_tickets.obtener_locations_de_tickets(
        municipio_id=municipio_id_del_admin
    )
    return jsonify(locations)


@municipal_bp.route('/incidents', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_incidents(current_user):
    """Lista los tickets municipales abiertos para el municipio del usuario."""

    try:
        tickets = (
            MunicipioTicket.query
            .filter_by(municipio_id=current_user.municipio_id)
            .filter(MunicipioTicket.estado != 'cerrado') # Podríamos querer ver todos en el admin, no solo los no cerrados
            .order_by(MunicipioTicket.fecha.desc())
            .all()
        )
    except Exception:
        current_app.logger.exception("Error fetching municipal incidents")
        tickets = []

    resultado = [
        {
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "asunto": getattr(t, "asunto", "N/A"),
            "categoria": getattr(t, "categoria", None),
            "estado": t.estado,
            "fecha": t.fecha.isoformat() if getattr(t, "fecha", None) else None,
            "pregunta": getattr(t, "pregunta", None), # Descripción breve inicial
            "detalles": getattr(t, "detalles", None), # Detalles completos del reclamo
            "direccion": getattr(t, "direccion", None),
            "latitud": getattr(t, "latitud", None),
            "longitud": getattr(t, "longitud", None),
            "archivo_url": getattr(t, "archivo_url", None), # Para la foto
            # Datos del vecino/usuario si están disponibles (requeriría join con User o guardar en ticket)
            "nombre_vecino": getattr(t, "nombre_vecino", None), # Asumiendo que se añada al modelo o se obtenga de User
            "telefono_vecino": getattr(t, "telefono_vecino", None),
            "email_vecino": getattr(t, "email_vecino", None),
        }
        for t in tickets
    ]

    return jsonify(resultado)


def _municipal_message_metrics(
    eid: int,
    *,
    fecha_inicio: datetime | None = None,
    fecha_fin: datetime | None = None,
) -> dict:
    """Calcula métricas de mensajes recibidos en distintos períodos."""

    ahora = get_local_now()

    def _contar_desde(dias: int) -> int:
        desde = ahora - timedelta(days=dias)
        if fecha_inicio:
            desde = max(desde, fecha_inicio)
        query = (
            db.session.query(func.count())
            .select_from(Conversacion)
            .join(User, Conversacion.user_id == User.id)
            .filter(User.empresa_id == eid)
            .filter(Conversacion.timestamp >= desde)
        )
        if fecha_fin:
            query = query.filter(Conversacion.timestamp < fecha_fin)
        return int(query.scalar() or 0)

    cards = [
        {"label": "Mensajes esta semana", "value": _contar_desde(7)},
        {"label": "Mensajes este mes", "value": _contar_desde(30)},
        {"label": "Mensajes este año", "value": _contar_desde(365)},
    ]

    total_general_query = (
        db.session.query(func.count())
        .select_from(Conversacion)
        .join(User, Conversacion.user_id == User.id)
        .filter(User.empresa_id == eid)
    )
    total_general = int(total_general_query.scalar() or 0)

    filtrado_query = (
        db.session.query(func.count())
        .select_from(Conversacion)
        .join(User, Conversacion.user_id == User.id)
        .filter(User.empresa_id == eid)
    )
    if fecha_inicio:
        filtrado_query = filtrado_query.filter(Conversacion.timestamp >= fecha_inicio)
    if fecha_fin:
        filtrado_query = filtrado_query.filter(Conversacion.timestamp < fecha_fin)
    total_filtrado = int(filtrado_query.scalar() or 0)

    summary = {
        "last_7_days": cards[0]["value"],
        "last_30_days": cards[1]["value"],
        "last_365_days": cards[2]["value"],
        "total": total_general,
        "filtered_total": total_filtrado,
    }

    return {"cards": cards, "summary": summary}


@municipal_bp.route('/metrics', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_metrics(current_user):
    """Devuelve cantidad de mensajes de vecinos por rango de tiempo."""

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    return jsonify(_municipal_message_metrics(eid))


import os
import json
from werkzeug.utils import secure_filename
from uuid import uuid4

@municipal_bp.route('/posts', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def list_municipal_posts(current_user):
    """Devuelve los posts municipales (eventos o noticias) guardados."""
    if current_user.tipo_chat != "municipio":
        return jsonify({"error": "Acceso denegado. Se requiere un usuario municipal."}), 403

    from services.config_loader import BASE_CONFIG_PATH
    agenda_dir = os.path.join(BASE_CONFIG_PATH, 'default')
    agenda_path = os.path.join(agenda_dir, 'agenda_cultural.json')

    if not os.path.exists(agenda_path):
        return jsonify([]), 200

    try:
        with open(agenda_path, 'r', encoding='utf-8') as f:
            contenido = f.read().strip()
            if not contenido:
                return jsonify([]), 200
            try:
                data = json.loads(contenido)
            except json.JSONDecodeError:
                current_app.logger.warning("agenda_cultural.json corrupto, se recreará.")
                return jsonify([]), 200
        eventos = data.get('eventos', [])
        return jsonify(eventos[:10]), 200
    except Exception as e:
        current_app.logger.error(f"Error al leer agenda_cultural.json: {e}", exc_info=True)
        return jsonify({"error": "Error interno al leer la agenda."}), 500

@municipal_bp.route('/posts', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def create_municipal_post(current_user):
    """
    Crea un nuevo post municipal (evento o noticia) y lo guarda en agenda_cultural.json.
    """
    if current_user.tipo_chat != "municipio":
        return jsonify({"error": "Acceso denegado. Se requiere un usuario municipal."}), 403

    # --- Recopilar datos del formulario ---
    titulo = request.form.get('titulo')
    subtitulo = request.form.get('subtitulo')
    contenido = request.form.get('contenido')
    tipo_post = request.form.get('tipo_post', 'noticia')  # 'noticia', 'evento' o 'informacion'
    imagen_url_externa = request.form.get('imagen_url', '')
    enlace = request.form.get('enlace') or request.form.get('url')
    fecha_evento_inicio = request.form.get('fecha_evento_inicio')
    fecha_evento_fin = request.form.get('fecha_evento_fin')
    ubicacion = request.form.get('ubicacion')

    if not all([titulo, contenido, tipo_post]):
        return jsonify({"error": "El título, el contenido y el tipo de post son requeridos."}), 400

    # --- Manejo del archivo de imagen (flyer) ---
    flyer_image_url = ''
    if 'flyer_image' in request.files:
        file = request.files['flyer_image']
        if file.filename != '':
            filename = secure_filename(file.filename)
            # Use persistent data directory when available
            from services.config_loader import BASE_DATA_PATH
            upload_folder = os.path.join(BASE_DATA_PATH, 'archivos')
            os.makedirs(upload_folder, exist_ok=True)
            file_path = os.path.join(upload_folder, filename)
            file.save(file_path)
            # Generar una URL pública para el archivo.
            # Esto asume que 'data/archivos' es servido públicamente en '/static/archivos' o similar.
            # Para una app en producción, esto debería ser una URL de GCS o S3.
            flyer_image_url = f"/data/archivos/{filename}"

    # --- Construir el nuevo post ---
    nuevo_post = {
        "id": str(int(get_local_now().timestamp())), # ID simple basado en timestamp
        "titulo": titulo,
        "subtitulo": subtitulo,
        "descripcion": contenido, # Mapear 'contenido' a 'descripcion' para consistencia
        "tipo_post": tipo_post,
        "tags": [tipo_post],
        "imagen_url": flyer_image_url or imagen_url_externa,
        "fecha_evento_inicio": fecha_evento_inicio,
        "fecha_evento_fin": fecha_evento_fin,
        "fecha_publicacion": get_local_now().isoformat(),
        "enlace": enlace,
        "ubicacion": ubicacion,
    }

    # --- Leer, actualizar y escribir el archivo JSON ---
    from services.config_loader import BASE_CONFIG_PATH
    agenda_dir = os.path.join(BASE_CONFIG_PATH, 'default')
    os.makedirs(agenda_dir, exist_ok=True)
    agenda_path = os.path.join(agenda_dir, 'agenda_cultural.json')

    try:
        data = {"eventos": []}
        if os.path.exists(agenda_path):
            with open(agenda_path, 'r', encoding='utf-8') as f:
                contenido = f.read().strip()
                if contenido:
                    try:
                        data = json.loads(contenido)
                    except json.JSONDecodeError:
                        current_app.logger.warning("agenda_cultural.json corrupto, se recreará.")
        if 'eventos' not in data or not isinstance(data['eventos'], list):
            data['eventos'] = []

        data['eventos'].insert(0, nuevo_post)  # Insertar al principio para que aparezca primero

        # Mantener solo los 200 posts más recientes en el archivo
        data['eventos'] = data['eventos'][:200]

        with open(agenda_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return jsonify(nuevo_post), 201

    except Exception as e:
        current_app.logger.error(f"Error al actualizar agenda_cultural.json: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar el post."}), 500


@municipal_bp.route('/posts/bulk', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def create_municipal_posts_bulk(current_user):
    """Crea múltiples posts municipales a partir de una lista de eventos."""
    if current_user.tipo_chat != "municipio":
        return jsonify({"error": "Acceso denegado. Se requiere un usuario municipal."}), 403

    raw_payload = request.get_json(silent=True)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    events = payload.get("events")
    tipo_post = payload.get("tipo_post") or request.form.get("tipo_post") or "evento"
    text = None

    if events is None:
        text = payload.get("text") or (raw_payload if isinstance(raw_payload, str) else None) or request.form.get("text")
        body_text = request.get_data(as_text=True).strip()
        if events is None and not text and body_text:
            try:
                maybe_json = json.loads(body_text)
                if isinstance(maybe_json, dict):
                    events = maybe_json.get("events")
                    if text is None:
                        text = maybe_json.get("text")
                elif isinstance(maybe_json, list):
                    events = maybe_json
                else:
                    text = body_text
            except json.JSONDecodeError:
                text = body_text
        if events is None and text:
            from utils.agenda_parser import parse_agenda_text
            events = parse_agenda_text(text)

    if events is None and "file" in request.files:
        # Parse agenda from uploaded file (.txt or .docx)
        file = request.files["file"]
        from utils.agenda_parser import parse_agenda_text
        if file.filename.lower().endswith(".docx"):
            from docx import Document
            document = Document(file)
            text = "\n".join(p.text for p in document.paragraphs)
        else:
            text = file.read().decode("utf-8")
        events = parse_agenda_text(text)

    if not isinstance(events, list):
        return jsonify({"error": "Se requiere un JSON con la lista 'events' o un campo 'text' o archivo 'file'."}), 400

    from services.config_loader import BASE_CONFIG_PATH
    agenda_dir = os.path.join(BASE_CONFIG_PATH, 'default')
    os.makedirs(agenda_dir, exist_ok=True)
    agenda_path = os.path.join(agenda_dir, 'agenda_cultural.json')

    try:
        data = {"eventos": []}
        if os.path.exists(agenda_path):
            with open(agenda_path, 'r', encoding='utf-8') as f:
                contenido = f.read().strip()
                if contenido:
                    try:
                        data = json.loads(contenido)
                    except json.JSONDecodeError:
                        current_app.logger.warning("agenda_cultural.json corrupto, se recreará.")
        if 'eventos' not in data or not isinstance(data['eventos'], list):
            data['eventos'] = []

        created_posts = []
        for ev in events:
            title = ev.get("title") or ev.get("titulo")
            if not title:
                continue

            day = ev.get("day") or ev.get("dia")
            time_val = ev.get("time") or ev.get("hora") or ""
            descripcion = ev.get("description") or ev.get("descripcion", "")
            location = ev.get("location") or ev.get("ubicacion")
            image_url = ev.get("imagen_url") or ev.get("imagen", "")
            enlace = ev.get("enlace") or ev.get("url")

            post = {
                "id": str(uuid4()),
                "titulo": title,
                "subtitulo": day,
                "descripcion": descripcion,
                "tipo_post": tipo_post,
                "tags": [tipo_post],
                "imagen_url": image_url,
                "fecha_evento_inicio": f"{day or ''} {time_val}".strip(),
                "fecha_evento_fin": ev.get("fecha_evento_fin") or ev.get("fecha_fin"),
                "fecha_publicacion": get_local_now().isoformat(),
                "enlace": enlace,
                "ubicacion": location,
            }
            data['eventos'].insert(0, post)
            created_posts.append(post)

        # Limitar a los 200 eventos más recientes
        data['eventos'] = data['eventos'][:200]

        with open(agenda_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return jsonify({"created": created_posts}), 201

    except Exception as e:
        current_app.logger.error(f"Error al actualizar agenda_cultural.json: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar los posts."}), 500


@municipal_bp.route('/analytics', methods=['GET', 'OPTIONS'])
@token_requerido
@admin_o_empleado_requerido
def municipal_analytics(current_user):
    """Alias de ``/metrics`` para compatibilidad con el frontend."""

    if request.method == 'OPTIONS':
        return "", 204

    municipio_id = getattr(current_user, "municipio_id", None)
    filters, fecha_inicio, fecha_fin = _build_stats_filters_from_request(request.args)

    if filters:
        stats = build_stats_for_municipio(municipio_id, filters=filters)
    else:
        stats = build_stats_for_municipio(municipio_id)

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    metrics_raw = _municipal_message_metrics(
        eid, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin
    )

    if isinstance(metrics_raw, dict):
        metrics_cards = list(metrics_raw.get("cards", []) or [])
        metrics_summary = dict(metrics_raw.get("summary", {}) or {})
        metrics_payload = metrics_raw
    else:
        metrics_cards = list(metrics_raw or [])
        metrics_summary = {}
        metrics_payload = {"cards": metrics_cards, "summary": metrics_summary}

    response = {
        "stats": stats,
        "metrics": metrics_payload,
        "cards": metrics_cards,
        "summary": metrics_summary,
    }

    return jsonify(response)
