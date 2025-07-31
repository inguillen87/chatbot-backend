import logging
from datetime import datetime
import os # For path joining
import requests # For downloading files if URLs are external

from celery_utils import celery_app # Importar desde el nuevo módulo
from extensions import db
from models import ArchivoAdjunto, AnalisisArchivo, User # User might not be directly needed here
from services.llm_utils import analyze_image_with_google_vision_ocr, analyze_document_with_google_document_ai, robust_chat 
from config import Config 

logger = logging.getLogger(__name__)

# Helper function to get or create AnalisisArchivo
def _get_or_create_analisis_archivo(session, archivo_adjunto_id: int) -> AnalisisArchivo:
    analisis = session.query(AnalisisArchivo).filter_by(archivo_adjunto_id=archivo_adjunto_id).first()
    if not analisis:
        analisis = AnalisisArchivo(archivo_adjunto_id=archivo_adjunto_id, estado_analisis="pendiente")
        session.add(analisis)
        # Commit will be handled by the task context or calling function
    return analisis

# En services/analisis_archivo_service.py
# ... otras importaciones ...
from services.interpretacion_imagen_service import interpretar_imagen_para_chat
from services.logic import es_rubro_publico # Para determinar contexto municipal
from services.generic_file_processor import procesar_archivo_generico
# ...

# ... _get_or_create_analisis_archivo ...

@celery_app.task(name='tasks.analizar_contenido_archivo', bind=True, max_retries=3, default_retry_delay=60)
def tarea_analizar_contenido_archivo(self, archivo_adjunto_id: int):
    logger.info(f"Iniciando tarea de análisis para ArchivoAdjunto ID: {archivo_adjunto_id} (Intento: {self.request.retries + 1})")
    
    session = db.session # Obtain session from Flask-SQLAlchemy
    archivo_adjunto = session.query(ArchivoAdjunto).get(archivo_adjunto_id)
    user_obj = session.query(User).get(archivo_adjunto.user_id) if archivo_adjunto else None


    if not archivo_adjunto:
        logger.error(f"No se encontró ArchivoAdjunto con ID: {archivo_adjunto_id}. No se reintentará.")
        return

    if not user_obj: # Necesitamos el usuario para determinar el contexto
        logger.error(f"No se encontró User con ID: {archivo_adjunto.user_id} para ArchivoAdjunto ID: {archivo_adjunto_id}. No se puede determinar contexto.")
        # Marcar análisis como error o pendiente de contexto
        analisis_temp = _get_or_create_analisis_archivo(session, archivo_adjunto_id)
        analisis_temp.estado_analisis = "error"
        analisis_temp.error_analisis = "Usuario no encontrado, no se pudo determinar contexto para análisis de imagen."
        analisis_temp.fecha_analisis = datetime.utcnow()
        session.commit()
        session.remove()
        return

    try:
        analisis_archivo = _get_or_create_analisis_archivo(session, archivo_adjunto_id)
        if analisis_archivo.estado_analisis == "completado" and analisis_archivo.tipo_analisis == 'reclamo_vision_llm_v1':
            logger.info(f"Análisis de reclamo para ArchivoAdjunto ID: {archivo_adjunto_id} ya está completado. Saltando.")
            session.remove()
            return

        analisis_archivo.estado_analisis = "procesando"
        session.commit() # Commit temprano del estado "procesando"

        mime_type = archivo_adjunto.mime.lower() if archivo_adjunto.mime else ''
        # También obtener la extensión del archivo para tipos como xlsx, csv
        _, file_extension = os.path.splitext(archivo_adjunto.filename.lower())

        # Determinar si el contexto es PYME
        # Asumimos que user_obj.tipo_chat == "pyme" o que no es un rubro público.
        es_contexto_pyme = user_obj.tipo_chat == "pyme" if user_obj.tipo_chat else \
                           (user_obj.rubro and not es_rubro_publico(user_obj.rubro))
        
        # Determinar si el contexto es Municipal (para la lógica de reclamos por imagen)
        es_contexto_municipal = user_obj.tipo_chat == "municipio" if user_obj.tipo_chat else \
                                (user_obj.rubro and es_rubro_publico(user_obj.rubro))


        # --- LÓGICA DE PROCESAMIENTO DE ARCHIVOS ---

        # 1. Procesamiento de Pedidos Excel para PYMEs
        if es_contexto_pyme and file_extension in ['.xlsx', '.xls', '.csv']:
            logger.info(f"Archivo {archivo_adjunto_id} ({archivo_adjunto.filename}) es un Excel/CSV en contexto PYME. Intentando procesar como pedido.")
            from services.pedido_processor_service import procesar_pedido_excel # Importar aquí para evitar circularidad

            ruta_fisica_archivo = obtener_ruta_fisica_archivo(archivo_adjunto)
            if ruta_fisica_archivo:
                resultado_pedido_excel = procesar_pedido_excel(ruta_fisica_archivo, user_obj.id)

                if "error" in resultado_pedido_excel:
                    analisis_archivo.estado_analisis = "error"
                    analisis_archivo.error_analisis = resultado_pedido_excel["error"]
                    logger.error(f"Error procesando Excel de pedido {archivo_adjunto_id}: {resultado_pedido_excel['error']}")
                else:
                    analisis_archivo.estado_analisis = "completado"
                    analisis_archivo.datos_estructurados = resultado_pedido_excel # Guardar todo el resultado
                    logger.info(f"Procesamiento de Excel de pedido {archivo_adjunto_id} completado. Items: {len(resultado_pedido_excel.get('items_procesados',[]))}")
                analisis_archivo.tipo_analisis = "pedido_excel_v1"
            else:
                analisis_archivo.estado_analisis = "error"
                analisis_archivo.error_analisis = "No se pudo obtener la ruta física del archivo para procesar el pedido Excel."
                logger.error(f"No se pudo obtener ruta física para Excel de pedido {archivo_adjunto_id}.")
                analisis_archivo.tipo_analisis = "pedido_excel_error_ruta"

        # 2. Procesamiento de Imágenes para Reclamos Municipales
        elif mime_type.startswith("image/") and es_contexto_municipal:
            logger.info(f"Archivo {archivo_adjunto_id} es una imagen en contexto municipal. Iniciando análisis de reclamo.")
            # Llamar a la función correcta con los parámetros adecuados
            interpretar_imagen_para_chat(archivo_adjunto=archivo_adjunto, tipo_interpretacion="reclamo_municipal", pyme_user=None)
            # La función interpretar_imagen_para_chat maneja el estado de analisis_archivo internamente.
            # No necesitamos cambiar estado_analisis aquí.

        # 3. OCR Simple para Imágenes en otros contextos (ej. PYME pero no es pedido Excel)
        elif mime_type.startswith("image/"): # Si no es Excel de pedido y es PYME, o cualquier imagen no municipal
            logger.info(f"Archivo {archivo_adjunto_id} es una imagen ({mime_type}) en contexto no municipal o no pedido Excel. Realizando OCR simple.")
            # Esta función también maneja el estado de analisis_archivo internamente.
            _realizar_analisis_ocr_simple(session, analisis_archivo.id)

        # 4. Análisis de PDF con Document AI (genérico)
        elif mime_type == "application/pdf":
            project_id = current_app.config.get('GOOGLE_PROJECT_ID')
            docai_location = current_app.config.get('GOOGLE_DOCAI_LOCATION')
            docai_processor_id = current_app.config.get('GOOGLE_DOCAI_PROCESSOR_ID') # General purpose

            if project_id and docai_location and docai_processor_id:
                logger.info(f"Archivo {archivo_adjunto_id} es un PDF. Intentando análisis con Document AI (genérico).")
                # Esta función también maneja el estado de analisis_archivo internamente.
                analizar_pdf_con_document_ai_service(session, analisis_archivo.id, project_id, docai_location, docai_processor_id)

                # TODO: Implementar la transformación del resultado de Document AI.
                # El objeto 'analisis_archivo.texto_extraido' y 'analisis_archivo.datos_estructurados'
                # (si fueron poblados por analizar_pdf_con_document_ai_service a través de la llamada a llm_utils)
                # necesitarán ser procesados aquí para convertirlos en:
                # - Una lista de items de pedido para PYMEs (similar a lo que hace _procesar_interpretacion_pedido_pyme con OCR).
                # - Un resumen o palabras clave para reclamos municipales.
                # Este resultado transformado debería luego ser almacenado en analisis_archivo.datos_estructurados
                # de una forma que los handlers (PedidoHandler, ReclamoHandler) puedan consumir.
                # Por ejemplo, para pedidos:
                # if es_contexto_pyme and analisis_archivo.estado_analisis == "completado":
                #     items_pedido_de_doc = transformar_doc_ai_output_a_lista_pedido(analisis_archivo.datos_estructurados)
                #     # Actualizar analisis_archivo.datos_estructurados con esta lista estandarizada.
                #     # session.commit() se hará al final de la tarea Celery.
            else:
                logger.warning(f"Configuración de Document AI (genérico) incompleta. Saltando PDF para archivo {archivo_adjunto_id}.")
                analisis_archivo.estado_analisis = "omitido_config"
                analisis_archivo.tipo_analisis = "pdf_docai_config_faltante"
        
        # 5. Speech-to-Text for Audio Files
        elif mime_type.startswith("audio/"):
            logger.info(f"Archivo {archivo_adjunto_id} es un archivo de audio. Iniciando transcripción.")
            from services.google_speech_to_text import SpeechToTextService
            stt_service = SpeechToTextService()
            transcription = stt_service.transcribe_audio_url(archivo_adjunto.url, mime_type)
            if transcription:
                analisis_archivo.texto_extraido = transcription
                analisis_archivo.tipo_analisis = "speech_to_text"
                analisis_archivo.estado_analisis = "completado"
                logger.info(f"Transcripción de audio completada para Archivo ID: {archivo_adjunto.id}. Texto: {transcription[:100]}...")
                # Ahora que tenemos el texto, podemos tratarlo como un mensaje de texto normal.
                # Esto se manejará en el flujo del chat, no aquí.
            else:
                analisis_archivo.estado_analisis = "error"
                analisis_archivo.error_analisis = "No se pudo transcribir el audio."
        # 6. Archivos de Texto Plano y otros tipos genéricos
        else:
            logger.info(f"Intentando procesamiento genérico para archivo {archivo_adjunto_id} con MIME type: {mime_type}")
            ruta_fisica_archivo = obtener_ruta_fisica_archivo(archivo_adjunto)
            if ruta_fisica_archivo:
                resultado_generico = procesar_archivo_generico(ruta_fisica_archivo, mime_type)
                if resultado_generico:
                    analisis_archivo.texto_extraido = resultado_generico.get("texto_extraido")
                    analisis_archivo.datos_estructurados = {"analisis_gemini": resultado_generico.get("analisis_gemini")}
                    analisis_archivo.estado_analisis = "completado"
                    analisis_archivo.tipo_analisis = "generico_gemini_v1"
                    logger.info(f"Procesamiento genérico de {archivo_adjunto_id} completado.")
                else:
                    logger.warning(f"Procesamiento genérico no arrojó resultados para {archivo_adjunto_id}.")
                    analisis_archivo.estado_analisis = "no_aplicable"
                    analisis_archivo.tipo_analisis = "generico_no_soportado"
            else:
                analisis_archivo.estado_analisis = "error"
                analisis_archivo.error_analisis = "No se pudo obtener la ruta física del archivo para procesamiento genérico."
                logger.error(f"No se pudo obtener ruta física para procesamiento genérico de {archivo_adjunto_id}.")

        if analisis_archivo.estado_analisis not in ["error", "procesando", "omitido_config", "completado"]:
             analisis_archivo.estado_analisis = "completado"

        if analisis_archivo.tipo_analisis != 'reclamo_vision_llm_v1' and analisis_archivo.estado_analisis == "completado":
            analisis_archivo.fecha_analisis = datetime.utcnow()
            session.commit()
        elif analisis_archivo.estado_analisis == "error" or analisis_archivo.estado_analisis == "omitido_config" or analisis_archivo.estado_analisis == "no_aplicable":
            analisis_archivo.fecha_analisis = datetime.utcnow()
            session.commit()

        logger.info(f"Análisis (o delegación) finalizado para ArchivoAdjunto ID: {archivo_adjunto_id}. Estado final en DB: {analisis_archivo.estado_analisis}")

    except requests.exceptions.RequestException as exc: 
        logger.error(f"Error de red en tarea de análisis para ArchivoAdjunto ID: {archivo_adjunto_id}: {exc}", exc_info=True)
        session.rollback()
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error(f"Máximos reintentos alcanzados para ArchivoAdjunto ID: {archivo_adjunto_id} por error de red.")
            analisis_archivo = session.query(AnalisisArchivo).filter_by(archivo_adjunto_id=archivo_adjunto_id).first()
            if analisis_archivo:
                analisis_archivo.estado_analisis = "error"
                analisis_archivo.error_analisis = f"Error de red persistente: {str(exc)}"
                analisis_archivo.fecha_analisis = datetime.utcnow()
                session.commit()
    except Exception as e:
        logger.error(f"Error crítico en la tarea de análisis para ArchivoAdjunto ID: {archivo_adjunto_id}: {e}", exc_info=True)
        session.rollback()
        analisis_archivo = session.query(AnalisisArchivo).filter_by(archivo_adjunto_id=archivo_adjunto_id).first()
        if analisis_archivo:
            analisis_archivo.estado_analisis = "error"
            analisis_archivo.error_analisis = str(e)
            analisis_archivo.fecha_analisis = datetime.utcnow()
            session.commit()
    finally:
        session.remove()

    # --- After all processing, if successful and relevant, update ChatSessionContext ---
    if archivo_adjunto and analisis_archivo and analisis_archivo.estado_analisis == "completado" and \
       analisis_archivo.tipo_analisis in ["reclamo_auto_descripcion_categoria", "reclamo_vision_llm_v1"]: # Add other relevant types if necessary

        logger.info(f"Análisis completado y relevante para ArchivoAdjunto ID: {archivo_adjunto_id}. Intentando actualizar ChatSessionContext.")
        from models import ChatSessionContext # Import here to avoid potential top-level circularity

        chat_session_id_to_update = archivo_adjunto.session_id
        if chat_session_id_to_update:
            # Re-acquire session for ChatSessionContext modification if needed, or use existing if task is configured with app context
            # Assuming db.session is still valid here or re-fetched if necessary for tasks.
            # For simplicity, let's try to use the existing session from the task context first.
            # If Celery tasks run outside Flask app context by default, this needs careful handling.
            # However, the task starts with `session = db.session`, implying it has one.

            try:
                # Ensure we are using a session that can commit changes to ChatSessionContext
                # This might require a new session if the previous one was closed or is specific to the task's isolated operations.
                # For now, let's assume the `session` object from the start of the task is still usable.
                # If not, one might need: session = db.create_scoped_session() or similar.

                chat_context_record = session.query(ChatSessionContext).filter_by(chat_session_id=chat_session_id_to_update).first()
                if chat_context_record:
                    if chat_context_record.context_data is None:
                        chat_context_record.context_data = {}

                    # Store info about the completed analysis
                    chat_context_record.context_data["web_analisis_listo"] = {
                        "archivo_id": archivo_adjunto.id,
                        "timestamp": datetime.utcnow().isoformat(),
                        "tipo_analisis": analisis_archivo.tipo_analisis
                    }
                    # db.session.add(chat_context_record) # Not needed if already fetched and modified
                    session.commit()
                    logger.info(f"ChatSessionContext {chat_session_id_to_update} actualizado con web_analisis_listo para archivo ID {archivo_adjunto.id}.")
                else:
                    logger.warning(f"No se encontró ChatSessionContext con ID {chat_session_id_to_update} para actualizar tras análisis de archivo {archivo_adjunto.id}.")
            except Exception as e_csc_update:
                logger.error(f"Error actualizando ChatSessionContext para archivo {archivo_adjunto.id} tras análisis: {e_csc_update}", exc_info=True)
                session.rollback() # Rollback ChatSessionContext update only
        else:
            logger.warning(f"ArchivoAdjunto ID {archivo_adjunto.id} no tiene session_id. No se puede actualizar ChatSessionContext.")


# Nueva función para OCR simple, separada de la lógica de reclamos
def _realizar_analisis_ocr_simple(session, analisis_archivo_id: int):
    analisis_archivo = session.query(AnalisisArchivo).get(analisis_archivo_id)
    if not analisis_archivo or not analisis_archivo.archivo_adjunto:
        logger.error(f"No se encontró AnalisisArchivo o ArchivoAdjunto asociado para OCR simple. ID: {analisis_archivo_id}")
        return

    archivo_adjunto = analisis_archivo.archivo_adjunto
    logger.info(f"Iniciando OCR simple con Vision para ArchivoAdjunto ID: {archivo_adjunto.id}, Análisis ID: {analisis_archivo.id}")

    try:
        image_content = obtener_contenido_archivo(archivo_adjunto)
        if not image_content:
            raise ValueError("No se pudo obtener el contenido de la imagen para OCR simple.")

        texto_extraido_ocr = analyze_image_with_google_vision_ocr(image_content) 

        if texto_extraido_ocr is not None: # analyze_image_with_google_vision_ocr returns "" on no text, or actual text
            analisis_archivo.texto_extraido = texto_extraido_ocr if texto_extraido_ocr else None
            analisis_archivo.tipo_analisis = "vision_ocr"
            analisis_archivo.error_analisis = None 
            logger.info(f"OCR completado para Archivo ID: {archivo_adjunto.id}. Texto extraído (primeros 100 chars): {texto_extraido_ocr[:100] if texto_extraido_ocr else 'N/A'}")
        else: 
            analisis_archivo.texto_extraido = None
            analisis_archivo.tipo_analisis = "vision_ocr_sin_resultado"
            analisis_archivo.error_analisis = "Vision API no devolvió texto (podría ser error o imagen sin texto)."
            logger.info(f"OCR ejecutado para Archivo ID: {archivo_adjunto.id}, pero no se obtuvo resultado de texto.")
        
        analisis_archivo.estado_analisis = "completado"
    except Exception as e:
        logger.error(f"Error durante OCR simple de imagen ID {archivo_adjunto.id}: {e}", exc_info=True)
        analisis_archivo.estado_analisis = "error"
        analisis_archivo.error_analisis = f"Error en OCR Vision: {str(e)}"
        analisis_archivo.tipo_analisis = "vision_ocr_error"
    
    analisis_archivo.fecha_analisis = datetime.utcnow()
    # session.commit() is handled by the main Celery task

def analizar_pdf_con_document_ai_service(session, analisis_archivo_id: int, project_id: str, location: str, processor_id: str):
    analisis_archivo = session.query(AnalisisArchivo).get(analisis_archivo_id)
    if not analisis_archivo or not analisis_archivo.archivo_adjunto:
        logger.error(f"No se encontró AnalisisArchivo o ArchivoAdjunto para Document AI. ID: {analisis_archivo_id}")
        return

    archivo_adjunto = analisis_archivo.archivo_adjunto
    logger.info(f"Iniciando análisis con Document AI para ArchivoAdjunto ID: {archivo_adjunto.id}, Análisis ID: {analisis_archivo.id}")

    try:
        pdf_content = obtener_contenido_archivo(archivo_adjunto)
        if not pdf_content:
            raise ValueError("No se pudo obtener el contenido del PDF.")

        # Llamada a la función de llm_utils.py
        document_ai_result = analyze_document_with_google_document_ai(
            project_id=project_id,
            location=location,
            processor_id=processor_id,
            file_content=pdf_content,
            mime_type=archivo_adjunto.mime # Should be 'application/pdf'
        )

        if document_ai_result and document_ai_result.text:
            analisis_archivo.texto_extraido = document_ai_result.text
            # Aquí se podrían procesar document_ai_result.entities, tables, etc. para datos_estructurados
            # Ejemplo simple:
            # datos_estructurados = {}
            # for entity in document_ai_result.entities:
            #    datos_estructurados[entity.type_] = entity.mention_text
            # analisis_archivo.datos_estructurados = datos_estructurados
            analisis_archivo.tipo_analisis = f"document_ai_{processor_id}" # O más específico
            analisis_archivo.estado_analisis = "completado"
            analisis_archivo.error_analisis = None
            logger.info(f"Análisis Document AI completado para Archivo ID: {archivo_adjunto.id}. Texto extraído (primeros 100 chars): {document_ai_result.text[:100]}")
        elif document_ai_result: # Result exists but no text
             analisis_archivo.texto_extraido = None
             analisis_archivo.tipo_analisis = f"document_ai_{processor_id}_sin_texto"
             analisis_archivo.estado_analisis = "completado" # Still completed
             logger.info(f"Análisis Document AI ejecutado para Archivo ID: {archivo_adjunto.id}, pero no se encontró texto.")
        else: # No result from Document AI (likely an error in llm_utils placeholder or API call)
            raise ValueError("analyze_document_with_google_document_ai no devolvió resultado.")

    except Exception as e:
        logger.error(f"Error durante el análisis Document AI del PDF ID {archivo_adjunto.id}: {e}", exc_info=True)
        analisis_archivo.estado_analisis = "error"
        analisis_archivo.error_analisis = f"Error en Document AI: {str(e)}"
        analisis_archivo.tipo_analisis = "document_ai_error"
    
    analisis_archivo.fecha_analisis = datetime.utcnow()
    # session.commit() is handled by the main Celery task


def obtener_contenido_archivo(archivo_adjunto: ArchivoAdjunto) -> bytes | None:
    file_path_to_try = None
    # Asegurarse que Config.UPLOAD_FOLDER está disponible. Si no, loguear y retornar.
    upload_folder = getattr(Config, 'UPLOAD_FOLDER', None)
    if not upload_folder:
        logger.error("Config.UPLOAD_FOLDER no está configurado. No se puede acceder a archivos locales.")
        # Podríamos intentar solo la URL si existe, pero es mejor que la config esté bien.
        # For now, let's allow falling through to URL if UPLOAD_FOLDER is missing,
        # but log it prominently.
    
    if upload_folder and archivo_adjunto.filename:
        potential_path = os.path.join(upload_folder, archivo_adjunto.filename)
        if os.path.exists(potential_path) and os.path.isfile(potential_path):
            file_path_to_try = potential_path
        else:
            logger.warning(f"Archivo {archivo_adjunto.filename} no encontrado en la ruta esperada: {potential_path} (UPLOAD_FOLDER: {upload_folder})")

    if file_path_to_try:
        logger.info(f"Leyendo archivo localmente desde: {file_path_to_try}")
        try:
            with open(file_path_to_try, "rb") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error leyendo archivo local {file_path_to_try}: {e}", exc_info=True)
            # Fall through to URL download

    if archivo_adjunto.url and archivo_adjunto.url.lower().startswith(('http://', 'https://')):
        logger.info(f"Intentando descargar archivo desde URL: {archivo_adjunto.url}")
        try:
            with requests.Session() as s:
                response = s.get(archivo_adjunto.url, timeout=30, stream=True) 
                response.raise_for_status()
                return response.content 
        except requests.exceptions.RequestException as e:
            logger.error(f"Error descargando archivo desde URL {archivo_adjunto.url}: {e}", exc_info=True)
            raise 
    
    logger.warning(f"No se pudo obtener contenido para ArchivoAdjunto ID: {archivo_adjunto.id}. Path local no encontrado o URL no válida/accesible.")
    return None

# Necesario para que `current_app` funcione en la tarea Celery si no se usa Flask-Celery-Helper o similar
from flask import current_app

logger.info("Servicio de análisis de archivos (re)cargado con lógica OCR y Document AI (placeholder).")
