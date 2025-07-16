# services/interpretacion_imagen_service.py
import logging
import requests
import random # Para el mock de AnalisisArchivo en las pruebas
from typing import Dict, Any, List, Optional

from models import ArchivoAdjunto, AnalisisArchivo, User, CatalogoItem, db
from services.google_vision_service import analyze_image_from_content, VISION_CLIENT # Import VISION_CLIENT para pruebas
from services.llm_utils import extract_complaint_details_llm
from services.common_utils import limpiar_texto_base, parse_precio_flexible # Para procesar texto de pedido
from services.pedido_processor_service import calcular_similitud_levenshtein, UMBRAL_SIMILITUD_PRODUCTO_PEDIDO # Reutilizar lógica de matching

logger = logging.getLogger(__name__)

# --- Constantes para Reclamos Municipales ---
PALABRAS_CLAVE_RECLAMO_OBJETOS = {
    "streetlight", "street light", "light pole", "lamp post", "traffic light", "traffic signal",
    "pothole", "hole", "crack", "broken pavement",
    "trash", "garbage", "waste", "dumpster", "overflowing bin",
    "leak", "pipe burst", "water leak", "flooding",
    "fallen tree", "broken branch",
    "graffiti",
    "broken sign", "street sign",
    "blocked drain", "sewer",
}
PALABRAS_CLAVE_RECLAMO_ETIQUETAS = {
    "public utility", "infrastructure", "road", "street", "sidewalk",
    "hazard", "danger", "damage", "broken", "fallen", "overflowing",
    "vandalism", "neglect",
}

# --- Constantes para Pedidos PYME (si fueran necesarias, por ahora parseo directo) ---
# Ejemplo: PALABRAS_CLAVE_PEDIDO_TEXTO = {"pedido", "orden", "comprar", ...}


def _descargar_imagen(url: str) -> Optional[bytes]:
    """Descarga el contenido de una imagen desde una URL."""
    try:
        response = requests.get(url, timeout=10) # Timeout de 10 segundos
        response.raise_for_status() # Lanza excepción para códigos de error HTTP
        return response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Error al descargar imagen desde {url}: {e}", exc_info=True)
        return None

def _inicializar_analisis_archivo(archivo_adjunto_id: int, tipo_analisis_inicial: str) -> AnalisisArchivo:
    """Obtiene o crea un registro de AnalisisArchivo."""
    analisis = AnalisisArchivo.query.filter_by(archivo_adjunto_id=archivo_adjunto_id).first()
    if not analisis:
        analisis = AnalisisArchivo(
            archivo_adjunto_id=archivo_adjunto_id,
            tipo_analisis=tipo_analisis_inicial
        )
        db.session.add(analisis)
    else: # Si ya existe, reseteamos para un nuevo análisis (o podríamos actualizar el existente)
        analisis.tipo_analisis = tipo_analisis_inicial
        analisis.datos_estructurados = {}
        analisis.texto_extraido = None
        analisis.error_analisis = None

    analisis.estado_analisis = "procesando"
    db.session.commit()
    return analisis

def interpretar_imagen_para_chat(
    archivo_adjunto: ArchivoAdjunto,
    tipo_interpretacion: str, # "reclamo_municipal" o "pedido_pyme"
    pyme_user: Optional[User] = None # Requerido si tipo_interpretacion es "pedido_pyme"
) -> Dict[str, Any]:
    """
    Función principal para interpretar una imagen según el tipo de necesidad (reclamo o pedido).
    Maneja tanto `ArchivoAdjunto` de la DB como diccionarios con info de URL (ej. de WhatsApp).
    """
    is_db_object = hasattr(archivo_adjunto, 'id') and archivo_adjunto.id is not None

    input_url = None
    input_mime_type = None
    input_source_id_info = "" # For logging

    if is_db_object:
        input_url = archivo_adjunto.url
        input_mime_type = archivo_adjunto.mime
        input_source_id_info = f"ArchivoAdjunto ID {archivo_adjunto.id}"
    elif isinstance(archivo_adjunto, dict):
        input_url = archivo_adjunto.get("url")
        input_mime_type = archivo_adjunto.get("mime_type") # Asumimos que el dict tiene 'mime_type'
        input_source_id_info = f"Diccionario (URL: {input_url})"
    else: # tipo inesperado
        logger.error(f"❌ Tipo de archivo_adjunto no esperado: {type(archivo_adjunto)}")
        return {'error': 'Tipo de archivo_adjunto no válido.', 'analisis_id': None}

    if not input_url:
        return {'error': 'URL del archivo no válida.', 'analisis_id': None}

    if tipo_interpretacion == "pedido_pyme" and not pyme_user:
        logger.error("❌ Se requiere pyme_user para interpretar un pedido PYME.")
        return {'error': 'Usuario PYME no especificado para interpretación de pedido.', 'analisis_id': None}

    analisis_db_record = None # Será None si es un dict de WhatsApp, o el objeto AnalisisArchivo si es de DB

    if is_db_object:
        tipo_analisis_db_prefix = f'{tipo_interpretacion}_vision_v1'
        analisis_db_record = _inicializar_analisis_archivo(archivo_adjunto.id, tipo_analisis_db_prefix)
        logger.info(f"➡️ Iniciando interpretación '{tipo_interpretacion}' para {input_source_id_info}")
    else: # Es un diccionario (ej. WhatsApp), no interactuamos con AnalisisArchivo todavía
        logger.info(f"➡️ Iniciando interpretación '{tipo_interpretacion}' para imagen desde {input_source_id_info} (sin interacción con DB de AnalisisArchivo en esta etapa).")


    image_content = _descargar_imagen(input_url)
    if not image_content:
        error_message = "Fallo al descargar la imagen."
        if is_db_object and analisis_db_record:
            analisis_db_record.estado_analisis = "error"
            analisis_db_record.error_analisis = error_message
            db.session.commit()
            return {'error': error_message, 'analisis_id': analisis_db_record.id}
        else: # WhatsApp dict, no hay analisis_db_record
            return {'error': error_message, 'analisis_id': None, 'raw_analysis': None}

    logger.info(f"🖼️  Enviando imagen (tamaño: {len(image_content)} bytes, mime: {input_mime_type}) a Vision API...")
    vision_results = analyze_image_from_content(image_content) # Esta función ya loguea sus errores

    # Si es un objeto de DB, guardar resultados parciales de Vision en AnalisisArchivo
    if is_db_object and analisis_db_record:
        current_datos_db = analisis_db_record.datos_estructurados or {}
        current_datos_db['vision_api_raw'] = vision_results # Guardar el resultado crudo de Vision
        analisis_db_record.datos_estructurados = current_datos_db

    if vision_results.get("error"):
        error_message_vision = f"Error de Vision API: {vision_results['error']}"
        logger.error(f"❌ {error_message_vision}")
        if is_db_object and analisis_db_record:
            analisis_db_record.estado_analisis = "error"
            analisis_db_record.error_analisis = error_message_vision
            db.session.commit()
            return {'error': error_message_vision, 'analisis_id': analisis_db_record.id}
        else: # WhatsApp dict
            return {'error': error_message_vision, 'analisis_id': None, 'raw_analysis': {'vision_api_raw': vision_results}}

    extracted_ocr_text = ""
    if vision_results.get("full_text_annotation"):
        extracted_ocr_text = vision_results["full_text_annotation"].get("description", "").strip()
        if is_db_object and analisis_db_record:
            analisis_db_record.texto_extraido = extracted_ocr_text
        logger.info(f" टेक्स्ट OCR detectado: '{extracted_ocr_text[:200]}...'")

    # Si es un objeto de DB, guardar el análisis con texto OCR antes de la lógica específica.
    if is_db_object and analisis_db_record:
        db.session.commit()

    # ----- Lógica de procesamiento específica (reclamo o pedido) -----
    # Estas funciones (_procesar_interpretacion_reclamo, _procesar_interpretacion_pedido_pyme)
    # ahora recibirán `analisis_db_record` (que puede ser None si es un dict de WhatsApp).
    # Deberán manejar esto: si es None, no intentan actualizarlo.
    # Y la función principal retornará el resultado de estas, añadiendo `mime_type` si no es de DB.

    resultado_procesamiento = None
    if tipo_interpretacion == "reclamo_municipal":
        resultado_procesamiento = _procesar_interpretacion_reclamo(analisis_db_record, vision_results, extracted_ocr_text)
    elif tipo_interpretacion == "reclamo_auto_descripcion_categoria":
        resultado_procesamiento = _procesar_interpretacion_reclamo(analisis_db_record, vision_results, extracted_ocr_text, auto_mode=True)
    elif tipo_interpretacion == "pedido_pyme":
        if not pyme_user: # Doble chequeo
            error_msg_pyme = "Error interno: pyme_user es None para pedido_pyme en _procesar."
            logger.error(f"❌ {error_msg_pyme}")
            if is_db_object and analisis_db_record:
                analisis_db_record.estado_analisis = "error"; analisis_db_record.error_analisis = error_msg_pyme; db.session.commit()
                return {'error': error_msg_pyme, 'analisis_id': analisis_db_record.id}
            else: return {'error': error_msg_pyme, 'analisis_id': None, 'raw_analysis': {'vision_api_raw': vision_results}} # Propagar error y raw vision
        resultado_procesamiento = _procesar_interpretacion_pedido_pyme(analisis_db_record, vision_results, extracted_ocr_text, pyme_user)
    elif tipo_interpretacion == "orden_de_compra":
        resultado_procesamiento = _procesar_interpretacion_orden_de_compra(analisis_db_record, vision_results, extracted_ocr_text)
    else:
        error_msg_tipo = f"Tipo de interpretación no soportado: {tipo_interpretacion}"
        logger.error(f"❌ {error_msg_tipo}")
        if is_db_object and analisis_db_record:
            analisis_db_record.estado_analisis = "error"; analisis_db_record.error_analisis = error_msg_tipo; db.session.commit()
            return {'error': error_msg_tipo, 'analisis_id': analisis_db_record.id}
        else: return {'error': error_msg_tipo, 'analisis_id': None, 'raw_analysis': {'vision_api_raw': vision_results}}


    # Si no es un objeto de DB (es un dict de WhatsApp), necesitamos enriquecer el resultado
    # con la información cruda del análisis y el mime_type original.
    if not is_db_object:
        if resultado_procesamiento: # Si el procesamiento específico tuvo éxito
            # Añadir los datos crudos de análisis y mime_type
            # raw_analysis_data contendrá los resultados de vision y la extracción del LLM (si aplica)
            raw_analysis_data_for_return = {
                'vision_api_raw': vision_results,
                'extracted_ocr_text': extracted_ocr_text,
                # Si _procesar_interpretacion_reclamo/pedido devuelven datos adicionales
                # (ej. llm_extraction), deberían estar en resultado_procesamiento.
                # Aquí podemos decidir qué parte de resultado_procesamiento es "raw" vs "final".
                # Por ahora, asumimos que resultado_procesamiento ya tiene la estructura deseada
                # para 'categoria_sugerida', 'descripcion_sugerida', etc.
                # Y 'raw_analysis' contendrá las fuentes primarias de datos.
            }
            if 'llm_complaint_extraction_from_image' in (resultado_procesamiento.get('analisis_interno', {})):
                raw_analysis_data_for_return['llm_complaint_extraction_from_image'] = resultado_procesamiento['analisis_interno']['llm_complaint_extraction_from_image']

            resultado_procesamiento['raw_analysis'] = raw_analysis_data_for_return
            resultado_procesamiento['mime_type'] = input_mime_type # Agregar el mime_type original
            resultado_procesamiento['analisis_id'] = None # Explicitar que no hay ID de AnalisisArchivo
        else: # Si el procesamiento específico falló (devolvió None o dict con error)
            # Esto no debería pasar si las funciones _procesar_ siempre devuelven un dict.
            # Pero por si acaso:
            logger.error("❌ Error inesperado: resultado_procesamiento es None para input tipo dict.")
            return {
                'error': 'Error interno en procesamiento específico de la imagen.',
                'analisis_id': None,
                'raw_analysis': {'vision_api_raw': vision_results, 'extracted_ocr_text': extracted_ocr_text},
                'mime_type': input_mime_type
            }

    return resultado_procesamiento

# Define mapping from common Vision API labels (in lowercase normalized form) to our claim categories
# This needs to be expanded and refined.
VISION_LABEL_TO_RECLAMO_CATEGORIA = {
    "pothole": "Arreglo de calle",
    "street light": "Luminaria", "lamp post": "Luminaria",
    "traffic light": "Rotura de semaforo",
    "tree": "Arbol Caido",
    "fallen tree": "Arbol Caido",
    "fire": "Incendio", "smoke": "Incendio", "flame": "Incendio",
    "trash": "Limpieza", "garbage": "Limpieza", "waste": "Limpieza",
    "water leak": "Falta de agua, rotura de caño", "pipe": "Falta de agua, rotura de caño", "leak": "Falta de agua, rotura de caño",
    "leakage": "Falta de agua, rotura de caño",
    "road": "Arreglo de calle", # Generic, might need more context
    "signage": "Rotura de semaforo", # If context implies damage/issue, could be other types of signs
    "power line": "Luminaria" # Or a generic public service issue
}
# Also import CATEGORIAS_RECLAMO from municipios to validate against
try:
    from services.municipios import CATEGORIAS_RECLAMO, normalizar_texto as normalizar_texto_municipios
except ImportError: # Fallback if circular or testing standalone
    CATEGORIAS_RECLAMO = ["arbol caido", "arreglo de calle", "incendio", "luminaria", "rotura de semaforo", "limpieza", "falta de agua, rotura de caño", "otro motivo"]
    def normalizar_texto_municipios(s): return s.lower() if s else ""


def _infer_category_from_vision_results(vision_results: Dict[str, Any], min_confidence: float = 0.55) -> Optional[str]:
    """Infers a claim category from Vision API labels and objects."""
    detected_items_with_confidence = []
    for label in vision_results.get("labels", []):
        confidence = label.get("confidence", 0)
        if confidence >= min_confidence:
            detected_items_with_confidence.append({
                "text": normalizar_texto_municipios(label.get("description","")),
                "score": confidence
            })
    for obj in vision_results.get("objects", []):
        confidence = obj.get("confidence", 0)
        if confidence >= min_confidence:
             detected_items_with_confidence.append({
                "text": normalizar_texto_municipios(obj.get("name","")),
                "score": confidence
            })

    # Sort by confidence
    detected_items_with_confidence.sort(key=lambda x: x["score"], reverse=True)
    logger.info(f"[VISION_CAT_INFERENCE] Sorted detected items: {detected_items_with_confidence}")

    for item in detected_items_with_confidence:
        item_desc = item["text"]
        # Direct mapping first
        if item_desc in VISION_LABEL_TO_RECLAMO_CATEGORIA:
            cat = VISION_LABEL_TO_RECLAMO_CATEGORIA[item_desc]
            if cat in CATEGORIAS_RECLAMO:
                logger.info(f"[VISION_CAT_INFERENCE] Mapped '{item_desc}' to category '{cat}' with score {item['score']}")
                return cat
        # Check if any part of a multi-word item_desc maps
        for keyword, category_map in VISION_LABEL_TO_RECLAMO_CATEGORIA.items():
            if keyword in item_desc:
                 if category_map in CATEGORIAS_RECLAMO:
                    logger.info(f"[VISION_CAT_INFERENCE] Mapped partial '{item_desc}' (found '{keyword}') to category '{category_map}' with score {item['score']}")
                    return category_map
    return None


def _procesar_interpretacion_reclamo(
    analisis_db_record: Optional[AnalisisArchivo], # Puede ser None si es de WhatsApp
    vision_results: Dict[str, Any],
    extracted_ocr_text: str,
    auto_mode: bool = False
) -> Dict[str, Any]:
    """Lógica específica para interpretar un reclamo municipal."""
    analisis_id_for_log = analisis_db_record.id if analisis_db_record else "N/A (WhatsApp)"
    logger.info(f"⚙️ Procesando como RECLAMO MUNICIPAL (auto_mode: {auto_mode}) para Análisis ID: {analisis_id_for_log}")

    sugerida_categoria_vision = None
    # Datos que se guardarán en AnalisisArchivo (si existe) o se retornarán en 'analisis_interno'
    datos_internos_analisis = {}

    if auto_mode:
        sugerida_categoria_vision = _infer_category_from_vision_results(vision_results)
        if analisis_db_record:
            analisis_db_record.tipo_analisis = 'reclamo_auto_vision_v1'
        datos_internos_analisis['tipo_analisis_sugerido'] = 'reclamo_auto_vision_v1'
    else:
        if analisis_db_record:
            analisis_db_record.tipo_analisis = 'reclamo_vision_llm_v1'
        datos_internos_analisis['tipo_analisis_sugerido'] = 'reclamo_vision_llm_v1'

    # Construct description for LLM from image content
    prompt_description_parts = []
    top_labels_str = ", ".join([f"{l['description']}" for l in vision_results.get("labels", [])[:5]])
    top_objects_str = ", ".join([f"{o['name']}" for o in vision_results.get("objects", [])[:3]])

    if top_objects_str:
        prompt_description_parts.append(f"Objetos principales detectados: {top_objects_str}")
    if top_labels_str:
        prompt_description_parts.append(f"Aspectos generales de la imagen: {top_labels_str}")

    ocr_snippet_for_prompt = ""
    if extracted_ocr_text:
        ocr_snippet_for_prompt = extracted_ocr_text.strip().replace("\n", " ")
        prompt_description_parts.append(f"Texto en imagen: '{ocr_snippet_for_prompt}'")

    if not prompt_description_parts:
         logger.info(f"ℹ️ [RECLAMO_IMG_PROC] No hay suficiente información visual/textual para enviar al LLM (Análisis ID: {analisis_id_for_log}).")
         if analisis_db_record:
             analisis_db_record.estado_analisis = "completado_sin_info_suficiente"
             db.session.commit()

         datos_internos_analisis['vision_inferred_category'] = sugerida_categoria_vision
         return {
             'es_reclamo': bool(sugerida_categoria_vision),
             'categoria_sugerida': sugerida_categoria_vision,
             'descripcion_sugerida': "No se pudo generar una descripción automática. Por favor, describí el problema.",
             'texto_ocr': extracted_ocr_text,
             'analisis_id': analisis_db_record.id if analisis_db_record else None, 'error': None,
             'analisis_interno': datos_internos_analisis
         }

    imagen_descripcion_para_llm = ". ".join(prompt_description_parts) + "."
    logger.info(f"📝 [RECLAMO_IMG_PROC] Descripción para LLM (desde imagen): {imagen_descripcion_para_llm} (Análisis ID: {analisis_id_for_log})")

    # Use LLM to refine/generate details based on image description
    detalles_llm = extract_complaint_details_llm(imagen_descripcion_para_llm)

    datos_internos_analisis['llm_complaint_extraction_from_image'] = detalles_llm
    datos_internos_analisis['vision_inferred_category'] = sugerida_categoria_vision

    final_categoria_sugerida = sugerida_categoria_vision

    llm_tipo_problema = detalles_llm.get("tipo_problema","").strip()
    if llm_tipo_problema:
        normalized_llm_cat = normalizar_texto_municipios(llm_tipo_problema)
        matched_llm_cat = next((cat for cat in CATEGORIAS_RECLAMO if normalizar_texto_municipios(cat) == normalized_llm_cat), None)
        if not matched_llm_cat:
            from services.herramientas_municipio import categorias_normalizadas as reclamo_categorias_norm_hm
            from difflib import get_close_matches as get_close_matches_hm

            close_matches_llm = get_close_matches_hm(normalized_llm_cat, reclamo_categorias_norm_hm, n=1, cutoff=0.75)
            if close_matches_llm:
                idx = reclamo_categorias_norm_hm.index(close_matches_llm[0])
                matched_llm_cat = CATEGORIAS_RECLAMO[idx]

        if matched_llm_cat and matched_llm_cat != "otro motivo":
            final_categoria_sugerida = matched_llm_cat
            logger.info(f"[RECLAMO_IMG_PROC] LLM propuso categoría: '{llm_tipo_problema}', mapeada a: '{final_categoria_sugerida}'")
        elif not final_categoria_sugerida and matched_llm_cat == "otro motivo":
            final_categoria_sugerida = "otro motivo"

    final_descripcion_sugerida = detalles_llm.get("descripcion_problema", "").strip()
    if not final_descripcion_sugerida or len(final_descripcion_sugerida) < 15:
        desc_parts = []
        if final_categoria_sugerida and final_categoria_sugerida != "otro motivo":
            desc_parts.append(f"Posible problema de '{final_categoria_sugerida}'.")

        if top_objects_str: desc_parts.append(f"Se observan: {top_objects_str}.")
        elif top_labels_str: desc_parts.append(f"Aspectos generales: {top_labels_str}.")

        if ocr_snippet_for_prompt:
            desc_parts.append(f"Texto en imagen: '{ocr_snippet_for_prompt}'.")

        if desc_parts:
            final_descripcion_sugerida = " ".join(desc_parts)
            logger.info(f"[RECLAMO_IMG_PROC] Descripción generada por fallback: {final_descripcion_sugerida}")
        else:
            final_descripcion_sugerida = "Por favor, describe el problema que observaste en la imagen."

    es_reclamo_valido_sugerido = bool(final_categoria_sugerida and final_categoria_sugerida != "otro motivo") or \
                                 (final_descripcion_sugerida and len(final_descripcion_sugerida) >= 15 and "describe el problema" not in final_descripcion_sugerida.lower())

    datos_internos_analisis['final_categoria_sugerida'] = final_categoria_sugerida
    datos_internos_analisis['final_descripcion_sugerida'] = final_descripcion_sugerida
    datos_internos_analisis['es_reclamo_sugerido'] = es_reclamo_valido_sugerido

    if analisis_db_record:
        analisis_db_record.estado_analisis = "completado"
        current_datos_db = analisis_db_record.datos_estructurados if isinstance(analisis_db_record.datos_estructurados, dict) else {}
        if 'vision_api_raw' not in datos_internos_analisis and 'vision_api_raw' in current_datos_db:
            datos_internos_analisis['vision_api_raw'] = current_datos_db['vision_api_raw']

        current_datos_db.update(datos_internos_analisis)
        analisis_db_record.datos_estructurados = current_datos_db
        db.session.commit()

    return {
        'es_reclamo': es_reclamo_valido_sugerido,
        'categoria_sugerida': final_categoria_sugerida if final_categoria_sugerida else None,
        'descripcion_sugerida': final_descripcion_sugerida if len(final_descripcion_sugerida) >=10 else None,
        'texto_ocr': extracted_ocr_text,
        'analisis_id': analisis_db_record.id if analisis_db_record else None,
        'error': None,
        'analisis_interno': datos_internos_analisis
    }

# --- Lógica para Interpretación de Pedidos PYME ---
def _procesar_interpretacion_pedido_pyme(
    analisis_db_record: Optional[AnalisisArchivo], # Puede ser None
    vision_results: Dict[str, Any],
    extracted_ocr_text: str,
    pyme_user: User
) -> Dict[str, Any]:
    """Lógica específica para interpretar una imagen como un pedido para una PYME."""
    analisis_id_for_log = analisis_db_record.id if analisis_db_record else "N/A (WhatsApp)"
    logger.info(f"⚙️ Procesando como PEDIDO PYME para Análisis ID: {analisis_id_for_log}, PYME ID: {pyme_user.id}")

    datos_internos_analisis = {'tipo_analisis_sugerido': 'pedido_pyme_vision_ocr_v1'}
    if analisis_db_record:
        analisis_db_record.tipo_analisis = 'pedido_pyme_vision_ocr_v1'

    items_pedido_detectados = []
    items_no_encontrados_catalogo = []
    resumen_ocr = ""

    if not extracted_ocr_text:
        logger.info(f"ℹ️ [PEDIDO] No se detectó texto OCR en la imagen para Análisis ID: {analisis_id_for_log}.")
        if analisis_db_record:
            analisis_db_record.estado_analisis = "completado"
            analisis_db_record.tipo_analisis = 'imagen_general_vision_v1' # No hay texto, no puede ser pedido
            # Guardar datos internos aunque esté vacío el OCR
            current_datos_db = analisis_db_record.datos_estructurados if isinstance(analisis_db_record.datos_estructurados, dict) else {}
            current_datos_db.update(datos_internos_analisis) # tipo_analisis_sugerido
            analisis_db_record.datos_estructurados = current_datos_db
            db.session.commit()

        datos_internos_analisis.update({ # También para el retorno si es WhatsApp
            'items_parseados_ocr': [], 'items_encontrados_catalogo': [],
            'items_no_encontrados_catalogo': [], 'resumen_ocr_completo': None,
        })
        return {
            'es_pedido': False,
            'motivo': 'No se detectó texto en la imagen que pueda interpretarse como un pedido.',
            'items_detectados': [],
            'items_no_encontrados': [],
            'resumen_ocr': None,
            'analisis_id': analisis_db_record.id if analisis_db_record else None,
            'error': None,
            'analisis_interno': datos_internos_analisis
        }

    resumen_ocr = extracted_ocr_text # Guardar el texto completo para referencia

    # Heurística simple para parsear el texto del pedido:
    # Asumimos que cada línea puede ser un ítem.
    # Buscamos patrones como "texto [separador] cantidad" o "cantidad [separador] texto"
    # Esto es muy básico y puede mejorarse mucho (ej. con regex más robustos o LLM).

    # --- Nueva lógica: Usar LLM para extraer items del OCR ---
    from services.llm_utils import extraer_lista_pedido_de_texto_con_llm # Local import

    posibles_items_texto = extraer_lista_pedido_de_texto_con_llm(extracted_ocr_text, pyme_user.id if pyme_user else None)

    if not posibles_items_texto:
        # Fallback a la lógica regex si el LLM no devuelve nada o si se prefiere un intento regex primero.
        # Por ahora, si LLM no devuelve nada, consideramos que no hay items parseables.
        # Podríamos re-introducir el regex aquí como un segundo intento si el LLM falla.
        # Ejemplo de re-introducción de regex (comentado por ahora):
        # logger.info(f"[PEDIDO] LLM no extrajo items. Intentando con Regex. OCR: {extracted_ocr_text[:100]}")
        # lineas = extracted_ocr_text.split('\n')
        # for i, linea_raw in enumerate(lineas):
        #     linea = limpiar_texto_base(linea_raw)
        #     if not linea: continue
        #     match_cantidad_al_final = re.search(r"^(.*?)(?:[\sxX*])?\s*(\d+[\.,]?\d*)\s*$", linea)
        #     match_cantidad_al_principio = re.search(r"^\s*(\d+[\.,]?\d*)\s*(?:[\sxX*])?\s*(.+)", linea)
        #     nombre_producto_ocr = None; cantidad_ocr_str = None
        #     if match_cantidad_al_final:
        #         nombre_producto_ocr = limpiar_texto_base(match_cantidad_al_final.group(1))
        #         cantidad_ocr_str = match_cantidad_al_final.group(2).replace(',', '.')
        #     elif match_cantidad_al_principio:
        #         cantidad_ocr_str = match_cantidad_al_principio.group(1).replace(',', '.')
        #         nombre_producto_ocr = limpiar_texto_base(match_cantidad_al_principio.group(2))
        #     else: continue
        #     if nombre_producto_ocr and cantidad_ocr_str:
        #         try:
        #             cantidad_float = float(cantidad_ocr_str); cantidad_int = int(round(cantidad_float))
        #             if cantidad_int <= 0: continue
        #             posibles_items_texto.append({
        #                 "nombre_ocr": nombre_producto_ocr, "cantidad_ocr": cantidad_int,
        #                 "linea_original_ocr": linea_raw, "linea_idx_ocr": i
        #             })
        #         except ValueError: pass
        logger.info(f"ℹ️ [PEDIDO] Ni LLM ni Regex (si estuviera activo) produjeron items parseables. Texto OCR: {extracted_ocr_text[:200]} (Análisis ID: {analisis_id_for_log})")

        datos_internos_analisis.update({
            'items_parseados_ocr': [], 'items_encontrados_catalogo': [],
            'items_no_encontrados_catalogo': [], 'resumen_ocr_completo': resumen_ocr,
        })

        if analisis_db_record:
            current_datos_db = analisis_db_record.datos_estructurados if isinstance(analisis_db_record.datos_estructurados, dict) else {}
            current_datos_db.update(datos_internos_analisis) # tipo_analisis_sugerido y los de arriba
            analisis_db_record.datos_estructurados = current_datos_db
            analisis_db_record.estado_analisis = "completado"
            db.session.commit()

        return {
            'es_pedido': False, # No se pudieron parsear items
            'motivo': 'El texto de la imagen no pudo ser interpretado como una lista de productos y cantidades.',
            'items_detectados': [],
            'items_no_encontrados': [],
            'resumen_ocr': resumen_ocr,
            'analisis_id': analisis_db_record.id if analisis_db_record else None,
            'error': None,
            'analisis_interno': datos_internos_analisis
        }

    logger.info(f"📝 [PEDIDO] Items parseados del OCR: {posibles_items_texto} (Análisis ID: {analisis_id_for_log})")

    # Buscar cada item parseado en el catálogo de la PYME
    for item_ocr in posibles_items_texto:
        nombre_norm_ocr = item_ocr["nombre_ocr"].lower()
        cantidad_pedido = item_ocr["cantidad_ocr"]

        # Lógica de búsqueda similar a procesar_pedido_excel (idealmente refactorizada y reutilizada)
        item_catalogo_encontrado = None
        # Opción 1: Buscar por SKU si el nombre OCR parece un SKU (ej. solo números y letras, corto)
        # (Esta heurística de SKU es muy simple, podría mejorarse)
        if re.match(r"^[A-Za-z0-9-]{3,15}$", item_ocr["nombre_ocr"]): # Si parece un SKU
            item_catalogo_encontrado = CatalogoItem.query.filter_by(user_id=pyme_user.id, sku=item_ocr["nombre_ocr"]).first()

        if not item_catalogo_encontrado:
            candidatos = CatalogoItem.query.filter(
                CatalogoItem.user_id == pyme_user.id,
                CatalogoItem.nombre.ilike(f"%{nombre_norm_ocr}%") # Búsqueda case-insensitive
            ).limit(5).all()

            if not candidatos and len(nombre_norm_ocr.split()) > 1:
                primera_palabra = nombre_norm_ocr.split()[0]
                if len(primera_palabra) > 2: # Un poco más permisivo para OCR
                    candidatos = CatalogoItem.query.filter(
                        CatalogoItem.user_id == pyme_user.id,
                        CatalogoItem.nombre.ilike(f"%{primera_palabra}%")
                    ).limit(5).all()

            if candidatos:
                mejor_candidato = None
                max_sim = -1.0
                for candidato in candidatos:
                    sim = calcular_similitud_levenshtein(nombre_norm_ocr, candidato.nombre.lower())
                    if sim > max_sim:
                        max_sim = sim
                        mejor_candidato = candidato

                if mejor_candidato and max_sim >= UMBRAL_SIMILITUD_PRODUCTO_PEDIDO - 0.05: # Un poco más flexible para OCR
                    item_catalogo_encontrado = mejor_candidato

        if item_catalogo_encontrado:
            precio_str, precio_float, moneda = parse_precio_flexible(item_catalogo_encontrado.precio)
            if precio_float is None:
                logger.warning(f"[PEDIDO] Producto '{item_catalogo_encontrado.nombre}' (ID: {item_catalogo_encontrado.id}) encontrado pero sin precio válido ('{item_catalogo_encontrado.precio}'). OCR: '{item_ocr['nombre_ocr']}'")
                items_no_encontrados_catalogo.append({
                    "nombre_ocr": item_ocr["nombre_ocr"],
                    "cantidad_ocr": cantidad_pedido,
                    "linea_ocr": item_ocr["linea_original_ocr"],
                    "razon": f"Producto '{item_catalogo_encontrado.nombre}' encontrado pero sin precio válido."
                })
                continue

            items_pedido_detectados.append({
                "catalogo_item_id": item_catalogo_encontrado.id,
                "nombre_producto_ocr": item_ocr["nombre_ocr"],
                "nombre_producto_catalogo": item_catalogo_encontrado.nombre,
                "sku_catalogo": item_catalogo_encontrado.sku,
                "cantidad_pedido": cantidad_pedido,
                "precio_unitario_catalogo": precio_float,
                "moneda_catalogo": moneda,
                "subtotal_calculado": round(cantidad_pedido * precio_float, 2),
                "linea_original_ocr": item_ocr["linea_original_ocr"]
            })
            logger.info(f"✅ [PEDIDO] OCR item '{item_ocr['nombre_ocr']}' -> Catálogo ID {item_catalogo_encontrado.id} ('{item_catalogo_encontrado.nombre}') x {cantidad_pedido}")
        else:
            logger.info(f"❌ [PEDIDO] OCR item '{item_ocr['nombre_ocr']}' no encontrado en catálogo de PYME {pyme_user.id}.")
            items_no_encontrados_catalogo.append({
                "nombre_ocr": item_ocr["nombre_ocr"],
                "cantidad_ocr": cantidad_pedido,
                "linea_ocr": item_ocr["linea_original_ocr"],
                "razon": "Producto no encontrado en el catálogo."
            })

    # Guardar resultados en datos_internos_analisis (para retorno si es WhatsApp)
    # y en analisis_db_record.datos_estructurados si existe.
    datos_internos_analisis.update({
        'items_parseados_ocr': posibles_items_texto,
        'items_encontrados_catalogo': items_pedido_detectados,
        'items_no_encontrados_catalogo': items_no_encontrados_catalogo,
        'resumen_ocr_completo': resumen_ocr,
    })

    if analisis_db_record:
        current_datos_db = analisis_db_record.datos_estructurados if isinstance(analisis_db_record.datos_estructurados, dict) else {}
        current_datos_db.update(datos_internos_analisis) # tipo_analisis_sugerido y los de arriba
        analisis_db_record.datos_estructurados = current_datos_db
        analisis_db_record.estado_analisis = "completado"
        db.session.commit()

    if not items_pedido_detectados and not items_no_encontrados_catalogo:
         motivo_final = 'El texto de la imagen no parece corresponder a productos de nuestro catálogo.'
    elif not items_pedido_detectados and items_no_encontrados_catalogo:
        motivo_final = 'Algunos productos mencionados en la imagen no se encontraron en el catálogo o no tienen precio.'
    else:
        motivo_final = 'Pedido parcialmente interpretado desde la imagen.'


    return {
        'es_pedido': bool(items_pedido_detectados), # Es pedido si al menos un item se pudo matchear y tiene precio
        'motivo': motivo_final if not items_pedido_detectados else "Pedido interpretado desde la imagen.",
        'items_detectados': items_pedido_detectados,
        'items_no_encontrados_catalogo': items_no_encontrados_catalogo,
        'resumen_ocr': resumen_ocr,
        'analisis_id': analisis_db_record.id if analisis_db_record else None,
        'error': None,
        'analisis_interno': datos_internos_analisis
    }

def _procesar_interpretacion_orden_de_compra(
    analisis_db_record: Optional[AnalisisArchivo],
    vision_results: Dict[str, Any],
    extracted_ocr_text: str
) -> Dict[str, Any]:
    """Lógica específica para interpretar una imagen como una orden de compra."""
    from services.purchase_order_processor import extraer_datos_orden_de_compra_con_llm

    analisis_id_for_log = analisis_db_record.id if analisis_db_record else "N/A (WhatsApp)"
    logger.info(f"⚙️ Procesando como ORDEN DE COMPRA para Análisis ID: {analisis_id_for_log}")

    datos_internos_analisis = {'tipo_analisis_sugerido': 'orden_de_compra_vision_llm_v1'}
    if analisis_db_record:
        analisis_db_record.tipo_analisis = 'orden_de_compra_vision_llm_v1'

    if not extracted_ocr_text:
        logger.info(f"ℹ️ [OC] No se detectó texto OCR en la imagen para Análisis ID: {analisis_id_for_log}.")
        if analisis_db_record:
            analisis_db_record.estado_analisis = "completado"
            analisis_db_record.tipo_analisis = 'imagen_general_vision_v1'
            db.session.commit()
        return {
            'es_orden_de_compra': False,
            'motivo': 'No se detectó texto en la imagen.',
            'datos_orden': {},
            'analisis_id': analisis_db_record.id if analisis_db_record else None,
            'error': None,
            'analisis_interno': datos_internos_analisis
        }

    datos_oc = extraer_datos_orden_de_compra_con_llm(extracted_ocr_text)

    if not datos_oc:
        logger.warning(f"No se pudieron extraer datos de la orden de compra desde el texto OCR para Análisis ID: {analisis_id_for_log}")
        if analisis_db_record:
            analisis_db_record.estado_analisis = "completado"
            analisis_db_record.tipo_analisis = 'orden_de_compra_fallido_llm'
            db.session.commit()
        return {
            'es_orden_de_compra': False,
            'motivo': 'No se pudieron extraer datos estructurados de la orden de compra.',
            'datos_orden': {},
            'analisis_id': analisis_db_record.id if analisis_db_record else None,
            'error': None,
            'analisis_interno': datos_internos_analisis
        }

    if analisis_db_record:
        current_datos_db = analisis_db_record.datos_estructurados if isinstance(analisis_db_record.datos_estructurados, dict) else {}
        current_datos_db.update(datos_oc)
        analisis_db_record.datos_estructurados = current_datos_db
        analisis_db_record.estado_analisis = "completado"
        db.session.commit()

    return {
        'es_orden_de_compra': True,
        'datos_orden': datos_oc,
        'analisis_id': analisis_db_record.id if analisis_db_record else None,
        'error': None,
        'analisis_interno': datos_internos_analisis
    }


if __name__ == '__main__':
    # --- Bloque de prueba local ---
    # Esto requiere una app Flask y un contexto de base de datos para funcionar completamente.
    # Simulación básica:
    logging.basicConfig(level=logging.INFO)
    logger.info("Ejecutando pruebas locales de interpretacion_imagen_service.py...")

    # Crear un objeto ArchivoAdjunto simulado (normalmente vendría de la DB)
    class MockArchivoAdjunto:
        def __init__(self, id, url, analisis_existente=None):
            self.id = id
            self.url = url
            self._analisis_existente = analisis_existente # Para simular uno ya creado

    class MockAnalisisArchivo:
        def __init__(self, archivo_adjunto_id):
            self.id = random.randint(1000,2000)
            self.archivo_adjunto_id = archivo_adjunto_id
            self.estado_analisis = "pendiente"
            self.tipo_analisis = None
            self.datos_estructurados = {}
            self.texto_extraido = None
            self.error_analisis = None

    # Simular la base de datos y sesión
    class MockDbSession:
        def add(self, instance):
            logger.info(f"[MOCK_DB] add: {instance}")
        def commit(self):
            logger.info("[MOCK_DB] commit")
        def query(self, model): # Simular query
            class MockQuery:
                def filter_by(self, **kwargs):
                    logger.info(f"[MOCK_DB] filter_by: {kwargs}")
                    # Para la prueba, si se busca analisis para el archivo_id=1, devolver uno mock
                    if model == AnalisisArchivo and kwargs.get('archivo_adjunto_id') == 1:
                        # Devolver el análisis existente si se pasó al mock de ArchivoAdjunto
                        if hasattr(archivo_prueba_reclamo, '_analisis_existente') and archivo_prueba_reclamo._analisis_existente:
                            return self
                        return self # Devolver la query para poder llamar a first()
                    if model == CatalogoItem: # Simular query de catálogo
                        logger.info(f"[MOCK_DB] Query CatalogoItem con filtro: {kwargs}")
                        # Aquí podríamos devolver una lista mock de CatalogoItems si es necesario para la prueba de pedidos
                        # Por ahora, devolvemos la query para que se pueda llamar a limit().all() y devuelva vacío o mock.
                        # Esto necesitaría más elaboración para una prueba completa de _procesar_interpretacion_pedido_pyme
                        return self
                    return self
                def first(self):
                    logger.info("[MOCK_DB] first()")
                    if hasattr(archivo_prueba_reclamo, '_analisis_existente') and archivo_prueba_reclamo._analisis_existente: # Adaptar para el mock correcto
                        return archivo_prueba_reclamo._analisis_existente
                    # Simular que no existe un análisis previo si no se mockeó explícitamente
                    return None
                def limit(self, num): # Para las queries de catálogo
                    logger.info(f"[MOCK_DB] limit({num})")
                    return self
                def all(self): # Para las queries de catálogo
                    logger.info(f"[MOCK_DB] all()")
                    # Devolver una lista vacía para simular que no se encuentran candidatos,
                    # o un mock de catalogo items si se quiere probar el matching.
                    # Ejemplo mock:
                    # return [MockCatalogoItem(id=100, nombre="Producto Mock A", sku="SKU00A", precio="100.00"),
                    #         MockCatalogoItem(id=101, nombre="Otro Producto Mock B", sku="SKU00B", precio="25.50")]
                    return []


            return MockQuery()

    # Reemplazar db.session con el mock para la prueba
    original_db_session = None
    if 'db' in globals() and hasattr(db, 'session'):
        original_db_session = db.session

    class MockDBGlobal:
        session = MockDbSession()

    if not hasattr(globals(), 'db'):
        import sys
        db_module_mock = type(sys)('db_mock')
        db_module_mock.session = MockDbSession()
        db = db_module_mock
        logger.warning("Se creó un mock global 'db' para ejecución standalone. Esto no es para producción.")

    # --- Prueba para Reclamo Municipal ---
    # URL_IMAGEN_PRUEBA_RECLAMO = "https://upload.wikimedia.org/wikipedia/commons/thumb/2/25/Pothole_in_need_of_repair.JPG/640px-Pothole_in_need_of_repair.JPG" # Bache
    URL_IMAGEN_PRUEBA_RECLAMO_NO_RECLAMO = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a3/Eq_it-na_pizza-margherita_sep2005_sml.jpg/640px-Eq_it-na_pizza-margherita_sep2005_sml.jpg" # Pizza

    archivo_prueba_reclamo = MockArchivoAdjunto(id=1, url=URL_IMAGEN_PRUEBA_RECLAMO_NO_RECLAMO)
    logger.info(f"\n--- Probando RECLAMO MUNICIPAL con URL: {archivo_prueba_reclamo.url} ---")

    if not VISION_CLIENT: # Asumiendo que vision viene de google_vision_service
         logger.error("El cliente de Google Vision no está inicializado en google_vision_service.py. La prueba fallará o usará mocks.")
    else:
        resultado_interpretacion_reclamo = interpretar_imagen_para_chat(
            archivo_adjunto=archivo_prueba_reclamo,
            tipo_interpretacion="reclamo_municipal"
        )
        logger.info("\n--- Resultado de la Interpretación de RECLAMO ---")
        import json as json_parser
        logger.info(json_parser.dumps(resultado_interpretacion_reclamo, indent=2, ensure_ascii=False))

    # --- Prueba para Pedido PYME ---
    # Necesitaríamos una imagen con texto de un pedido, ej: "2 Coca Cola\n1 Papas Fritas Grandes"
    # URL_IMAGEN_PRUEBA_PEDIDO = "URL_A_UNA_IMAGEN_DE_PEDIDO_CON_TEXTO"
    # logger.info(f"\n--- Probando PEDIDO PYME con URL: {URL_IMAGEN_PRUEBA_PEDIDO} ---")
    # mock_pyme_user = User(id=99, nombre_empresa="Pyme de Prueba") # Crear un User mock

    # archivo_prueba_pedido = MockArchivoAdjunto(id=2, url=URL_IMAGEN_PRUEBA_PEDIDO)
    # if not VISION_CLIENT:
    #      logger.error("El cliente de Google Vision no está inicializado. Prueba de pedido PYME incompleta.")
    # else:
    #     resultado_interpretacion_pedido = interpretar_imagen_para_chat(
    #         archivo_adjunto=archivo_prueba_pedido,
    #         tipo_interpretacion="pedido_pyme",
    #         pyme_user=mock_pyme_user
    #     )
    #     logger.info("\n--- Resultado de la Interpretación de PEDIDO PYME ---")
    #     logger.info(json_parser.dumps(resultado_interpretacion_pedido, indent=2, ensure_ascii=False))


    logger.info("\n--- Fin de la Prueba Local ---")

    if original_db_session:
        db.session = original_db_session
    # Esto requiere una app Flask y un contexto de base de datos para funcionar completamente.
    # Simulación básica:
    logging.basicConfig(level=logging.INFO)
    logger.info("Ejecutando pruebas locales de interpretacion_imagen_service.py...")

    # Crear un objeto ArchivoAdjunto simulado (normalmente vendría de la DB)
    class MockArchivoAdjunto:
        def __init__(self, id, url, analisis_existente=None):
            self.id = id
            self.url = url
            self._analisis_existente = analisis_existente # Para simular uno ya creado

    class MockAnalisisArchivo:
        def __init__(self, archivo_adjunto_id):
            self.id = random.randint(1000,2000)
            self.archivo_adjunto_id = archivo_adjunto_id
            self.estado_analisis = "pendiente"
            self.tipo_analisis = None
            self.datos_estructurados = {}
            self.texto_extraido = None
            self.error_analisis = None

    # Simular la base de datos y sesión
    class MockDbSession:
        def add(self, instance):
            logger.info(f"[MOCK_DB] add: {instance}")
        def commit(self):
            logger.info("[MOCK_DB] commit")
        def query(self, model): # Simular query
            class MockQuery:
                def filter_by(self, **kwargs):
                    logger.info(f"[MOCK_DB] filter_by: {kwargs}")
                    # Para la prueba, si se busca analisis para el archivo_id=1, devolver uno mock
                    if model == AnalisisArchivo and kwargs.get('archivo_adjunto_id') == 1:
                        # Devolver el análisis existente si se pasó al mock de ArchivoAdjunto
                        if hasattr(archivo_prueba, '_analisis_existente') and archivo_prueba._analisis_existente:
                            return self
                        return self # Devolver la query para poder llamar a first()
                    return self
                def first(self):
                    logger.info("[MOCK_DB] first()")
                    # Devolver el análisis existente si se pasó al mock de ArchivoAdjunto
                    if hasattr(archivo_prueba, '_analisis_existente') and archivo_prueba._analisis_existente:
                        return archivo_prueba._analisis_existente
                    return None # Simular que no existe un análisis previo
            return MockQuery()

    # Reemplazar db.session con el mock para la prueba
    # Esto es una simplificación. En un test real usarías pytest y mocks de unittest.mock
    original_db_session = None
    if 'db' in globals() and hasattr(db, 'session'):
        original_db_session = db.session

    # Para que la prueba se ejecute, necesitamos simular 'db' si no está en el contexto global
    # (por ejemplo, si se ejecuta este archivo directamente sin la app Flask completa)
    class MockDBGlobal:
        session = MockDbSession()

    # Aquí asignamos el mock a db.session. Cuidado si 'db' no está definido.
    # En un entorno de prueba real, esto se manejaría de forma más limpia.
    # Por ahora, asumimos que 'db' podría no estar completamente inicializado si se corre standalone.
    # Lo ideal sería tener un contexto de aplicación Flask para esto.

    # URL de una imagen de prueba (ej: un semáforo, un bache)
    # ¡DEBES CAMBIAR ESTA URL POR UNA IMAGEN REAL ACCESIBLE PÚBLICAMENTE PARA PROBAR!
    # Ejemplo: imagen de un semáforo de Wikipedia Commons
    # URL_IMAGEN_PRUEBA = "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Traffic_lights_in_Poland_-_Cykl_A_-_Krak%C3%B3w_2.jpg/640px-Traffic_lights_in_Poland_-_Cykl_A_-_Krak%C3%B3w_2.jpg"
    # URL_IMAGEN_PRUEBA_BACHE = "https://upload.wikimedia.org/wikipedia/commons/thumb/2/25/Pothole_in_need_of_repair.JPG/640px-Pothole_in_need_of_repair.JPG"
    URL_IMAGEN_PRUEBA_NO_RECLAMO = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a3/Eq_it-na_pizza-margherita_sep2005_sml.jpg/640px-Eq_it-na_pizza-margherita_sep2005_sml.jpg" # Pizza

    if not hasattr(globals(), 'db'): # Si db no está en el scope global (ej. corriendo standalone)
        import sys
        # Crear un mock simple para db
        db_module_mock = type(sys)('db_mock')
        db_module_mock.session = MockDbSession()
        db = db_module_mock # Asignar el mock a una variable 'db' global
        # Esto es muy hacky, solo para que el script no falle al ejecutarse directamente.
        # No es una buena práctica para tests reales.
        logger.warning("Se creó un mock global 'db' para ejecución standalone. Esto no es para producción.")


    archivo_prueba = MockArchivoAdjunto(id=1, url=URL_IMAGEN_PRUEBA_NO_RECLAMO)
    # Para simular que ya existe un AnalisisArchivo:
    # analisis_existente_mock = MockAnalisisArchivo(archivo_adjunto_id=1)
    # archivo_prueba_con_analisis = MockArchivoAdjunto(id=1, url=URL_IMAGEN_PRUEBA_BACHE, analisis_existente=analisis_existente_mock)


    logger.info(f"Probando con URL: {archivo_prueba.url}")

    # Necesitamos que services.google_vision_service.VISION_CLIENT esté inicializado
    # Si se ejecuta este archivo directamente, google_vision_service se importa y su inicialización se ejecuta.
    # Asegurarse de que las credenciales de Vision estén configuradas.
    if not vision.VISION_CLIENT: # Asumiendo que vision viene de google_vision_service
         logger.error("El cliente de Google Vision no está inicializado en google_vision_service.py. La prueba fallará o usará mocks.")
         # Podríamos mockear analyze_image_from_content aquí si es necesario para un test aislado.

    # La función interpretar_imagen_reclamo ya no existe.
    # Se podría llamar a interpretar_imagen_para_chat con tipo_interpretacion="reclamo_municipal".
    # Ejemplo:
    # resultado_interpretacion = interpretar_imagen_para_chat(
    #    archivo_adjunto=archivo_prueba,
    #    tipo_interpretacion="reclamo_municipal"
    # )
    # Por ahora, comentaremos la llamada original para evitar errores.
    # resultado_interpretacion = interpretar_imagen_reclamo(archivo_prueba) # Esta función no existe
    resultado_interpretacion = {"mensaje": "Llamada a interpretar_imagen_reclamo comentada ya que la función no existe. Adaptar a interpretar_imagen_para_chat si es necesario para pruebas."}


    logger.info("\n--- Resultado de la Interpretación ---")
    import json as json_parser # para evitar conflicto con el modulo json de credenciales
    logger.info(json_parser.dumps(resultado_interpretacion, indent=2, ensure_ascii=False))
    logger.info("--- Fin de la Prueba Local ---")

    # Restaurar db.session si lo habíamos mockeado y existía antes
    if original_db_session:
        db.session = original_db_session
