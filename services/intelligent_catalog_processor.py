import os
import logging
from typing import Dict, Any, List
from werkzeug.utils import secure_filename
from services.google_search import google_search

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
        Eres un asistente experto en procesamiento de catálogos de productos para Argentina.
        Tu tarea es analizar el texto proporcionado y extraer una lista de productos en formato JSON.
        El JSON debe ser una lista de objetos, donde cada objeto representa un producto.
        Cada producto debe tener los siguientes campos: 'nombre', 'descripcion', 'precio', 'sku', 'marca', 'categoria', 'unidad'.

        Instrucciones importantes:
        1. **Categoria y Marca (Crítico):** Debes inferir SIEMPRE la 'categoria' y la 'marca' basándote en el nombre del producto, la descripción o el contexto general. No dejes estos campos vacíos.
           - Ejemplo: Si el producto es 'Rutini Malbec', Marca: 'Rutini', Categoria: 'Vinos Tintos'.
           - Ejemplo: Si es 'Coca Cola 1.5L', Marca: 'Coca Cola', Categoria: 'Bebidas'.
        2. **Precios (Argentina):** El formato de precios es argentino. El punto (.) se usa para miles y la coma (,) para decimales (ej: "$ 10.410" son diez mil cuatrocientos diez pesos).
           - Devuelve el precio como un número flotante (JSON number) o string SIN separadores de miles, usando punto para decimales si es necesario.
           - Ejemplo: Si el texto dice "$ 10.410", devuelve 10410. Si dice "5.207", devuelve 5207. Si dice "10,50", devuelve 10.50.
           - PRECAUCIÓN: No confundas "10.410" (diez mil) con "10.41" (diez con cuarenta). En este contexto, precios de productos como vinos suelen ser > 1000.
        3. **Descripción:** Si encuentras una descripción, inclúyela. Si no, genera una breve y atractiva basada en el nombre y tipo de producto.
        4. **Unidad:** Normaliza la unidad (ej: "u", "unid", "caja x6", "750ml").

        El resultado debe ser únicamente el JSON, sin ninguna otra explicación.
        """

        user_prompt = f"Aquí está el texto del catálogo:\n\n---\n{text}\n\n---\nPor favor, extráelo en el formato JSON especificado, asegurando inferir categorías y marcas cuando sea posible."

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

    def _enrich_item_data(self, item_data: Dict[str, Any]) -> Dict[str, Any]:
        """Enriches a single catalog item with an image URL and description if they are missing."""
        if not item_data.get('imagen_url'):
            try:
                query = f"{item_data.get('nombre', '')} {item_data.get('marca', '')}".strip()
                logger.info(f"Searching image for: {query}")
                search_results = google_search(query)
                # A simple strategy: take the first image result.
                # This could be improved with more sophisticated logic.
                if search_results and isinstance(search_results, list):
                    for result in search_results:
                        if result.get('pagemap') and result['pagemap'].get('cse_image'):
                            item_data['imagen_url'] = result['pagemap']['cse_image'][0]['src']
                            break
                elif search_results is None:
                    logger.warning("Google search returned None. Check API keys and CSE ID.")
            except Exception as e:
                logger.error(f"Error searching for image for item {item_data.get('nombre')}: {e}")

        if not item_data.get('descripcion'):
            try:
                # We can use the LLM to generate a description based on the item's name and brand.
                system_prompt = "Eres un asistente de marketing. Tu tarea es generar una descripción de producto concisa y atractiva."
                user_prompt = f"Genera una descripción para el producto '{item_data.get('nombre')}' de la marca '{item_data.get('marca')}'. Sé breve y destaca sus características principales."
                description = llamar_llm_para_json_estructurado(system_prompt=system_prompt, user_prompt=user_prompt)
                if description and isinstance(description, str):
                    item_data['descripcion'] = description
            except Exception as e:
                logger.error(f"Error generating description for item {item_data.get('nombre')}: {e}")

        return item_data

    def _save_catalog_items(self, items: List[Dict[str, Any]]):
        """Deletes the old catalog, enriches the new items, and saves them to the database."""
        # Delete old catalog items for the user
        CatalogoItem.query.filter_by(user_id=self.user_id).delete()

        for item_data in items:
            enriched_item_data = self._enrich_item_data(item_data)

            # Parse price for monetary field
            raw_price = enriched_item_data.get('precio', 0)
            try:
                price_float = float(raw_price) if raw_price else 0
            except (ValueError, TypeError):
                price_float = 0

            item = CatalogoItem(
                user_id=self.user_id,
                nombre=enriched_item_data.get('nombre'),
                descripcion=enriched_item_data.get('descripcion'),
                precio=str(raw_price),
                precio_monetario=price_float,
                cantidad=str(enriched_item_data.get('cantidad', '')),
                sku=enriched_item_data.get('sku'),
                marca=enriched_item_data.get('marca'),
                categoria=enriched_item_data.get('categoria'),
                unidad=enriched_item_data.get('unidad'),
                imagen_url=enriched_item_data.get('imagen_url'),
                extra_metadata=enriched_item_data # Store full raw data for future flexibility
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
