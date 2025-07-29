from flask import Blueprint, request, jsonify, current_app, send_from_directory, make_response
from extensions import db
from models import ArchivoAdjunto, User
import os
import uuid
from werkzeug.utils import secure_filename
from datetime import datetime
from routes.auth import token_requerido
from services.analisis_archivo_service import tarea_analizar_contenido_archivo # Nueva importación
from google.cloud import storage
from services.google_vision_service import analyze_image_from_content
from services.google_docai import procesar_catalogo_pdf_google, procesar_catalogo_imagen_google

archivos_bp = Blueprint('archivos_bp', __name__, url_prefix='/archivos')

BUCKET_NAME = "chatboc-files"
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
        'Authorization, Content-Type, Origin, Accept, Anon-Id, x-entity-token, x-chat-session-id'
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
            from models import MunicipioTicket
            ticket = MunicipioTicket.query.filter_by(id=adj.municipio_ticket_id).first()
            if not ticket or ticket.municipio_id != user.municipio_id:
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
    # Cambiado de 'archivo' a 'archivos' y usando getlist
    files = request.files.getlist("archivos")

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

    # Permisos: empleados solo pueden asociar archivos a tickets de su empresa/municipio
    if current_user.rol == 'empleado':
        from models import PymeTicket, MunicipioTicket
        if pyme_ticket_id:
            ticket = PymeTicket.query.filter_by(id=pyme_ticket_id).first()
            if not ticket or ticket.empresa_id != current_user.empresa_id:
                return jsonify({'error': 'No puede asociar archivos a tickets de otra empresa.'}), 403
        if municipio_ticket_id:
            from models import MunicipioTicket
            ticket = MunicipioTicket.query.filter_by(id=municipio_ticket_id).first()
            if not ticket or ticket.municipio_id != current_user.municipio_id:
                return jsonify({'error': 'No puede asociar archivos a tickets de otro municipio.'}), 403

    resultados_subida = []
    archivos_guardados_info = [] # Para rollback en caso de error parcial

    for file in files:
        if file.filename == '':
            continue

        original = secure_filename(file.filename)
        unique = f"{uuid.uuid4().hex}_{original}"

        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(BUCKET_NAME)
            blob = bucket.blob(unique)

            blob.upload_from_file(file, content_type=file.mimetype)

            tamano = blob.size

            if tamano > MAX_FILE_SIZE:
                blob.delete()
                for agi in archivos_guardados_info:
                    try:
                        storage_client.bucket(BUCKET_NAME).blob(agi['unique']).delete()
                    except Exception as e_delete:
                        current_app.logger.error(f"Error al eliminar archivo {agi['unique']} de GCS durante el rollback: {e_delete}", exc_info=True)
                return jsonify({'error': f'Archivo "{original}" demasiado grande (máx 10MB).'}), 413

            archivos_guardados_info.append({'path': blob.public_url, 'unique': unique, 'original': original, 'mimetype': file.mimetype, 'tamano': tamano})

        except Exception as e:
            current_app.logger.error(f"Error al subir el archivo {original} a GCS: {e}", exc_info=True)
            return jsonify({'error': f'Error al subir el archivo {original}.'}), 500

    # Si todos los archivos se guardaron bien, ahora los registramos en la BD
    for agi in archivos_guardados_info:
        url = agi['path']
        nuevo_adjunto = ArchivoAdjunto(
            user_id=current_user.id,
            session_id=session_id,
            filename=agi['unique'],
            nombre_original=agi['original'],
            mime=agi['mimetype'],
            tamano=agi['tamano'],
            tipo=tipo_adjunto,
            pyme_ticket_id=pyme_ticket_id if pyme_ticket_id else None,
            municipio_ticket_id=municipio_ticket_id if municipio_ticket_id else None,
            url=url,
        )
        db.session.add(nuevo_adjunto)

        try:
            db.session.commit() # Commit por cada archivo para obtener ID para la tarea

            # Encolar tarea de análisis de archivo
            try:
                tarea_analizar_contenido_archivo.delay(nuevo_adjunto.id)
                current_app.logger.info(f"Tarea de análisis encolada para ArchivoAdjunto ID: {nuevo_adjunto.id}")
            except Exception as e_celery:
                current_app.logger.error(f"Error al encolar tarea de análisis para ArchivoAdjunto ID: {nuevo_adjunto.id}. Error: {e_celery}", exc_info=True)
                # No revertimos la subida, solo logueamos el error de encolado

            current_app.logger.info(
                f"Archivo subido por user {current_user.id}: {agi['unique']} ({agi['original']}). ID: {nuevo_adjunto.id}"
            )

            # Procesar el archivo con Document AI si es un PDF o una imagen
            extracted_data = None
            if agi['mimetype'] == 'application/pdf':
                extracted_data = procesar_catalogo_pdf_google(agi['path'], current_user.id)
            elif agi['mimetype'].startswith('image/'):
                extracted_data = procesar_catalogo_imagen_google(agi['path'], current_user.id)

            resultados_subida.append({
                'filename': agi['unique'],
                'id': nuevo_adjunto.id,
                'name': agi['original'],
                'mimeType': agi['mimetype'],
                'size': agi['tamano'],
                'url': url,
                'extracted_data': extracted_data
            })
        except Exception as e_db:
            db.session.rollback()
            current_app.logger.error(f"Error al registrar en BD el archivo {agi['original']}: {e_db}", exc_info=True)
            # Eliminar el archivo físico que se guardó pero no se pudo registrar en BD
            try:
                storage_client.bucket(BUCKET_NAME).blob(agi['unique']).delete()
            except Exception as e_delete:
                current_app.logger.error(f"Error al eliminar archivo {agi['unique']} de GCS durante el rollback: {e_delete}", exc_info=True)

    if not resultados_subida and archivos_guardados_info:
        for agi in archivos_guardados_info:
            try:
                storage_client.bucket(BUCKET_NAME).blob(agi['unique']).delete()
            except Exception as e_delete:
                current_app.logger.error(f"Error al eliminar archivo {agi['unique']} de GCS durante el rollback: {e_delete}", exc_info=True)
        return jsonify({'error': 'Error al procesar archivos en la base de datos después de guardarlos.'}), 500

    if not resultados_subida and not files: # Si no se enviaron archivos válidos desde el principio
         return jsonify({'error': 'No se proporcionaron archivos válidos.'}), 400

    if resultados_subida:
        return jsonify({
            'mensaje': f'{len(resultados_subida)} archivo(s) subido(s) y análisis encolado.',
            'archivos': resultados_subida
        }), 200
    else:
        # Si llegó aquí, es probable que los archivos fallaran las validaciones iniciales o hubo otro problema.
        # Los errores específicos ya deberían haber sido retornados.
        # Este es un fallback, aunque la lógica anterior debería cubrir los casos de error.
        return jsonify({'error': 'No se pudieron procesar los archivos.'}), 400


@archivos_bp.route('/subir_imagen', methods=['POST'])
@token_requerido
def subir_imagen(current_user):
    if 'archivo' not in request.files:
        return jsonify({'error': 'No se encontró el archivo'}), 400

    file = request.files['archivo']

    if file.filename == '':
        return jsonify({'error': 'No se seleccionó ningún archivo'}), 400

    if file and allowed_file(file.filename) and allowed_mime(file.mimetype):
        original = secure_filename(file.filename)
        unique = f"{uuid.uuid4().hex}_{original}"
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        save_path = os.path.join(UPLOAD_FOLDER, unique)

        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(BUCKET_NAME)
            blob = bucket.blob(unique)

            blob.upload_from_file(file, content_type=file.mimetype)

            tamano = blob.size

            if tamano > MAX_FILE_SIZE:
                blob.delete()
                return jsonify({'error': 'Archivo demasiado grande (máx 10MB).'}), 413

            url = blob.public_url
            nuevo_adjunto = ArchivoAdjunto(
                user_id=current_user.id,
                filename=unique,
                nombre_original=original,
                mime=file.mimetype,
                tamano=tamano,
                tipo='imagen',
                url=url,
            )
            db.session.add(nuevo_adjunto)
            db.session.commit()

            analysis_result = analyze_image_from_content(file.read())
            file.seek(0)

            return jsonify({
                'mensaje': 'Imagen subida y analizada correctamente.',
                'archivo': {
                    'filename': unique,
                    'id': nuevo_adjunto.id,
                    'name': original,
                    'mimeType': file.mimetype,
                    'size': tamano,
                    'url': url
                },
                'analisis': analysis_result
            }), 200

        except Exception as e:
            current_app.logger.error(f"Error al subir la imagen {original} a GCS: {e}", exc_info=True)
            return jsonify({'error': 'Error al subir la imagen.'}), 500

    return jsonify({'error': 'Formato de archivo no permitido'}), 400


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
            from models import MunicipioTicket
            ticket = MunicipioTicket.query.filter_by(id=adj.municipio_ticket_id).first()
            if not ticket or ticket.municipio_id != current_user.municipio_id:
                return jsonify({'error': 'Acceso denegado'}), 403
        # Si es archivo propio
        elif adj.user_id != current_user.id:
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
        'Authorization, Content-Type, Origin, Accept, Anon-Id, x-entity-token, x-chat-session-id'
    )
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response
