from flask import Blueprint, jsonify, request, current_app
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from datetime import datetime, timedelta
from utils.permissions import require_role
from routes.crm import _obtener_clientes
from services.municipio_responder import TODAS_LAS_CATEGORIAS_UNICAS
from routes.tramites import listar_tramites, obtener_tramite
from models import MunicipioTicket, db
from sqlalchemy import text

municipal_bp = Blueprint('municipal_legacy', __name__, url_prefix='/municipal')

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

@municipal_bp.route('/stats', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats(current_user):
    """Estadísticas profesionales del municipio del usuario."""

    mid = current_user.municipio_id

    abiertos = db.session.execute(
        text(
            "SELECT COUNT(*) FROM municipio_ticket "
            "WHERE municipio_id = :mid AND estado != 'cerrado'"
        ),
        {"mid": mid},
    ).scalar() or 0

    cerrados = db.session.execute(
        text(
            "SELECT COUNT(*) FROM municipio_ticket "
            "WHERE municipio_id = :mid AND estado = 'cerrado'"
        ),
        {"mid": mid},
    ).scalar() or 0

    rows = db.session.execute(
        text(
            "SELECT categoria, "
            "SUM(CASE WHEN estado != 'cerrado' THEN 1 ELSE 0 END) AS abiertos, "
            "SUM(CASE WHEN estado = 'cerrado' THEN 1 ELSE 0 END) AS cerrados "
            "FROM municipio_ticket WHERE municipio_id = :mid GROUP BY categoria"
        ),
        {"mid": mid},
    ).fetchall()
    por_categoria = [
        {
            "categoria": r.categoria,
            "abiertos": r.abiertos,
            "cerrados": r.cerrados,
        }
        for r in rows
    ]

    rows_distrito = db.session.execute(
        text(
            "SELECT distrito, COUNT(*) as total "
            "FROM municipio_ticket WHERE municipio_id = :mid "
            "GROUP BY distrito"
        ),
        {"mid": mid},
    ).fetchall()
    por_distrito = [
        {"distrito": r.distrito, "total": r.total}
        for r in rows_distrito
        if r.distrito is not None
    ]

    rows_mes = db.session.execute(
        text(
            "SELECT strftime('%Y-%m', fecha) AS mes, COUNT(*) as total "
            "FROM municipio_ticket WHERE municipio_id = :mid "
            "GROUP BY mes ORDER BY mes"
        ),
        {"mid": mid},
    ).fetchall()
    por_mes = [{"mes": r.mes, "total": r.total} for r in rows_mes]

    tiempo_respuesta = db.session.execute(
        text(
            "SELECT AVG(julianday(tc.fecha) - julianday(mt.fecha)) * 86400 "
            "FROM municipio_ticket mt JOIN ticket_comentario tc "
            "ON tc.municipio_ticket_id = mt.id "
            "WHERE tc.es_admin = 1 AND mt.municipio_id = :mid"
        ),
        {"mid": mid},
    ).scalar()

    datos = {
        "totales": {"abiertos": abiertos, "cerrados": cerrados},
        "por_categoria": por_categoria,
        "por_distrito": por_distrito,
        "por_mes": por_mes,
        "tiempo_respuesta_promedio_segundos": round(tiempo_respuesta or 0, 2),
    }

    return jsonify(datos)

@municipal_bp.route('/stats/filters', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats_filters(current_user):
    return jsonify({'categorias': TODAS_LAS_CATEGORIAS_UNICAS})

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


def _municipal_message_metrics(eid: int) -> list[dict]:
    """Calcula métricas de mensajes recibidos en distintos períodos."""

    ahora = datetime.now()

    def _contar_desde(dias: int) -> int:
        desde = ahora - timedelta(days=dias)
        return (
            db.session.execute(
                db.text(
                    "SELECT COUNT(*) FROM conversacion c JOIN user u ON c.user_id = u.id "
                    "WHERE u.empresa_id = :eid AND c.timestamp >= :desde"
                ),
                {"eid": eid, "desde": desde},
            ).scalar()
            or 0
        )

    return [
        {"label": "Mensajes esta semana", "value": _contar_desde(7)},
        {"label": "Mensajes este mes", "value": _contar_desde(30)},
        {"label": "Mensajes este año", "value": _contar_desde(365)},
    ]


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
        return jsonify(eventos), 200
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
    tipo_post = request.form.get('tipo_post', 'noticia') # 'noticia' o 'evento'
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
        "id": str(int(datetime.now().timestamp())), # ID simple basado en timestamp
        "titulo": titulo,
        "subtitulo": subtitulo,
        "descripcion": contenido, # Mapear 'contenido' a 'descripcion' para consistencia
        "tipo_post": tipo_post,
        "imagen_url": flyer_image_url or imagen_url_externa,
        "fecha_evento_inicio": fecha_evento_inicio,
        "fecha_evento_fin": fecha_evento_fin,
        "fecha_publicacion": datetime.now().isoformat(),
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

    payload = request.get_json(silent=True) or {}
    events = payload.get("events")

    if events is None:
        # Allow a raw agenda text via JSON or form field ``text``
        text = payload.get("text") or request.form.get("text")
        if text:
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
            title = ev.get("title")
            if not title:
                continue
            post = {
                "id": str(uuid4()),
                "titulo": title,
                "subtitulo": ev.get("day"),
                "descripcion": ev.get("description", ""),
                "tipo_post": "evento",
                "imagen_url": ev.get("imagen_url", ""),
                "fecha_evento_inicio": f"{ev.get('day', '')} {ev.get('time', '')}".strip(),
                "fecha_evento_fin": ev.get("fecha_evento_fin"),
                "fecha_publicacion": datetime.now().isoformat(),
                "enlace": ev.get("enlace"),
                "ubicacion": ev.get("location"),
            }
            data['eventos'].insert(0, post)
            created_posts.append(post)

        with open(agenda_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return jsonify({"created": created_posts}), 201

    except Exception as e:
        current_app.logger.error(f"Error al actualizar agenda_cultural.json: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar los posts."}), 500


@municipal_bp.route('/analytics', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_analytics(current_user):
    """Alias de ``/metrics`` para compatibilidad con el frontend."""

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    return jsonify(_municipal_message_metrics(eid))
