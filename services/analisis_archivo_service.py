import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from models import db, AnalisisArchivo

logger = logging.getLogger(__name__)

class AnalisisArchivoService:
    def crear_analisis_inicial(self, archivo_adjunto_id: int) -> AnalisisArchivo:
        """
        Creates an initial analysis record for a given file.
        """
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
        analisis = db.session.get(AnalisisArchivo, analisis_id)
        if not analisis:
            logger.error(f"Could not find AnalisisArchivo with ID: {analisis_id} to update with error.")
            return

        logger.error(f"Updating analysis record {analisis_id} with error: {error_message}")
        analisis.estado_analisis = "error"
        analisis.error_analisis = error_message
        analisis.fecha_analisis = datetime.utcnow()
        db.session.commit()

# The existing Celery task can be kept for other asynchronous processing,
# but our IntelligentCatalogProcessor will use the service class directly.
# from celery_utils import celery_app
# @celery_app.task(...)
# def tarea_analizar_contenido_archivo(...)
# ...
