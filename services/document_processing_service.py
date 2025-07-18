# services/document_processing_service.py

import logging
from typing import Dict, Any, Optional
from google.cloud import documentai
from services.google_auth_util import get_google_credentials
from config import Config

logger = logging.getLogger(__name__)

class DocumentProcessingService:
    def __init__(self):
        self.credentials = get_google_credentials()
        self.client = None
        if self.credentials:
            try:
                self.client = documentai.DocumentProcessorServiceClient(credentials=self.credentials)
                logger.info("✅ [DOC_PROC_SVC] Cliente de Document AI inicializado exitosamente.")
            except Exception as e:
                logger.error(f"❌ [DOC_PROC_SVC] Error inicializando cliente de Document AI: {e}", exc_info=True)
        else:
            logger.error("❌ [DOC_PROC_SVC] No se pudieron cargar las credenciales de Google. El servicio de Document AI no funcionará.")

    def process_document(self, file_content: bytes, mime_type: str) -> Optional[documentai.Document]:
        if not self.client:
            logger.error("❌ [DOC_PROC_SVC] Cliente de Document AI no inicializado. No se puede procesar el documento.")
            return None

        processor_id = Config.GOOGLE_DOCAI_PROCESSOR_ID
        if not processor_id:
            logger.error(f"❌ [DOC_PROC_SVC] No se encontró un procesador de Document AI para el tipo de archivo: {mime_type}")
            return None

        try:
            processor_path = self.client.processor_path(
                Config.GOOGLE_PROJECT_ID,
                Config.GOOGLE_DOCAI_LOCATION,
                processor_id
            )

            raw_document = documentai.RawDocument(
                content=file_content,
                mime_type=mime_type,
            )

            request = documentai.ProcessRequest(
                name=processor_path,
                raw_document=raw_document,
            )

            result = self.client.process_document(request=request)
            return result.document

        except Exception as e:
            logger.error(f"❌ [DOC_PROC_SVC] Error procesando documento con Document AI: {e}", exc_info=True)
            return None

document_processing_service = DocumentProcessingService()
