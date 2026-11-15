import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from celery_utils import celery_app
from cutover_writer_fence import (
    background_writer_fence_report,
    cutover_writer_fence_enabled,
)
from global_writer_authority import global_writer_authority_enabled
from services.global_writer_authority import (
    background_global_writer_authority_report,
)

logger = logging.getLogger(__name__)


def _analysis_writer_fence_enabled() -> bool:
    try:
        from flask import current_app

        if cutover_writer_fence_enabled(current_app.config):
            return True
        return (
            background_global_writer_authority_report(
                "file_content_analysis",
                current_app.config,
            )
            is not None
        )
    except RuntimeError:
        return cutover_writer_fence_enabled() or global_writer_authority_enabled()

class AnalisisArchivoService:
    def crear_analisis_inicial(self, archivo_adjunto_id: int) -> Any:
        """
        Creates an initial analysis record for a given file.
        """
        from models import AnalisisArchivo, db

        logger.info(f"Creating initial analysis record for file ID: {archivo_adjunto_id}")
        analisis = AnalisisArchivo(
            archivo_adjunto_id=archivo_adjunto_id,
            estado_analisis="procesando",
            fecha_analisis=datetime.utcnow()
        )
        db.session.add(analisis)
        db.session.commit()
        return analisis

    def actualizar_analisis_completado(
        self,
        analisis_id: int,
        datos_estructurados: Optional[Dict | List],
        texto_extraido: Optional[str]
    ):
        """
        Updates an analysis record to 'completed' and saves the extracted data.
        """
        from models import AnalisisArchivo, db

        analisis = db.session.get(AnalisisArchivo, analisis_id)
        if not analisis:
            logger.error(f"Could not find AnalisisArchivo with ID: {analisis_id} to update.")
            return

        logger.info(f"Updating analysis record {analisis_id} to 'completado'.")
        analisis.estado_analisis = "completado"
        analisis.datos_estructurados = datos_estructurados
        analisis.texto_extraido = texto_extraido
        analisis.fecha_analisis = datetime.utcnow()
        analisis.error_analisis = None
        db.session.commit()

    def actualizar_analisis_con_error(self, analisis_id: int, error_message: str):
        """
        Updates an analysis record to 'error' and saves the error message.
        """
        from models import AnalisisArchivo, db

        analisis = db.session.get(AnalisisArchivo, analisis_id)
        if not analisis:
            logger.error(f"Could not find AnalisisArchivo with ID: {analisis_id} to update with error.")
            return

        logger.error(f"Updating analysis record {analisis_id} with error: {error_message}")
        analisis.estado_analisis = "error"
        analisis.error_analisis = error_message
        analisis.fecha_analisis = datetime.utcnow()
        db.session.commit()

@celery_app.task(name="analisis_archivo.tarea_analizar_contenido_archivo")
def tarea_analizar_contenido_archivo(archivo_adjunto_id: int, access_grant: str | None = None):
    """
    Celery task to analyze the content of a file asynchronously.
    """
    if _analysis_writer_fence_enabled():
        return {
            **background_writer_fence_report("file_content_analysis"),
            "processed": 0,
            "provider_attempts": 0,
        }

    from models import db
    from services.attachment_analysis_access import BackgroundAttachmentAccess
    from services.interpretacion_service import interpretacion_service

    logger.info(f"Iniciando tarea de análisis para ArchivoAdjunto ID: {archivo_adjunto_id}")
    service = AnalisisArchivoService()
    analisis = None
    try:
        access = BackgroundAttachmentAccess(archivo_adjunto_id, access_grant)
        archivo_adjunto = access.require()
        # Create an initial record to track that processing has started
        analisis = service.crear_analisis_inicial(archivo_adjunto_id)

        # Delegate to the interpretation service
        resultado_interpretacion = interpretacion_service.interpretar_archivo(
            archivo_adjunto, byte_reader=access.read_bytes, before_provider=access.before_provider)
        # Processing may overlap retirement, scope changes or writer fencing.
        access.require()
        if _analysis_writer_fence_enabled():
            return {**background_writer_fence_report("file_content_analysis"),
                    "processed": 0}
        if resultado_interpretacion.get("error"):
            service.actualizar_analisis_con_error(
                analisis.id, "No fue posible analizar el archivo.")
            return {"processed": 0, "reason_code": "attachment_analysis_failed"}

        # Update the analysis record with the result
        service.actualizar_analisis_completado(
            analisis_id=analisis.id,
            datos_estructurados=resultado_interpretacion.get("datos_estructurados"),
            texto_extraido=resultado_interpretacion.get("texto_extraido")
        )
        logger.info(f"Análisis completado exitosamente para ArchivoAdjunto ID: {archivo_adjunto_id}")
        return {"processed": 1}

    except Exception as e:
        # Neither broker grants nor provider/storage details belong in logs or UI.
        logger.error("File analysis failed (attachment_id=%s, error_type=%s)",
                     archivo_adjunto_id, type(e).__name__)
        db.session.rollback()
        if analisis and not _analysis_writer_fence_enabled():
            service.actualizar_analisis_con_error(
                analisis.id, "No fue posible analizar el archivo.")
        return {"processed": 0, "reason_code": "attachment_analysis_failed"}


def enqueue_file_content_analysis(archivo_adjunto_id: int):
    """Queue file analysis only while this process owns background writes."""

    if _analysis_writer_fence_enabled():
        return False
    from services.attachment_analysis_access import create_attachment_analysis_grant
    grant = create_attachment_analysis_grant(archivo_adjunto_id)
    return tarea_analizar_contenido_archivo.apply_async(
        args=(archivo_adjunto_id, grant),
        argsrepr=f"({archivo_adjunto_id}, '<attachment-analysis-grant>')",
    )
