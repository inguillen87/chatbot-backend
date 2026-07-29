import os
import logging
from typing import Dict, Any, List
from werkzeug.utils import secure_filename
from services.google_search import google_search

from models import db, CatalogoItem, ArchivoAdjunto, AnalisisArchivo, User, TenantProfile
from services.procesar_catalogo_excel import procesar_catalogo_excel
from services.generic_file_processor import procesar_archivo_generico
from services.google_vision_service import GoogleVisionService
from services.analisis_archivo_service import AnalisisArchivoService
from services.llm_utils import llamar_llm_para_json_estructurado
from utils.money_ar import parse_ars
from services.r2_service import r2_service
from services.openai_model_defaults import (
    DEFAULT_OPENAI_SOL_MODEL,
    resolve_openai_model,
)

logger = logging.getLogger(__name__)


def _catalog_processing_model() -> str:
    return resolve_openai_model(
        "OPENAI_CATALOG_PROCESSING_MODEL",
        DEFAULT_OPENAI_SOL_MODEL,
    )

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
        # Now uploads to R2 immediately
        archivo_adjunto = self._create_archivo_adjunto(filepath, original_filename, mime_type)
        if not archivo_adjunto:
             logger.error("Failed to create ArchivoAdjunto or upload to R2.")
             return False

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
        user = db.session.get(User, self.user_id)
        rubro = user.rubro.nombre if user and user.rubro else "generico"

        data = procesar_catalogo_excel(filepath, self.user_id, rubro)
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
        """Uses a dynamic prompt based on user's business type (rubro) to extract data."""

        user = db.session.get(User, self.user_id)
        rubro_nombre = "generico"
        if user and user.rubro:
            rubro_nombre = (user.rubro.nombre or "").lower()

        logger.info(f"Generating extraction prompt for rubro: '{rubro_nombre}'")

        is_wine_sector = any(k in rubro_nombre for k in ["vino", "bodega", "licor", "bebida"])
        is_food_sector = any(k in rubro_nombre for k in ["comida", "restaurante", "bar", "menu", "gastronomia"])

        base_instructions = """
        Eres un asistente experto en procesamiento de catálogos de productos para Argentina.
        Tu tarea es analizar el texto (que puede venir de un PDF o imagen mal formateado) y extraer una lista de productos en formato JSON.
        El JSON debe ser una lista de objetos, donde cada objeto representa un producto.

        **Instrucciones Generales:**
        1. **Precios (Argentina):** El formato de precios es argentino. El punto (.) se usa para miles y la coma (,) para decimales.
           - DEVUELVE EL PRECIO COMO UN STRING TEXTUAL EXACTO (ej: "10.410" o "5.207").
           - NO lo conviertas a número flotante tú mismo. Déjalo como string.
           - Si el texto dice "$ 5,50" pero por contexto parece un precio alto (ej: electrodomésticos, vinos finos), es probable que sea un error de OCR y signifique 5500. Si parece pesos argentinos, asume miles si es coherente.
           - Ejemplo: "5.207" -> devolver string "5.207".
        2. **Limpieza:** Elimina caracteres extraños del nombre (ej: "•", "-", etc. al inicio).
        """

        if is_wine_sector:
            specific_instructions = """
            **Instrucciones Específicas para VINOS y BEBIDAS:**
            1. **Marca (Bodega/Línea):** Debes inferir la 'marca' basándote en el nombre de la botella o el contexto.
               - Si el nombre dice "Rutini Malbec", Marca: "Rutini".
               - Si el nombre es "Luigi Bosca Cabernet", Marca: "Luigi Bosca".
               - IMPORTANTE: Si la marca no está explícita como columna, extráela del comienzo del nombre del producto.
            2. **Varietal:** Extrae el varietal (ej: Malbec, Cabernet, Blend).
            3. **Presentación:** Extrae el tamaño o formato (ej: "750ml", "Caja x6"). Si es una caja, indícalo claramente.
            4. **Descripción:** Si no hay descripción, genera una breve basada en el varietal y marca (ej: "Vino tinto de cuerpo medio...").
            5. **Campos requeridos:** nombre, precio, marca, varietal, categoria, descripcion, unidad, presentacion.
            """
        elif is_food_sector:
            specific_instructions = """
            **Instrucciones Específicas para GASTRONOMÍA:**
            1. **Categoría:** Clasifica en Entradas, Principales, Postres, Bebidas, etc.
            2. **Descripción:** Extrae ingredientes si están listados.
            3. **Campos requeridos:** nombre, precio, descripcion, categoria.
            """
        else:
            specific_instructions = """
            **Instrucciones Específicas Generales:**
            1. **Marca:** Si el producto tiene marca (ej: "Taladro Bosch"), extráela en el campo 'marca'.
            2. **Categoría:** Infiere una categoría lógica (ej: "Herramientas", "Limpieza").
            3. **Campos requeridos:** nombre, precio, marca, categoria, descripcion, unidad.
            """

        system_prompt = f"{base_instructions}\n{specific_instructions}\nEl resultado debe ser únicamente el JSON, sin ninguna otra explicación."

        user_prompt = f"Aquí está el texto del catálogo:\n\n---\n{text}\n\n---\nPor favor, extráelo en el formato JSON especificado."

        response_json = llamar_llm_para_json_estructurado(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=_catalog_processing_model(),
        )

        if not response_json or not isinstance(response_json, list):
            logger.error(
                "LLM did not return a valid product list model=%s response_type=%s",
                _catalog_processing_model(),
                type(response_json).__name__,
            )
            return []

        return response_json

    def _normalize_data(self, data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensures all items have the required keys for the CatalogoItem model."""
        normalized = []
        model_keys = ['nombre', 'descripcion', 'precio', 'cantidad', 'sku', 'marca', 'categoria', 'unidad', 'imagen_url', 'varietal', 'anada', 'presentacion']

        for item in data:
            norm_item = {}
            norm_item['nombre'] = item.get('nombre')
            norm_item['descripcion'] = item.get('descripcion')
            norm_item['precio'] = item.get('precio')
            norm_item['cantidad'] = item.get('cantidad') or item.get('stock')
            norm_item['sku'] = item.get('sku')
            norm_item['marca'] = item.get('marca')
            norm_item['categoria'] = item.get('categoria') or item.get('categoria_producto')
            norm_item['unidad'] = item.get('unidad')
            norm_item['imagen_url'] = item.get('imagen_url')
            norm_item['varietal'] = item.get('varietal')
            norm_item['anada'] = item.get('anada')
            norm_item['presentacion'] = item.get('presentacion')

            for key in model_keys:
                if key not in norm_item:
                    norm_item[key] = None

            if not norm_item.get('nombre'):
                continue
            normalized.append(norm_item)
        return normalized

    def _enrich_item_data(self, item_data: Dict[str, Any]) -> Dict[str, Any]:
        """Enriches a single catalog item with an image URL and description if they are missing."""
        if not item_data.get('imagen_url'):
            try:
                query = f"{item_data.get('nombre', '')} {item_data.get('marca', '')}".strip()
                logger.info(f"Searching image for: {query}")
                search_results = google_search(query)
                if search_results and isinstance(search_results, list):
                    for result in search_results:
                        if result.get('pagemap') and result['pagemap'].get('cse_image'):
                            item_data['imagen_url'] = result['pagemap']['cse_image'][0]['src']
                            break
            except Exception as e:
                logger.error(f"Error searching for image for item {item_data.get('nombre')}: {e}")

        if not item_data.get('descripcion'):
            try:
                system_prompt = "Eres un asistente de marketing. Tu tarea es generar una descripción de producto concisa y atractiva."
                user_prompt = f"Genera una descripción para el producto '{item_data.get('nombre')}' de la marca '{item_data.get('marca')}'. Sé breve y destaca sus características principales."
                description = llamar_llm_para_json_estructurado(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model=_catalog_processing_model(),
                )
                if description and isinstance(description, str):
                    item_data['descripcion'] = description
            except Exception as e:
                logger.error(f"Error generating description for item {item_data.get('nombre')}: {e}")

        return item_data

    def _save_catalog_items(self, items: List[Dict[str, Any]]):
        """Deletes the old catalog, enriches the new items, and saves them to the database."""
        CatalogoItem.query.filter_by(user_id=self.user_id).delete()

        for item_data in items:
            enriched_item_data = self._enrich_item_data(item_data)

            raw_price = enriched_item_data.get('precio', 0)
            price_decimal = parse_ars(raw_price)
            price_float = float(price_decimal) if price_decimal is not None else 0.0

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
                varietal=enriched_item_data.get('varietal'),
                anada=enriched_item_data.get('anada'),
                presentacion=enriched_item_data.get('presentacion'),
                extra_metadata=enriched_item_data
            )
            db.session.add(item)

        db.session.commit()

    def _create_archivo_adjunto(self, filepath: str, original_filename: str, mime_type: str) -> ArchivoAdjunto:
        """Creates an ArchivoAdjunto record, uploads to R2, and returns the record."""
        user = db.session.get(User, self.user_id)
        if not user:
            logger.error(f"User {self.user_id} not found.")
            return None

        # Determine Tenant Context for R2 key
        tenant_slug = "generico"
        tenant_type = "pymes" # Default for this processor usually

        if user.tenant_slug:
            tenant_slug = user.tenant_slug
        elif user.rubro:
            # Fallback to rubro if tenant slug is missing (e.g. legacy)
            tenant_slug = f"legacy-{user.rubro.nombre.lower().replace(' ', '-')}-{user.id}"

        # Override if municipality
        if user.tipo_chat == "municipio" or user.municipio_id:
            tenant_type = "municipios"
            # Try to find tenant profile for better slug
            tenant = TenantProfile.query.filter_by(municipio_id=user.municipio_id).first()
            if tenant:
                tenant_slug = tenant.slug
        else:
            # Try to find pyme tenant profile
            tenant = TenantProfile.query.filter_by(pyme_id=user.id).first()
            if tenant:
                tenant_slug = tenant.slug

        filename = secure_filename(original_filename)
        # R2 Key: type/slug/catalogos/filename
        key = f"{tenant_type}/{tenant_slug}/catalogos/{filename}"

        logger.info(f"Uploading catalog to R2 with key: {key}")

        # Open file and upload
        try:
            with open(filepath, "rb") as f:
                public_url = r2_service.upload_file_with_key(f, key, mime_type)

            if not public_url:
                logger.error("R2 Upload returned None.")
                return None

            new_adj = ArchivoAdjunto(
                user_id=self.user_id,
                filename=filename,
                nombre_original=original_filename,
                mime=mime_type,
                tamano=os.path.getsize(filepath),
                tipo="catalogo",
                url=public_url # Save the CDN URL!
            )
            db.session.add(new_adj)
            db.session.commit()
            return new_adj

        except Exception as e:
            logger.error(f"Error uploading/saving attachment: {e}")
            return None

    def _get_mime_type(self, filename: str) -> str:
        """Determines the MIME type from the file extension."""
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
