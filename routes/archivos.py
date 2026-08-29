from flask import Blueprint, request, jsonify, current_app, send_from_directory, make_response
from extensions import db
from models import ArchivoAdjunto, MunicipioTicket, PymeTicket, TenantProfile, User, AnalisisArchivo
import os
import uuid
from werkzeug.utils import secure_filename
from datetime import datetime
from utils.auth_helpers import anon_o_token_requerido
from routes.auth import token_requerido
from services.gcs_service import (
    upload_to_gcs,
    BUCKET_NAME,
    MAX_FILE_SIZE,
    resolve_attachment_thumb_url,
)
from services.attachment_delivery import serialize_attachment_for_delivery
from services.attachment_service import create_attachment_with_thumbnail
from services.archivo_service import guardar_archivo_adjunto_ticket
from services.employee_ticket_access import employee_ticket_category_access_allows
from services.ticket_service import servicio_tickets
from services.tenant_ticket_scope import (
    municipio_ticket_belongs_to_tenant,
    resolve_unique_tenant_for_owner,
)
from utils.permissions import require_role
from utils.roles import is_authorized_superadmin_user
from utils.lazy_module import LazyModule

storage = LazyModule("google.cloud.storage")


class _LazyFileAnalysisTask:
    def delay(self, *args, **kwargs):
        from services.analisis_archivo_service import (
            tarea_analizar_contenido_archivo as implementation,
        )

        return implementation.delay(*args, **kwargs)


tarea_analizar_contenido_archivo = _LazyFileAnalysisTask()


def analyze_image_from_content(*args, **kwargs):
    from services.google_vision_service import analyze_image_from_content as implementation

    return implementation(*args, **kwargs)


def procesar_catalogo_pdf_google(*args, **kwargs):
    from services.google_docai import procesar_catalogo_pdf_google as implementation

    return implementation(*args, **kwargs)


def procesar_catalogo_imagen_google(*args, **kwargs):
    from services.google_docai import procesar_catalogo_imagen_google as implementation

    return implementation(*args, **kwargs)

archivos_bp = Blueprint('archivos_bp', __name__, url_prefix='/archivos')
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
    'json', # Añadido json
    'mp3', 'wav', 'ogg', 'oga', 'm4a',
}

# Tipos MIME aceptados; cualquier otro se rechaza por seguridad
ALLOWED_MIME_PREFIXES = [
    'image/',
    'application/pdf',
    'application/msword',
    'application/vnd.', # Para .xlsx, .docx etc.
    'text/plain',
    'text/csv', # Añadido para CSV explícitamente si no lo cubre vnd
    'application/json', # Añadido para JSON
    'audio/',
]

# Tamaño máximo de archivo (10 MB) por archivo
MAX_FILE_SIZE = 10 * 1024 * 1024


# Definition of the new cors_options_response function
def cors_options_response():
    response = make_response(jsonify({}))
    origin = request.headers.get('Origin')
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
    else:
        # Consider if '*' is appropriate or if a more specific origin list should be used
        response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = (
        'Authorization, Content-Type, Origin, Accept, X-Anon-Id, Anon-Id, '
        'X-Entity-Token, X-Widget-Token, X-Tenant-Slug, X-Chat-Session-Id, '
        'X-Demo-Session-Id, X-Demo-Session, Idempotency-Key, X-Request-Id'
    )
    # Ensure all methods intended to be covered by CORS are listed, including OPTIONS itself
    # The methods listed here should ideally match or be a superset of those in apply_cors for consistency
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS' 
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response


def allowed_mime(mime: str) -> bool:
    return any(mime == pre or mime.startswith(pre) for pre in ALLOWED_MIME_PREFIXES)


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _municipio_tenant_for_actor(user: User) -> TenantProfile | None:
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id:
        tenant = db.session.get(TenantProfile, tenant_id)
        if tenant is not None:
            return tenant

    owner_ids = [
        getattr(user, "municipio_id", None),
        getattr(user, "empresa_id", None),
    ]
    if getattr(user, "tipo_chat", None) == "municipio":
        owner_ids.append(getattr(user, "id", None))
    resolved_tenants: dict[int, TenantProfile] = {}
    for owner_id in dict.fromkeys(value for value in owner_ids if value):
        try:
            resolution = resolve_unique_tenant_for_owner(owner_id)
        except ValueError:
            return None
        if resolution.status != "unique" or resolution.tenant is None:
            return None
        resolved_tenants[int(resolution.tenant.id)] = resolution.tenant
    if len(resolved_tenants) != 1:
        return None
    return next(iter(resolved_tenants.values()))


def _municipio_ticket_access_allowed(user: User, ticket: MunicipioTicket | None) -> bool:
    tenant = _municipio_tenant_for_actor(user)
    return municipio_ticket_belongs_to_tenant(ticket, tenant)


def _ticket_for_actor(
    user: User,
    ticket_id: object,
    tipo_ticket: str,
) -> MunicipioTicket | PymeTicket | None:
    """Return a ticket only when its exact tenant scope authorizes the actor."""

    if tipo_ticket not in {"municipio", "pyme"}:
        return None
    try:
        normalized_ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return None
    if normalized_ticket_id <= 0:
        return None

    TicketModel = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
    ticket = db.session.get(TicketModel, normalized_ticket_id)
    if ticket is None:
        return None
    if is_authorized_superadmin_user(user):
        return ticket

    tenant = _municipio_tenant_for_actor(user)
    if tenant is None:
        return None
    if tipo_ticket == "municipio":
        allowed = municipio_ticket_belongs_to_tenant(ticket, tenant)
    else:
        allowed = getattr(ticket, "tenant_id", None) == tenant.id
    if not allowed:
        return None

    if getattr(user, "rol", None) == "usuario" and ticket.user_id != user.id:
        return None
    if not employee_ticket_category_access_allows(user, ticket):
        return None
    return ticket


def _tiene_permiso(user: User, adj: ArchivoAdjunto) -> bool:
    """Replica las verificaciones de obtener_archivo para chequear acceso."""
    if adj.pyme_ticket_id:
        if _ticket_for_actor(user, adj.pyme_ticket_id, "pyme") is None:
            return False
    if adj.municipio_ticket_id:
        if _ticket_for_actor(user, adj.municipio_ticket_id, "municipio") is None:
            return False
    if is_authorized_superadmin_user(user):
        return True
    if user.rol == 'admin':
        if hasattr(user, 'empresa_id') and adj.user_id != user.id:
            from models import User as UserModel
            owner = UserModel.query.filter_by(id=adj.user_id).first()
            if not owner or owner.empresa_id != user.id:
                return False
        return True
    elif user.rol == 'empleado':
        from models import PymeTicket, MunicipioTicket, User as UserModel
        if not adj.pyme_ticket_id and not adj.municipio_ticket_id and adj.user_id != user.id:
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
    # El frontend puede enviar un solo "archivo" o una lista "archivos".
    files = request.files.getlist("archivos")
    if not files:
        single = request.files.get("archivo")
        if single:
            files = [single]

    # Si no se encontró lista "archivos", intentar con el campo singular.
    if not files:
        single = request.files.get("archivo")
        if single:
            files = [single]

    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No se enviaron archivos o nombres de archivo vacíos.'}), 400

    # Validar cada archivo antes de procesar
    for file_to_check in files:
        # La validación de content_length total es más compleja para múltiples archivos.
        # MAX_FILE_SIZE se aplicará por archivo.
        # file_to_check.seek(0, os.SEEK_END)
        # file_size = file_to_check.tell()
        # file_to_check.seek(0) # Resetear puntero del archivo
        # if file_size > MAX_FILE_SIZE:
        #     return jsonify({'error': f'Archivo "{file_to_check.filename}" demasiado grande (máx 10MB).'}), 400
        # Nota: Werkzeug FileStorage no tiene un método simple para obtener el tamaño antes de leerlo todo
        # o guardarlo. request.content_length es para toda la request.
        # La validación de tamaño se hará después de guardar o se confiará en el frontend,
        # o se leerá en memoria si es estrictamente necesario (no ideal para archivos grandes).
        # Por ahora, la validación de MAX_FILE_SIZE se omite aquí para el chequeo individual previo
        # y se verificará después de guardar, o se asume que el cliente lo valida.
        # El request.content_length total sí podría chequearse contra N * MAX_FILE_SIZE como un sanity check.

        if not allowed_file(file_to_check.filename):
            return jsonify({'error': f'Archivo "{file_to_check.filename}": formato no permitido.'}), 400
        if not allowed_mime(file_to_check.mimetype):
            return jsonify({'error': f'Archivo "{file_to_check.filename}": tipo MIME no permitido ({file_to_check.mimetype}).'}), 400

    pyme_ticket_id = request.form.get("pyme_ticket_id")
    municipio_ticket_id = request.form.get("municipio_ticket_id")
    session_id = request.form.get("session_id") or request.headers.get("X-Session-Id")
    tipo_adjunto = request.form.get("tipo", "chat") # tipo de archivo (ej. chat, ticket_adjunto, etc.)

    if pyme_ticket_id and municipio_ticket_id:
        return jsonify({'error': 'Solo puede asociarse un ticket por archivo.'}), 400
    if pyme_ticket_id:
        ticket = _ticket_for_actor(current_user, pyme_ticket_id, "pyme")
        if ticket is None:
            return jsonify({'error': 'Ticket no encontrado.'}), 404
        pyme_ticket_id = ticket.id
    if municipio_ticket_id:
        ticket = _ticket_for_actor(current_user, municipio_ticket_id, "municipio")
        if ticket is None:
            return jsonify({'error': 'Ticket no encontrado.'}), 404
        municipio_ticket_id = ticket.id

    resultados_subida = []
    archivos_guardados_info = [] # Para rollback en caso de error parcial

    for file in files:
        if file.filename == '':
            continue

        upload_result = upload_to_gcs(file)

        if not upload_result:
            # Rollback previous successful uploads if any
            # Note: This requires a delete function in gcs_service
            # For now, we log the issue. A more robust implementation would clean up.
            current_app.logger.error(f"Upload failed for {secure_filename(file.filename)}. Previously uploaded files in this batch may not be cleaned up automatically.")
            return jsonify({'error': f'Error al subir el archivo {secure_filename(file.filename)}.'}), 500

        archivos_guardados_info.append(upload_result)

    # Si todos los archivos se guardaron bien, ahora los registramos en la BD
    for agi in archivos_guardados_info:
        url = agi['public_url']
        nuevo_adjunto = ArchivoAdjunto(
            user_id=current_user.id,
            session_id=session_id,
            filename=agi['unique_name'],
            nombre_original=agi['original_name'],
            mime=agi['mimetype'],
            tamano=agi['size'],
            tipo=tipo_adjunto,
            pyme_ticket_id=pyme_ticket_id if pyme_ticket_id else None,
            municipio_ticket_id=municipio_ticket_id if municipio_ticket_id else None,
            url=url,
        )
        db.session.add(nuevo_adjunto)

        try:
            db.session.commit()  # Commit por cada archivo para obtener ID para la tarea

            # Encolar tarea de análisis de archivo
            try:
                tarea_analizar_contenido_archivo.delay(nuevo_adjunto.id)
                current_app.logger.info(
                    f"Tarea de análisis encolada para ArchivoAdjunto ID: {nuevo_adjunto.id}"
                )
            except Exception as e_celery:
                current_app.logger.error(
                    f"Error al encolar tarea de análisis para ArchivoAdjunto ID: {nuevo_adjunto.id}. Error: {e_celery}",
                    exc_info=True,
                )
                # No revertimos la subida, solo logueamos el error de encolado

            current_app.logger.info(
                f"Archivo subido por user {current_user.id}: {agi['unique_name']} ({agi['original_name']}). ID: {nuevo_adjunto.id}"
            )

            # Procesar el archivo con Document AI si es un PDF o una imagen (solo si hay path disponible)
            extracted_data = None
            if agi.get('path') and agi['mimetype'] == 'application/pdf':
                extracted_data = procesar_catalogo_pdf_google(agi['path'], current_user.id)
            elif agi.get('path') and agi['mimetype'].startswith('image/'):
                extracted_data = procesar_catalogo_imagen_google(agi['path'], current_user.id)

            resultados_subida.append(
                {
                    'filename': agi['unique_name'],
                    'id': nuevo_adjunto.id,
                    'name': agi['original_name'],
                    'mimeType': agi['mimetype'],
                    'size': agi['size'],
                    'url': url,
                    'extracted_data': extracted_data,
                }
            )
        except Exception as e_db:
            db.session.rollback()
            current_app.logger.error(
                f"Error al registrar en BD el archivo {agi.get('original_name', 'desconocido')}: {e_db}",
                exc_info=True,
            )
            # Eliminar el archivo físico que se guardó pero no se pudo registrar en BD
            try:
                storage_client = storage.Client()
                storage_client.bucket(BUCKET_NAME).blob(agi['unique_name']).delete()
            except Exception as e_delete:
                current_app.logger.error(
                    f"Error al eliminar archivo {agi['unique_name']} de GCS durante el rollback: {e_delete}",
                    exc_info=True,
                )

    if not resultados_subida and archivos_guardados_info:
        for agi in archivos_guardados_info:
            try:
                storage_client = storage.Client()
                storage_client.bucket(BUCKET_NAME).blob(agi['unique_name']).delete()
            except Exception as e_delete:
                current_app.logger.error(
                    f"Error al eliminar archivo {agi['unique_name']} de GCS durante el rollback: {e_delete}",
                    exc_info=True,
                )
        return jsonify({'error': 'Error al procesar archivos en la base de datos después de guardarlos.'}), 500

    if not resultados_subida and not files: # Si no se enviaron archivos válidos desde el principio
         return jsonify({'error': 'No se proporcionaron archivos válidos.'}), 400

    if not resultados_subida and archivos_guardados_info:
        # This case is already handled above, but as a safeguard:
        return jsonify({'error': 'Error al procesar archivos en la base de datos después de guardarlos.'}), 500

    if not resultados_subida:
        return jsonify({'error': 'No se proporcionaron archivos válidos o no se pudieron procesar.'}), 400

    # New response format as per frontend directives
    if len(resultados_subida) == 1:
        # If only one file was uploaded, return its object directly
        return jsonify(resultados_subida[0]), 200
    else:
        # If multiple files were uploaded, return a list of their objects
        return jsonify(resultados_subida), 200


@archivos_bp.route('/subir_imagen', methods=['POST'])
@token_requerido
def subir_imagen(current_user):
    if 'archivo' not in request.files:
        return jsonify({'error': 'No se encontró el archivo'}), 400

    file = request.files['archivo']

    if file.filename == '':
        return jsonify({'error': 'No se seleccionó ningún archivo'}), 400

    if file and allowed_file(file.filename) and allowed_mime(file.mimetype):
        upload_result = upload_to_gcs(file)

        if not upload_result:
            return jsonify({'error': 'Error al subir la imagen.'}), 500

        try:
            nuevo_adjunto = ArchivoAdjunto(
                user_id=current_user.id,
                filename=upload_result['unique_name'],
                nombre_original=upload_result['original_name'],
                mime=upload_result['mimetype'],
                tamano=upload_result['size'],
                tipo='imagen',
                url=upload_result['public_url'],
            )
            db.session.add(nuevo_adjunto)
            db.session.commit()

            # For analysis, we need the file content. Read it from the FileStorage object.
            file.seek(0)
            image_content = file.read()
            analysis_result = analyze_image_from_content(image_content)

            return jsonify({
                'mensaje': 'Imagen subida y analizada correctamente.',
                'archivo': {
                    'filename': upload_result['unique_name'],
                    'id': nuevo_adjunto.id,
                    'name': upload_result['original_name'],
                    'mimeType': upload_result['mimetype'],
                    'size': upload_result['size'],
                    'url': upload_result['public_url']
                },
                'analisis': analysis_result
            }), 200

        except Exception as e:
            current_app.logger.error(f"Error al procesar la imagen después de subirla a GCS: {e}", exc_info=True)
            # Optional: Add logic to delete the file from GCS if DB operation fails
            return jsonify({'error': 'Error al procesar la imagen.'}), 500

    return jsonify({'error': 'Formato de archivo no permitido'}), 400


@archivos_bp.route('/subir_admin', methods=['POST'])
@token_requerido
@require_role('admin', 'empleado')
def subir_archivo_admin(current_user: User):
    """
    Endpoint específico para que administradores/empleados suban archivos a un ticket existente.
    Crea tanto el ArchivoAdjunto como el TicketComentario asociado.
    """
    if 'archivo' not in request.files:
        return jsonify({'error': 'No se encontró el archivo'}), 400

    file = request.files['archivo']
    ticket_id = request.form.get('ticket_id')
    tipo_ticket = request.form.get('tipo_ticket') # 'municipio' o 'pyme'

    if not all([file, ticket_id, tipo_ticket]):
        return jsonify({'error': 'Faltan datos: se requiere archivo, ticket_id y tipo_ticket.'}), 400

    if tipo_ticket not in {'municipio', 'pyme'}:
        return jsonify({'error': 'Tipo de ticket no válido.'}), 400

    ticket = _ticket_for_actor(current_user, ticket_id, tipo_ticket)
    if ticket is None:
        return jsonify({'error': 'Ticket no encontrado.'}), 404
    ticket_id = ticket.id

    if not allowed_file(file.filename) or not allowed_mime(file.mimetype):
        return jsonify({'error': 'Tipo de archivo no permitido.'}), 400

    # Lógica de guardado y creación de comentario
    try:
        adjunto = guardar_archivo_adjunto_ticket(file, current_user.id, ticket_id, tipo_ticket)
        if not adjunto:
            return jsonify({'error': 'No se pudo guardar el archivo adjunto.'}), 500

        # Crear el comentario que representa este archivo en el chat
        comentario_texto = f"[Archivo adjunto: {adjunto.nombre_original}]"
        comentario = servicio_tickets.crear_comentario(
            ticket_id=ticket_id,
            tipo_ticket=tipo_ticket,
            comentario_data={
                "comentario": comentario_texto,
                "user_id": current_user.id,
                "es_admin": True,
                "archivo_adjunto_id": adjunto.id,
                "origen": "chat" # Marcar como originado desde el chat
            }
        )
        if not comentario:
            # Aquí deberíamos idealmente borrar el adjunto que quedó huérfano.
            # Por ahora, solo logueamos el error.
            current_app.logger.error(f"Se guardó el adjunto {adjunto.id} pero falló la creación de su comentario en el ticket {ticket_id}.")
            return jsonify({'error': 'El archivo fue guardado pero no se pudo asociar al chat.'}), 500

        db.session.commit()

        # Devolver el comentario serializado, que ya incluye 'attachmentInfo'
        return jsonify(comentario.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error en subir_archivo_admin para ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({'error': 'Error interno al procesar el archivo.'}), 500


@archivos_bp.route('/<path:filename>', methods=['GET'])
@token_requerido
def obtener_archivo(current_user: User, filename):
    """Devuelve el archivo subido anteriormente, con control de permisos."""
    adj = ArchivoAdjunto.query.filter_by(filename=filename).first()
    if not adj:
        return jsonify({'error': 'Archivo no encontrado'}), 404

    if adj.pyme_ticket_id:
        if _ticket_for_actor(current_user, adj.pyme_ticket_id, "pyme") is None:
            return jsonify({'error': 'Archivo no encontrado'}), 404
    if adj.municipio_ticket_id:
        if _ticket_for_actor(current_user, adj.municipio_ticket_id, "municipio") is None:
            return jsonify({'error': 'Archivo no encontrado'}), 404

    # Superadmin autorizado conserva acceso global.
    if is_authorized_superadmin_user(current_user):
        pass
    # Admin puede ver archivos de su empresa
    elif current_user.rol == 'admin':
        if hasattr(current_user, 'empresa_id') and adj.user_id != current_user.id:
            # Si el archivo fue subido por otro usuario, verificar que sea de la misma empresa
            from models import User as UserModel
            owner = UserModel.query.filter_by(id=adj.user_id).first()
            if not owner or owner.empresa_id != current_user.id:
                return jsonify({'error': 'Acceso denegado'}), 403

    # Empleado de empresa: solo archivos de tickets de su empresa o propios
    elif current_user.rol == 'empleado':
        from models import User as UserModel
        # Si es archivo propio y no está asociado a un ticket
        if not adj.pyme_ticket_id and not adj.municipio_ticket_id and adj.user_id != current_user.id:
            # Solo puede ver archivos propios o de tickets de su empresa/municipio
            from models import User as UserModel
            owner = UserModel.query.filter_by(id=adj.user_id).first()
            if not owner or (owner.empresa_id != current_user.empresa_id and owner.municipio_id != current_user.municipio_id):
                return jsonify({'error': 'Acceso denegado'}), 403

    # Usuario común: solo sus propios archivos
    elif current_user.rol == 'usuario':
        if adj.user_id != current_user.id:
            return jsonify({'error': 'Acceso denegado'}), 403

    else:
        return jsonify({'error': 'Permiso denegado.'}), 403

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)

        if not blob.exists():
            return jsonify({'error': 'Archivo no encontrado en el almacenamiento.'}), 404

        response = make_response(blob.download_as_bytes())
        response.headers['Content-Type'] = adj.mime
        response.headers['Content-Disposition'] = f'attachment; filename="{adj.nombre_original}"'
        return response

    except Exception as e:
        current_app.logger.error(f"Error al descargar el archivo {filename} de GCS: {e}", exc_info=True)
        return jsonify({'error': 'Error al descargar el archivo.'}), 500


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
        'Authorization, Content-Type, Origin, Accept, X-Anon-Id, Anon-Id, '
        'X-Entity-Token, X-Widget-Token, X-Tenant-Slug, X-Chat-Session-Id, '
        'X-Demo-Session-Id, X-Demo-Session, Idempotency-Key, X-Request-Id'
    )
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

# --- Nuevo Endpoint para Chat Widget ---

ALLOWED_CHAT_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "application/pdf",
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "audio/oga",
    "audio/webm",
    "audio/mp4",
    "audio/m4a",
    "audio/aac",
    "video/webm",
    "video/ogg",
    "video/mp4",
}

@archivos_bp.route('/upload/chat_attachment', methods=['OPTIONS'])
def upload_chat_attachment_options():
    """Manejo de preflight CORS para /upload/chat_attachment."""
    return cors_options_response()

@archivos_bp.route('/upload/chat_attachment', methods=['POST'])
@anon_o_token_requerido
def upload_chat_attachment(current_user=None, anon_id=None, owner_user=None):
    """
    Endpoint para que el ChatWidget suba un archivo.
    No lo asocia a ningún ticket, solo lo sube y crea los registros.
    Devuelve la metadata para que el frontend la use en la llamada a /ask.
    """
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex

    def _json(payload: dict, status: int = 200):
        body = dict(payload)
        body.setdefault("request_id", request_id)
        response = jsonify(body)
        response.status_code = status
        response.headers["X-Request-Id"] = request_id
        return response

    if 'file' not in request.files:
        return _json({"error": "No se encontro el campo de archivo file"}, 400)

    file = request.files['file']

    if file.filename == '':
        return _json({"error": "No se selecciono ningun archivo"}, 400)

    # Validaciones de seguridad
    raw_mime_type = file.content_type or file.mimetype or ""
    normalized_mime_type = raw_mime_type.split(";", 1)[0].strip().lower()

    if normalized_mime_type not in ALLOWED_CHAT_MIMES:
        display_mime = raw_mime_type or "desconocido"
        return _json({"error": f"Tipo de archivo no permitido: {display_mime}"}, 400)

    # El tamaño se valida dentro de gcs_service

    try:
        user = current_user or owner_user
        user_id = user.id if user else None
        session_id = request.headers.get("X-Chat-Session-Id")

        if not session_id:
             current_app.logger.warning("X-Chat-Session-Id header is missing.")
             # Consider returning an error if session ID is strictly required
             # return jsonify({'error': 'X-Chat-Session-Id header es requerido'}), 400

        adjunto = create_attachment_with_thumbnail(
            file_storage=file,
            user_id=user_id,
            session_id=session_id # Opcional, para asociar a una sesión de chat
        )

        if not adjunto:
            return _json({"error": "Error al procesar y guardar el archivo."}, 500)

        # Commit here to ensure adjunto.id is populated
        db.session.commit()

        # Cargar metadatos del análisis si existen
        analisis = AnalisisArchivo.query.filter_by(
            archivo_adjunto_id=adjunto.id, tipo_analisis='thumbnail_meta'
        ).first()
        meta_data = analisis.datos_estructurados if analisis else {}
        if not isinstance(meta_data, dict):
            meta_data = {}

        thumb_url, meta_data = resolve_attachment_thumb_url(
            file_url=adjunto.url,
            filename=adjunto.filename,
            mime_type=adjunto.mime,
            meta=meta_data,
        )

        # Build the final response object AFTER the commit using the same
        # delivery contract used by ticket timelines and CRM inbox views.
        attachment_info_payload = serialize_attachment_for_delivery(
            adjunto,
            meta=meta_data,
            thumb_url=thumb_url,
        )

        return _json({
            "ok": True,
            "attachmentInfo": attachment_info_payload,
        }, 200)

    except Exception as e:
        current_app.logger.error("Error critico en upload_chat_attachment: %s", e, exc_info=True)
        return _json({"error": "Error interno del servidor."}, 500)
