from flask import Blueprint, jsonify, request, current_app
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from datetime import datetime, timedelta
from utils.permissions import require_role
from routes.crm import _obtener_clientes
from services.municipios import TODAS_LAS_CATEGORIAS_UNICAS
from routes.tramites import listar_tramites, obtener_tramite
from models import MunicipioTicket, db, User
from sqlalchemy import text
import os
import json
import uuid

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
    abiertos = db.session.execute(text("SELECT COUNT(*) FROM municipio_ticket WHERE municipio_id = :mid AND estado != 'cerrado'"), {"mid": mid}).scalar() or 0
    cerrados = db.session.execute(text("SELECT COUNT(*) FROM municipio_ticket WHERE municipio_id = :mid AND estado = 'cerrado'"), {"mid": mid}).scalar() or 0
    rows = db.session.execute(text("SELECT categoria, SUM(CASE WHEN estado != 'cerrado' THEN 1 ELSE 0 END) AS abiertos, SUM(CASE WHEN estado = 'cerrado' THEN 1 ELSE 0 END) AS cerrados FROM municipio_ticket WHERE municipio_id = :mid GROUP BY categoria"), {"mid": mid}).fetchall()
    por_categoria = [{"categoria": r.categoria, "abiertos": r.abiertos, "cerrados": r.cerrados} for r in rows]
    tiempo_respuesta = db.session.execute(text("SELECT AVG(julianday(tc.fecha) - julianday(mt.fecha)) * 86400 FROM municipio_ticket mt JOIN ticket_comentario tc ON tc.municipio_ticket_id = mt.id WHERE tc.es_admin = 1 AND mt.municipio_id = :mid"), {"mid": mid}).scalar()
    datos = {"totales": {"abiertos": abiertos, "cerrados": cerrados}, "por_categoria": por_categoria, "tiempo_respuesta_promedio_segundos": round(tiempo_respuesta or 0, 2)}
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
    from services.ticket_service import servicio_tickets
    municipio_id_del_admin = current_user.municipio_id
    if not municipio_id_del_admin:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400
    tickets_con_ubicacion = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(tipo_ticket="municipio", municipio_id=municipio_id_del_admin, estado="abierto")
    return jsonify(tickets_con_ubicacion)

@municipal_bp.route('/incidents', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_incidents(current_user):
    try:
        tickets = (MunicipioTicket.query.filter_by(municipio_id=current_user.municipio_id).filter(MunicipioTicket.estado != 'cerrado').order_by(MunicipioTicket.fecha.desc()).all())
    except Exception:
        current_app.logger.exception("Error fetching municipal incidents")
        tickets = []
    resultado = [{"id": t.id, "nro_ticket": t.nro_ticket, "asunto": getattr(t, "asunto", "N/A"), "categoria": getattr(t, "categoria", None), "estado": t.estado, "fecha": t.fecha.isoformat() if getattr(t, "fecha", None) else None, "pregunta": getattr(t, "pregunta", None), "detalles": getattr(t, "detalles", None), "direccion": getattr(t, "direccion", None), "latitud": getattr(t, "latitud", None), "longitud": getattr(t, "longitud", None), "archivo_url": getattr(t, "archivo_url", None), "nombre_vecino": getattr(t, "nombre_vecino", None), "telefono_vecino": getattr(t, "telefono_vecino", None), "email_vecino": getattr(t, "email_vecino", None)} for t in tickets]
    return jsonify(resultado)

def _get_posts_path(municipio_id):
    safe_municipio_id = str(municipio_id)
    return os.path.join(os.path.dirname(__file__), '..', 'data', 'municipios', safe_municipio_id, 'posts.json')

@municipal_bp.route('/posts', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def list_posts(current_user):
    if not current_user.municipio_id:
        return jsonify({"error": "User not associated with a municipality"}), 400
    posts_path = _get_posts_path(current_user.municipio_id)
    try:
        if not os.path.exists(posts_path):
            return jsonify([])
        with open(posts_path, 'r', encoding='utf-8') as f:
            posts = json.load(f)
        posts.sort(key=lambda x: x.get('fecha_publicacion', ''), reverse=True)
        return jsonify(posts)
    except Exception as e:
        current_app.logger.error(f"Error reading posts file for municipio {current_user.municipio_id}: {e}")
        return jsonify({"error": "Could not read posts file"}), 500

@municipal_bp.route('/posts', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def add_post(current_user):
    if not current_user.municipio_id:
        return jsonify({"error": "User not associated with a municipality"}), 400

    flyer_url = None
    data = {}

    content_type = request.content_type.split(';')[0]

    if content_type == 'application/json':
        data = request.get_json()
    elif content_type == 'multipart/form-data':
        data = request.form.to_dict()
        if 'file' in request.files:
            file = request.files['file']
            if file and file.filename: # Check if a file was actually uploaded
                from services.archivo_service import guardar_archivo
                # Use the main user of the municipality for ownership
                owner_user = User.query.get(current_user.municipio_id) or current_user
                saved_file_info = guardar_archivo(file, owner_user)
                if not saved_file_info.get("success"):
                    return jsonify({"error": "Failed to save flyer image", "details": saved_file_info.get("message")}), 500
                flyer_url = saved_file_info.get("url")
    else:
        return jsonify({"error": "Unsupported Media Type", "sent_content_type": request.content_type}), 415


    titulo = data.get('titulo')
    descripcion = data.get('descripcion')
    post_type = data.get('tipo', 'general')

    if not titulo or not descripcion:
        return jsonify({"error": "Missing required fields: titulo and descripcion"}), 400

    new_post = {
        "id": str(uuid.uuid4()),
        "titulo": titulo,
        "descripcion": descripcion,
        "link": data.get('link', ''),
        "tipo": post_type,
        "flyer_url": flyer_url,
        "fecha_publicacion": datetime.utcnow().isoformat()
    }

    posts_path = _get_posts_path(current_user.municipio_id)
    os.makedirs(os.path.dirname(posts_path), exist_ok=True)

    try:
        posts = []
        if os.path.exists(posts_path):
            with open(posts_path, 'r', encoding='utf-8') as f:
                content = f.read()
                if content:
                    posts = json.loads(content)

        posts.insert(0, new_post)

        if len(posts) > 10:
            posts = posts[:10]

        with open(posts_path, 'w', encoding='utf-8') as f:
            json.dump(posts, f, indent=4, ensure_ascii=False)

        return jsonify(new_post), 201
    except Exception as e:
        current_app.logger.error(f"Error writing to posts file for municipio {current_user.municipio_id}: {e}")
        return jsonify({"error": "Could not save the new post"}), 500

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


@municipal_bp.route('/analytics', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_analytics(current_user):
    """Alias de ``/metrics`` para compatibilidad con el frontend."""

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    return jsonify(_municipal_message_metrics(eid))
