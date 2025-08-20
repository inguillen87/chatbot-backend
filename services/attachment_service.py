from flask import current_app
from models import db, ArchivoAdjunto, AnalisisArchivo
from services.gcs_service import guardar_adjunto_y_thumbnail
from werkzeug.datastructures import FileStorage

def create_attachment_with_thumbnail(file_storage: FileStorage, user_id: int = None, session_id: str = None) -> ArchivoAdjunto | None:
    """
    Orchestrates the full attachment creation process.
    1. Uploads file and thumbnail to GCS.
    2. Creates an ArchivoAdjunto record for the original file.
    3. Creates an AnalisisArchivo record to store thumbnail metadata.

    Args:
        file_storage: The FileStorage object from the request.
        user_id: The ID of the user uploading the file.
        session_id: The session ID for anonymous users.

    Returns:
        The created ArchivoAdjunto object, or None on failure.
    """
    if not file_storage:
        return None

    # 1. Upload to GCS
    upload_result = guardar_adjunto_y_thumbnail(file_storage)
    if not upload_result or not isinstance(upload_result, tuple) or len(upload_result) < 1:
        current_app.logger.error(f"Failed to upload attachment to GCS or invalid return format. Result: {upload_result}")
        return None

    try:
        # Unpack the tuple: (original_file_data, thumbnail_file_data)
        original_file_data = upload_result[0]
        thumb_data = upload_result[1] if len(upload_result) > 1 else None

        # 2. Create ArchivoAdjunto DB record
        nuevo_adjunto = ArchivoAdjunto(
            user_id=user_id,
            session_id=session_id,
            filename=original_file_data['unique_name'],
            nombre_original=original_file_data['original_name'],
            mime=original_file_data['mimetype'],
            tamano=original_file_data['size'],
            tipo='chat_adjunto',
            url=original_file_data['public_url']  # Correct key is public_url
        )
        db.session.add(nuevo_adjunto)
        db.session.flush()

        # 3. Create AnalisisArchivo to store thumbnail meta
        if thumb_data and thumb_data.get('thumb_meta'):
            analisis = AnalisisArchivo(
                archivo_adjunto_id=nuevo_adjunto.id,
                estado_analisis='completado',
                tipo_analisis='thumbnail_meta',
                datos_estructurados=thumb_data['thumb_meta']
            )
            db.session.add(analisis)

        current_app.logger.info(f"ArchivoAdjunto (ID: {nuevo_adjunto.id}) and AnalisisArchivo prepared for commit.")

        return nuevo_adjunto

    except (TypeError, KeyError) as e:
        current_app.logger.error(f"Error processing upload_result tuple/dict: {e}. Result was: {upload_result}", exc_info=True)
        return None
    except Exception as e:
        current_app.logger.error(f"Error preparing attachment records for DB: {e}", exc_info=True)
        return None
