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

@celery_app.task(name='tasks.analizar_contenido_archivo', bind=True, max_retries=3, default_retry_delay=60)
def tarea_analizar_contenido_archivo(self, archivo_adjunto_id: int):
    logger.info(f"Iniciando tarea de análisis para ArchivoAdjunto ID: {archivo_adjunto_id} (Intento: {self.request.retries + 1})")
    
    session = db.session # Obtain session from Flask-SQLAlchemy
    archivo_adjunto = session.query(ArchivoAdjunto).get(archivo_adjunto_id)

    if not archivo_adjunto:
        logger.error(f"No se encontró ArchivoAdjunto con ID: {archivo_adjunto_id}. No se reintentará.")
        return

    try:
        analisis_archivo = _get_or_create_analisis_archivo(session, archivo_adjunto_id)
        analisis_archivo.estado_analisis = "procesando"
        session.commit()

        mime_type = archivo_adjunto.mime.lower() if archivo_adjunto.mime else ''
        
        # GOOGLE_PROJECT_ID, GOOGLE_DOCAI_LOCATION, GOOGLE_DOCAI_PROCESSOR_ID needed for PDF
        # These should be loaded from Config and passed to analyze_pdf_con_document_ai
        project_id = current_app.config.get('GOOGLE_PROJECT_ID')
        docai_location = current_app.config.get('GOOGLE_DOCAI_LOCATION')
        docai_processor_id = current_app.config.get('GOOGLE_DOCAI_PROCESSOR_ID') # General purpose Form Parser or OCR

        if mime_type.startswith("image/"):
            logger.info(f"Archivo {archivo_adjunto_id} es una imagen ({mime_type}). Intentando OCR con Vision API.")
            analizar_imagen_con_vision_ocr_service(session, analisis_archivo.id)

        elif mime_type == "application/pdf":
            if project_id and docai_location and docai_processor_id:
                logger.info(f"Archivo {archivo_adjunto_id} es un PDF. Intentando análisis con Document AI.")
                analizar_pdf_con_document_ai_service(session, analisis_archivo.id, project_id, docai_location, docai_processor_id)
            else:
                logger.warning(f"Configuración de Document AI incompleta (PROJECT_ID, LOCATION, PROCESSOR_ID). Saltando análisis de PDF para archivo {archivo_adjunto_id}.")
                analisis_archivo.estado_analisis = "omitido_config"
                analisis_archivo.tipo_analisis = "pdf_config_faltante"
        
        elif mime_type.startswith("text/"):
            logger.info(f"Archivo {archivo_adjunto_id} es un archivo de texto ({mime_type}).")
            file_content_bytes = obtener_contenido_archivo(archivo_adjunto)
            if file_content_bytes:
                try:
                    file_content_text = file_content_bytes.decode('utf-8', errors='replace')
                    analisis_archivo.texto_extraido = file_content_text
                    # Optional: Resumir texto plano con LLM
                    # prompt = f"Proporciona un resumen conciso del siguiente texto:\n\n{file_content_text[:10000]}"
                    # resumen = robust_chat(message=prompt, user_id=archivo_adjunto.user_id) # Assuming robust_chat can take user_id
                    # analisis_archivo.resumen = resumen
                    analisis_archivo.tipo_analisis = "texto_directo"
                except UnicodeDecodeError:
                    logger.error(f"Error de decodificación para archivo de texto ID: {archivo_adjunto_id}")
                    analisis_archivo.error_analisis = "Error de decodificación del archivo de texto."
                    analisis_archivo.estado_analisis = "error"
            else:
                analisis_archivo.error_analisis = "No se pudo leer el contenido del archivo de texto."
                analisis_archivo.estado_analisis = "error"
        else:
            logger.warning(f"Tipo de archivo no soportado ({mime_type}) para análisis avanzado del archivo ID: {archivo_adjunto_id}.")
            analisis_archivo.estado_analisis = "no_aplicable"
            analisis_archivo.tipo_analisis = "desconocido"

        if analisis_archivo.estado_analisis not in ["error", "procesando", "omitido_config"]:
             analisis_archivo.estado_analisis = "completado"

        analisis_archivo.fecha_analisis = datetime.utcnow()
        session.commit()
        logger.info(f"Análisis finalizado para ArchivoAdjunto ID: {archivo_adjunto_id}. Estado: {analisis_archivo.estado_analisis}")

    except requests.exceptions.RequestException as exc: 
        logger.error(f"Error de red en tarea de análisis para ArchivoAdjunto ID: {archivo_adjunto_id}: {exc}", exc_info=True)
        session.rollback()
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error(f"Máximos reintentos alcanzados para ArchivoAdjunto ID: {archivo_adjunto_id} por error de red.")
            if 'analisis_archivo' in locals() and analisis_archivo: # Check if analisis_archivo exists
                analisis_archivo.estado_analisis = "error"
                analisis_archivo.error_analisis = f"Error de red persistente: {str(exc)}"
                analisis_archivo.fecha_analisis = datetime.utcnow()
                session.commit()
    except Exception as e:
        logger.error(f"Error crítico en la tarea de análisis para ArchivoAdjunto ID: {archivo_adjunto_id}: {e}", exc_info=True)
        session.rollback()
        if 'analisis_archivo' in locals() and analisis_archivo: # Check if analisis_archivo exists
            analisis_archivo.estado_analisis = "error"
            analisis_archivo.error_analisis = str(e)
            analisis_archivo.fecha_analisis = datetime.utcnow()
            session.commit()
    finally:
        session.remove() # Ensure session is closed, important for Celery tasks


def analizar_imagen_con_vision_ocr_service(session, analisis_archivo_id: int):
    analisis_archivo = session.query(AnalisisArchivo).get(analisis_archivo_id)
    if not analisis_archivo or not analisis_archivo.archivo_adjunto:
        logger.error(f"No se encontró AnalisisArchivo o ArchivoAdjunto asociado para AnalisisArchivo ID: {analisis_archivo_id}")
        return

    archivo_adjunto = analisis_archivo.archivo_adjunto
    logger.info(f"Iniciando OCR con Vision para ArchivoAdjunto ID: {archivo_adjunto.id}, Análisis ID: {analisis_archivo.id}")

    try:
        image_content = obtener_contenido_archivo(archivo_adjunto)
        if not image_content:
            raise ValueError("No se pudo obtener el contenido de la imagen.")

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
        logger.error(f"Error durante el análisis OCR de la imagen ID {archivo_adjunto.id}: {e}", exc_info=True)
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
