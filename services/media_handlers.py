from flask import current_app
from werkzeug.datastructures import FileStorage
from services.attachment_service import create_attachment_with_thumbnail
from services.audio_transcription_service import transcribe_audio_from_url
from services.media_classifier import clasificar_adjunto_whatsapp
from services.document_processing_service import process_document_with_gcs
import os

# Twilio credentials are needed for audio transcription of files hosted by Twilio
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

def _save_attachment(file_storage: FileStorage, user_id: int, session_id: str) -> dict | None:
    """
    Generic helper to save an attachment and return its details.
    This is a common step for all media types.
    """
    adjunto = create_attachment_with_thumbnail(
        file_storage=file_storage,
        user_id=user_id,
        session_id=session_id
    )
    if adjunto:
        return {
            "id": adjunto.id,
            "url": adjunto.url,
            "mime_type": adjunto.mime,
            "name": adjunto.nombre_original,
            "source": "whatsapp"
        }
    current_app.logger.error("create_attachment_with_thumbnail failed to process the media")
    return None

def handle_audio(file_storage: FileStorage, uploaded_file_info: dict, context: dict) -> tuple[str, dict, None]:
    """
    Handles audio files by transcribing them.
    Returns the transcribed text, file info, and no interpreted data.
    """
    media_url = context.get("media_url")
    message_body = ""
    try:
        transcribed_text = transcribe_audio_from_url(media_url, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        if transcribed_text:
            message_body = transcribed_text
            uploaded_file_info['transcribed_text'] = transcribed_text
        else:
            current_app.logger.warning("Audio transcription failed or returned empty.")
            message_body = "[Audio recibido, no se pudo transcribir]"
    except Exception as e:
        current_app.logger.error(f"Error during audio transcription: {e}", exc_info=True)
        message_body = "[Error al procesar audio]"

    return message_body, uploaded_file_info, None

def handle_image(file_storage: FileStorage, uploaded_file_info: dict, context: dict) -> tuple[str, dict, dict | None]:
    """
    Handles image files by classifying them.
    Returns the message body, file info, and interpreted data.
    """
    client_user = context.get("client_user")
    interpretacion_media_data = clasificar_adjunto_whatsapp(uploaded_file_info, client_user)
    return "", uploaded_file_info, interpretacion_media_data

def handle_video(file_storage: FileStorage, uploaded_file_info: dict, context: dict) -> tuple[str, dict, None]:
    """
    Handles video files. Currently, it just acknowledges receipt.
    """
    current_app.logger.info(f"Video received and saved: {uploaded_file_info.get('url')}")
    message_body = "[Video recibido, el contenido no será analizado por ahora.]"
    return message_body, uploaded_file_info, None

def handle_document(file_storage: FileStorage, uploaded_file_info: dict, context: dict) -> tuple[str, dict, None]:
    """
    Handles document files by extracting their text content.
    """
    message_body = ""
    try:
        # The document is already saved, we just need to process its content
        result = process_document_with_gcs(uploaded_file_info['id'])
        if result and result.get('text'):
            message_body = result['text']
            uploaded_file_info['extracted_text'] = message_body
            current_app.logger.info(f"Extracted text from document ID {uploaded_file_info['id']}.")
        else:
            message_body = "[Documento recibido, no se pudo extraer texto.]"
            current_app.logger.warning(f"Failed to extract text from document ID {uploaded_file_info['id']}.")
    except Exception as e:
        current_app.logger.error(f"Error processing document: {e}", exc_info=True)
        message_body = "[Error al procesar documento.]"

    return message_body, uploaded_file_info, None

def handle_generic(file_storage: FileStorage, uploaded_file_info: dict, context: dict) -> tuple[str, dict, None]:
    """
    Generic handler for any other file type.
    """
    current_app.logger.info(f"Generic file received and saved: {uploaded_file_info.get('url')}")
    message_body = f"[Archivo '{uploaded_file_info.get('name')}' recibido.]"
    return message_body, uploaded_file_info, None

# --- Media Processor Dispatcher ---

MEDIA_HANDLERS = {
    "audio/": handle_audio,
    "image/": handle_image,
    "video/": handle_video,
    "application/pdf": handle_document,
    "application/msword": handle_document,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": handle_document,
}

def process_whatsapp_media(
    file_storage: FileStorage,
    media_url: str,
    media_type: str,
    user_id: int,
    session_id: str,
    client_user
) -> tuple[str, dict | None, dict | None]:
    """
    Processes an incoming media file from WhatsApp.

    1. Saves the attachment.
    2. Dispatches to the appropriate handler based on MIME type.
    3. Returns message body, file info, and any interpreted data.
    """
    uploaded_file_info = _save_attachment(file_storage, user_id, session_id)
    if not uploaded_file_info:
        return "[Error al guardar el archivo adjunto]", None, None

    # Dispatch to the correct handler
    handler_func = handle_generic
    for prefix, handler in MEDIA_HANDLERS.items():
        if media_type.startswith(prefix):
            handler_func = handler
            break

    current_app.logger.info(f"Dispatching media type '{media_type}' to handler: {handler_func.__name__}")

    # The context dictionary contains all possible arguments for the handlers
    context = {
        "media_url": media_url,
        "client_user": client_user,
    }

    # Call the selected handler with the unified signature
    message_body, updated_info, interpreted_data = handler_func(
        file_storage, uploaded_file_info, context
    )

    return message_body, updated_info, interpreted_data
