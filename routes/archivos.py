from flask import Blueprint, request, jsonify, current_app, send_from_directory
from routes.chat import cors_options_response
from extensions import db
from models import ArchivoAdjunto, User
import os
import uuid
from werkzeug.utils import secure_filename
from datetime import datetime
from routes.auth import token_requerido

archivos_bp = Blueprint('archivos_bp', __name__, url_prefix='/archivos')

UPLOAD_FOLDER = os.path.join('data', 'archivos')
# Extensiones permitidas para evitar archivos ejecutables sospechosos
ALLOWED_EXTENSIONS = {
    'jpg',
    'jpeg',
    'png',
    'pdf',
    'xlsx',
    'xls',
    'csv',
    'docx',
    'txt',
}

# Tipos MIME aceptados; cualquier otro se rechaza por seguridad
ALLOWED_MIME_PREFIXES = [
    'image/',
    'application/pdf',
    'application/msword',
    'application/vnd.',
    'text/plain',
]

# Tamaño máximo de archivo (10 MB)
MAX_FILE_SIZE = 10 * 1024 * 1024


def allowed_mime(mime: str) -> bool:
    return any(mime == pre or mime.startswith(pre) for pre in ALLOWED_MIME_PREFIXES)


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _tiene_permiso(user: User, adj: ArchivoAdjunto) -> bool:
    """Replica las verificaciones de obtener_archivo para chequear acceso."""
    if user.rol == 'admin':
        if hasattr(user, 'empresa_id') and adj.user_id != user.id:
            from models import User as UserModel
            owner = UserModel.query.filter_by(id=adj.user_id).first()
            if not owner or owner.empresa_id != user.id:
                return False
        return True
    elif user.rol == 'empleado':
        from models import PymeTicket, MunicipioTicket, User as UserModel
        if adj.pyme_ticket_id:
            ticket = PymeTicket.query.filter_by(id=adj.pyme_ticket_id).first()
            if not ticket or ticket.empresa_id != user.empresa_id:
                return False
        elif adj.municipio_ticket_id:
            ticket = MunicipioTicket.query.filter_by(id=adj.municipio_ticket_id).first()
            if not ticket or ticket.municipio_id != getattr(user, 'municipio_id', None):
                return False
        elif adj.user_id != user.id:
            owner = UserModel.query.filter_by(id=adj.user_id).first()
            if not owner or (
                owner.empresa_id != user.empresa_id
                and getattr(owner, 'municipio_id', None)
                != getattr(user, 'municipio_id', None)
            ):
                return False
        return True
    elif user.rol == 'usuario':
        return adj.user_id == user.id
    return False


def _meta_archivo(adj: ArchivoAdjunto) -> dict:
    return {
        "nombre": adj.nombre_original or adj.filename,
        "tipo": adj.mime,
        "tamano": adj.tamano,
        "fecha": adj.fecha.isoformat() if adj.fecha else None,
        "usuario_id": adj.user_id,
        "session_id": adj.session_id,
        "url": adj.url,
    }


@archivos_bp.route('/subir', methods=['OPTIONS'])
@archivos_bp.route('/subir/', methods=['OPTIONS'])
def subir_archivo_options():
    """Manejo de preflight CORS para /archivos/subir."""
    return cors_options_response()


@archivos_bp.route('/subir', methods=['POST'])
@token_requerido
def subir_archivo(current_user):
    if 'archivo' not in request.files:
        return jsonify({'error': 'No se envió archivo.'}), 400
    file = request.files['archivo']
    if file.filename == '':
        return jsonify({'error': 'Nombre de archivo vacío.'}), 400
    if request.content_length and request.content_length > MAX_FILE_SIZE:
        return jsonify({'error': 'Archivo demasiado grande (máx 10MB).'}), 400

    pyme_ticket_id = request.form.get("pyme_ticket_id")
    municipio_ticket_id = request.form.get("municipio_ticket_id")

    # Permisos: empleados solo pueden asociar archivos a tickets de su empresa/municipio
    if current_user.rol == 'empleado':
        from models import PymeTicket, MunicipioTicket
        if pyme_ticket_id:
            ticket = PymeTicket.query.filter_by(id=pyme_ticket_id).first()
            if not ticket or ticket.empresa_id != current_user.empresa_id:
                return jsonify({'error': 'No puede asociar archivos a tickets de otra empresa.'}), 403
        if municipio_ticket_id:
            ticket = MunicipioTicket.query.filter_by(id=municipio_ticket_id).first()
            if not ticket or ticket.municipio_id != getattr(current_user, 'municipio_id', None):
                return jsonify({'error': 'No puede asociar archivos a tickets de otro municipio.'}), 403

    if file and allowed_file(file.filename) and allowed_mime(file.mimetype):
        original = secure_filename(file.filename)
        unique = f"{uuid.uuid4().hex}_{original}"
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        save_path = os.path.join(UPLOAD_FOLDER, unique)
        file.save(save_path)
        tamano = os.path.getsize(save_path)
        url = f"/archivos/{unique}"
        session_id = request.form.get("session_id") or request.headers.get("X-Session-Id")
        tipo = request.form.get("tipo", "chat")
        db.session.add(
            ArchivoAdjunto(
                user_id=current_user.id,
                session_id=session_id,
                filename=unique,
                nombre_original=original,
                mime=file.mimetype,
                tamano=tamano,
                tipo=tipo,
                pyme_ticket_id=pyme_ticket_id,
                municipio_ticket_id=municipio_ticket_id,
                url=url,
            )
        )
        db.session.commit()
        current_app.logger.info(
            f"Archivo subido por user {current_user.id}: {unique} ({original})"
        )
        return jsonify({'mensaje': 'Archivo subido', 'filename': unique, 'url': url}), 200
    return jsonify({'error': 'Formato no permitido o tipo no permitido.'}), 400


@archivos_bp.route('/<path:filename>', methods=['GET'])
@token_requerido
def obtener_archivo(current_user: User, filename):
    """Devuelve el archivo subido anteriormente, con control de permisos."""
    adj = ArchivoAdjunto.query.filter_by(filename=filename).first()
    if not adj:
        return jsonify({'error': 'Archivo no encontrado'}), 404

    # Admin puede ver archivos de su empresa
    if current_user.rol == 'admin':
        if hasattr(current_user, 'empresa_id') and adj.user_id != current_user.id:
            # Si el archivo fue subido por otro usuario, verificar que sea de la misma empresa
            from models import User as UserModel
            owner = UserModel.query.filter_by(id=adj.user_id).first()
            if not owner or owner.empresa_id != current_user.id:
                return jsonify({'error': 'Acceso denegado'}), 403

    # Empleado de empresa: solo archivos de tickets de su empresa o propios
    elif current_user.rol == 'empleado':
        from models import PymeTicket, MunicipioTicket, User as UserModel
        # Si es archivo de ticket pyme
        if adj.pyme_ticket_id:
            ticket = PymeTicket.query.filter_by(id=adj.pyme_ticket_id).first()
            if not ticket or ticket.empresa_id != current_user.empresa_id:
                return jsonify({'error': 'Acceso denegado'}), 403
        # Si es archivo de ticket municipio
        elif adj.municipio_ticket_id:
            ticket = MunicipioTicket.query.filter_by(id=adj.municipio_ticket_id).first()
            if not ticket or ticket.municipio_id != getattr(current_user, 'municipio_id', None):
                return jsonify({'error': 'Acceso denegado'}), 403
        # Si es archivo propio
        elif adj.user_id != current_user.id:
            # Solo puede ver archivos propios o de tickets de su empresa/municipio
            owner = UserModel.query.filter_by(id=adj.user_id).first()
            if not owner or (owner.empresa_id != current_user.empresa_id and getattr(owner, 'municipio_id', None) != getattr(current_user, 'municipio_id', None)):
                return jsonify({'error': 'Acceso denegado'}), 403

    # Usuario común: solo sus propios archivos
    elif current_user.rol == 'usuario':
        if adj.user_id != current_user.id:
            return jsonify({'error': 'Acceso denegado'}), 403

    else:
        return jsonify({'error': 'Permiso denegado.'}), 403

    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)


@archivos_bp.route('/sesion/<session_id>', methods=['OPTIONS'])
@archivos_bp.route('/sesion/<session_id>/', methods=['OPTIONS'])
def archivos_sesion_options(session_id):
    """Manejo de preflight CORS para /archivos/sesion/<id>."""
    return cors_options_response()


@archivos_bp.route('/sesion/<session_id>', methods=['GET'])
@token_requerido
def archivos_por_sesion(current_user: User, session_id: str):
    """Lista los archivos de chat asociados a la sesión indicada."""
    adjuntos = (
        ArchivoAdjunto.query.filter_by(session_id=session_id, tipo="chat")
        .order_by(ArchivoAdjunto.fecha.asc())
        .all()
    )
    visibles = [_meta_archivo(a) for a in adjuntos if _tiene_permiso(current_user, a)]
    return jsonify(visibles)


@archivos_bp.after_request
def apply_cors(response):
    origin = request.headers.get('Origin')
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
    else:
        response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = (
        'Authorization, Content-Type, Origin, Accept, Anon-Id, x-entity-token'
    )
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response
