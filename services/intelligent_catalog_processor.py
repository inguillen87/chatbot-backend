import os
import logging
from typing import Dict, Any, List
from werkzeug.utils import secure_filename

from models import db, CatalogoItem, ArchivoAdjunto, AnalisisArchivo
from services.procesar_catalogo_excel import procesar_catalogo_excel
from services.generic_file_processor import procesar_archivo_generico
from services.google_vision_service import GoogleVisionService
from services.analisis_archivo_service import AnalisisArchivoService
from services.llm_utils import llamar_llm_para_json_estructurado

logger = logging.getLogger(__name__)

class IntelligentCatalogProcessor:
    def __init__(self, user_id: int):
        self.user_id = user_id

    def process_file(self, filepath: str, original_filename: str) -> bool:
        """
        Main method to process an uploaded catalog file.
        It identifies the file type and delegates to the appropriate processor.
        """
        logger.info(f"Starting intelligent catalog processing for user {self.user_id} with file {original_filename}")

        mime_type = self._get_mime_type(original_filename)

        # 1. Create ArchivoAdjunto and AnalisisArchivo records
        archivo_adjunto = self._create_archivo_adjunto(filepath, original_filename, mime_type)
        analisis_service = AnalisisArchivoService()
        analisis_archivo = analisis_service.crear_analisis_inicial(archivo_adjunto.id)

        try:
            extracted_text = None
            structured_data = None

            if mime_type in ['application/vnd.ms-excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']:
                structured_data = self._process_excel(filepath)
                # Excel processing doesn't easily yield a single "extracted_text" block, so we'll skip it for now.
            elif mime_type == 'application/pdf':
                extracted_text, structured_data = self._process_pdf(filepath)
            elif mime_type in ['application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']:
                extracted_text, structured_data = self._process_word(filepath)
            elif mime_type.startswith('image/'):
                extracted_text, structured_data = self._process_image(filepath)
            else:
                raise ValueError(f"Unsupported file type: {mime_type}")

            # 2. Save the structured data to CatalogoItem
            if structured_data:
                self._save_catalog_items(structured_data)
                analisis_service.actualizar_analisis_completado(
                    analisis_id=analisis_archivo.id,
                    datos_estructurados=structured_data,
                    texto_extraido=extracted_text
                )
                logger.info(f"Successfully processed and saved {len(structured_data)} items for user {self.user_id}")
                return True
            else:
                raise ValueError("No structured data could be extracted from the file.")

        except Exception as e:
            logger.error(f"Error processing catalog for user {self.user_id}: {e}", exc_info=True)
            analisis_service.actualizar_analisis_con_error(analisis_archivo.id, str(e))
            return False

    def _process_excel(self, filepath: str) -> List[Dict[str, Any]]:
        """Processes an Excel file."""
        logger.info(f"Processing Excel file: {filepath}")
        # We can directly use the sophisticated excel processor
        # The user object will be needed for rubro, for now we pass a generic one
        from models import User
        user = User.query.get(self.user_id)
        rubro = user.rubro.nombre if user and user.rubro else "generico"

        data = procesar_catalogo_excel(filepath, self.user_id, rubro)
        # The output of procesar_catalogo_excel needs to be mapped to CatologoItem fields
        return self._normalize_data(data)


    def _process_pdf(self, filepath: str) -> tuple[str, List[Dict[str, Any]]]:
        """Processes a PDF file using LLM."""
        logger.info(f"Processing PDF file: {filepath}")
        analysis_result = procesar_archivo_generico(filepath, 'application/pdf')
        if not analysis_result or not analysis_result.get('texto_extraido'):
            raise ValueError("Could not extract text from PDF.")

        extracted_text = analysis_result['texto_extraido']
        structured_data = self._get_structured_data_from_llm(extracted_text)
        return extracted_text, self._normalize_data(structured_data)

    def _process_word(self, filepath: str) -> tuple[str, List[Dict[str, Any]]]:
        """Processes a Word document using LLM."""
        logger.info(f"Processing Word file: {filepath}")
        analysis_result = procesar_archivo_generico(filepath, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        if not analysis_result or not analysis_result.get('texto_extraido'):
            raise ValueError("Could not extract text from Word document.")

        extracted_text = analysis_result['texto_extraido']
        structured_data = self._get_structured_data_from_llm(extracted_text)
        return extracted_text, self._normalize_data(structured_data)

    def _process_image(self, filepath: str) -> tuple[str, List[Dict[str, Any]]]:
        """Processes an image file using Vision OCR and LLM."""
        logger.info(f"Processing image file: {filepath}")
        vision_service = GoogleVisionService()
        with open(filepath, "rb") as image_file:
            content = image_file.read()

        # Assuming detect_text returns a response object with a text_annotations attribute
        response = vision_service.detect_text(content)
        if not response or not response.text_annotations:
            raise ValueError("No text found in image by Vision API.")

        extracted_text = response.text_annotations[0].description
        structured_data = self._get_structured_data_from_llm(extracted_text)
        return extracted_text, self._normalize_data(structured_data)

    def _get_structured_data_from_llm(self, text: str) -> List[Dict[str, Any]]:
        """Uses a specific prompt to get structured product data from text using an LLM."""

        system_prompt = """
        Eres un asistente experto en procesamiento de catálogos de productos.
        Tu tarea es analizar el texto proporcionado y extraer una lista de productos en formato JSON.
        El JSON debe ser una lista de objetos, donde cada objeto representa un producto.
        Cada producto debe tener los siguientes campos: 'nombre', 'descripcion', 'precio', 'sku', 'marca', 'categoria', 'unidad'.
        Si un campo no está presente, puedes omitirlo o dejarlo como un string vacío.
        El campo 'precio' debe ser un string, no un número.
        Analiza cuidadosamente el texto para identificar cada producto y sus detalles.
        El resultado debe ser únicamente el JSON, sin ninguna otra explicación.
        """

        user_prompt = f"Aquí está el texto del catálogo:\n\n---\n{text}\n\n---\nPor favor, extráelo en el formato JSON especificado."

        response_json = llamar_llm_para_json_estructurado(
            system_prompt=system_prompt,
            user_prompt=user_prompt
        )

        if not response_json or not isinstance(response_json, list):
            logger.error(f"LLM did not return a valid list of products. Response: {response_json}")
            return []

        return response_json

    def _normalize_data(self, data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensures all items have the required keys for the CatalogoItem model."""
        normalized = []
        # The keys required by the CatalogoItem model
        model_keys = ['nombre', 'descripcion', 'precio', 'cantidad', 'sku', 'marca', 'categoria', 'unidad', 'imagen_url']

        for item in data:
            norm_item = {}
            # Map data from processors (Excel, LLM) to model keys
            norm_item['nombre'] = item.get('nombre')
            norm_item['descripcion'] = item.get('descripcion')
            norm_item['precio'] = item.get('precio')
            norm_item['cantidad'] = item.get('cantidad') or item.get('stock') # Handle both keys
            norm_item['sku'] = item.get('sku')
            norm_item['marca'] = item.get('marca')
            norm_item['categoria'] = item.get('categoria') or item.get('categoria_producto') # Handle both
            norm_item['unidad'] = item.get('unidad')
            norm_item['imagen_url'] = item.get('imagen_url')

            # Ensure all model keys exist, even if None
            for key in model_keys:
                if key not in norm_item:
                    norm_item[key] = None

            if not norm_item.get('nombre'):
                continue # Skip items without a name
            normalized.append(norm_item)
        return normalized

    def _save_catalog_items(self, items: List[Dict[str, Any]]):
        """Deletes the old catalog and saves the new items to the database."""
        # Delete old catalog items for the user
        CatalogoItem.query.filter_by(user_id=self.user_id).delete()

        for item_data in items:
            item = CatalogoItem(
                user_id=self.user_id,
                nombre=item_data.get('nombre'),
                descripcion=item_data.get('descripcion'),
                precio=str(item_data.get('precio', '')),
                cantidad=str(item_data.get('cantidad', '')),
                sku=item_data.get('sku'),
                marca=item_data.get('marca'),
                categoria=item_data.get('categoria'),
                unidad=item_data.get('unidad'),
                imagen_url=item_data.get('imagen_url')
            )
            db.session.add(item)

        db.session.commit()

    def _create_archivo_adjunto(self, filepath: str, original_filename: str, mime_type: str) -> ArchivoAdjunto:
        """Creates an ArchivoAdjunto record for the uploaded file."""
        # This is a simplified version. In a real app, the file would be moved
        # to a persistent storage and a URL would be generated.
        # For now, we'll just record it.
        filename = secure_filename(original_filename)
        new_adj = ArchivoAdjunto(
            user_id=self.user_id,
            filename=filename,
            nombre_original=original_filename,
            mime=mime_type,
            tamano=os.path.getsize(filepath),
            tipo="catalogo",
            url=filepath # In a real system, this would be a GCS/S3 URL
        )
        db.session.add(new_adj)
        db.session.commit()
        return new_adj

    def _get_mime_type(self, filename: str) -> str:
        """Determines the MIME type from the file extension."""
        # This is a basic implementation. A more robust one would use a library like `python-magic`.
        ext = os.path.splitext(filename)[1].lower()
        mime_types = {
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.xls': 'application/vnd.ms-excel',
            '.pdf': 'application/pdf',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.doc': 'application/msword',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
        }
        return mime_types.get(ext, 'application/octet-stream')
