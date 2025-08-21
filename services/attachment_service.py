from flask import current_app
from services.gcs_service import guardar_adjunto_y_thumbnail
from werkzeug.datastructures import FileStorage

def create_attachment_with_thumbnail(file_storage: FileStorage, user_id: int = None, session_id: str = None):
    from models import db, ArchivoAdjunto, AnalisisArchivo
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
    if not upload_result:
        current_app.logger.error("Failed to upload attachment to GCS.")
        return None

    try:
        # 2. Create ArchivoAdjunto DB record
        nuevo_adjunto = ArchivoAdjunto(
            user_id=user_id,
            session_id=session_id,
            filename=upload_result['unique_name'],
            nombre_original=upload_result['original_name'],
            mime=upload_result['mimetype'],
            tamano=upload_result['size'],
            tipo='chat_adjunto',  # Generic type for these attachments
            url=upload_result['original_url']
        )
        db.session.add(nuevo_adjunto)
        db.session.flush()  # Flush to get the ID for the next step

        # 3. Create AnalisisArchivo to store thumbnail meta
        thumb_meta = upload_result.get('thumb_meta')
        if thumb_meta:
            analisis = AnalisisArchivo(
                archivo_adjunto_id=nuevo_adjunto.id,
                estado_analisis='completado', # Represents that thumbnail meta is stored
                tipo_analisis='thumbnail_meta',
                datos_estructurados=thumb_meta # Store {'width': x, 'height': y, 'pages': z}
            )
            db.session.add(analisis)

        # The calling function is responsible for the commit
        current_app.logger.info(f"ArchivoAdjunto (ID: {nuevo_adjunto.id}) and AnalisisArchivo prepared for commit.")

        return nuevo_adjunto

    except Exception as e:
        # The calling function should handle the rollback
        current_app.logger.error(f"Error preparing attachment records for DB: {e}", exc_info=True)
        # Here we should ideally also delete the files from GCS to avoid orphans
        return None
