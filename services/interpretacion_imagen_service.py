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
    """
    if not archivo_adjunto or not archivo_adjunto.url:
        return {'error': 'Archivo adjunto o URL no válidos.', 'analisis_id': None}

    if tipo_interpretacion == "pedido_pyme" and not pyme_user:
        logger.error("❌ Se requiere pyme_user para interpretar un pedido.")
        return {'error': 'Usuario PYME no especificado para interpretación de pedido.', 'analisis_id': None}

    # Determinar tipo de análisis inicial para el registro en DB
    tipo_analisis_db = f'{tipo_interpretacion}_vision_v1' # Ej: reclamo_municipal_vision_v1

    analisis = _inicializar_analisis_archivo(archivo_adjunto.id, tipo_analisis_db)

    logger.info(f"➡️ Iniciando interpretación '{tipo_interpretacion}' para imagen: Archivo ID {archivo_adjunto.id}, URL: {archivo_adjunto.url}")

    image_content = _descargar_imagen(archivo_adjunto.url)
    if not image_content:
        analisis.estado_analisis = "error"
        analisis.error_analisis = "Fallo al descargar la imagen."
        db.session.commit()
        return {'error': analisis.error_analisis, 'analisis_id': analisis.id}

    logger.info(f"🖼️  Enviando imagen (tamaño: {len(image_content)} bytes) a Vision API...")
    vision_results = analyze_image_from_content(image_content)
    current_datos = analisis.datos_estructurados or {}
    current_datos['vision_api_raw'] = vision_results
    analisis.datos_estructurados = current_datos

    if vision_results.get("error"):
        logger.error(f"❌ Error de Vision API: {vision_results['error']}")
        analisis.estado_analisis = "error"
        analisis.error_analisis = f"Error de Vision API: {vision_results['error']}"
        db.session.commit()
        return {'error': analisis.error_analisis, 'analisis_id': analisis.id}

    extracted_ocr_text = ""
    if vision_results.get("full_text_annotation"):
        extracted_ocr_text = vision_results["full_text_annotation"].get("description", "").strip()
        analisis.texto_extraido = extracted_ocr_text
        logger.info(f" टेक्स्ट OCR detectado: '{extracted_ocr_text[:200]}...'")

    # Guardar el análisis con el texto OCR y los resultados de Vision antes de la lógica específica
    db.session.commit()


    if tipo_interpretacion == "reclamo_municipal":
        return _procesar_interpretacion_reclamo(analisis, vision_results, extracted_ocr_text)
    elif tipo_interpretacion == "pedido_pyme":
        if not pyme_user: # Doble chequeo, aunque ya se hizo arriba.
            logger.error("❌ Error interno: pyme_user es None para pedido_pyme en _procesar.")
            analisis.estado_analisis = "error"
            analisis.error_analisis = "Error interno: Usuario PYME no disponible."
            db.session.commit()
            return {'error': analisis.error_analisis, 'analisis_id': analisis.id}
        return _procesar_interpretacion_pedido_pyme(analisis, vision_results, extracted_ocr_text, pyme_user)
    else:
        logger.error(f"❌ Tipo de interpretación '{tipo_interpretacion}' no soportado.")
        analisis.estado_analisis = "error"
        analisis.error_analisis = f"Tipo de interpretación no soportado: {tipo_interpretacion}"
        db.session.commit()
        return {'error': analisis.error_analisis, 'analisis_id': analisis.id}


def _procesar_interpretacion_reclamo(
    analisis: AnalisisArchivo,
    vision_results: Dict[str, Any],
    extracted_ocr_text: str
) -> Dict[str, Any]:
    """Lógica específica para interpretar un reclamo municipal."""
    logger.info(f"⚙️ Procesando como RECLAMO MUNICIPAL para Análisis ID: {analisis.id}")
    analisis.tipo_analisis = 'reclamo_vision_llm_v1' # Actualizar si es necesario

    found_keywords = []
    # Primero, revisar objetos detectados
    for obj in vision_results.get("objects", []):
        obj_name_lower = obj.get("name", "").lower()
        if obj_name_lower in PALABRAS_CLAVE_RECLAMO_OBJETOS:
            found_keywords.append(f"objeto:{obj_name_lower} (conf: {obj.get('confidence', 0):.2f})")

    # Luego, revisar etiquetas
    for label in vision_results.get("labels", []):
        label_desc_lower = label.get("description", "").lower()
        if label_desc_lower in PALABRAS_CLAVE_RECLAMO_ETIQUETAS:
            found_keywords.append(f"etiqueta:{label_desc_lower} (conf: {label.get('confidence', 0):.2f})")

    # Guardar keywords encontradas en el análisis
    current_datos_estructurados = analisis.datos_estructurados if isinstance(analisis.datos_estructurados, dict) else {}
    current_datos_estructurados['keywords_detectadas_reclamo'] = found_keywords # Específico para reclamo
    analisis.datos_estructurados = current_datos_estructurados

    if not found_keywords and not extracted_ocr_text:
        logger.info(f"ℹ️ [RECLAMO] No se encontraron palabras clave relevantes ni texto OCR en la imagen para Análisis ID: {analisis.id}.")
        analisis.estado_analisis = "completado"
        analisis.tipo_analisis = 'imagen_general_vision_v1'
        db.session.commit()
        return {'es_reclamo': False, 'motivo': 'No se detectaron elementos visuales o textuales de reclamo claros.', 'vision_results': vision_results, 'analisis_id': analisis.id}

    logger.info(f"🔑 [RECLAMO] Palabras clave/elementos detectados: {found_keywords} para Análisis ID: {analisis.id}")

    prompt_description_parts = []
    if vision_results.get("objects"):
        prompt_description_parts.append("Objetos detectados: " + ", ".join([f"{o['name']}" for o in vision_results["objects"][:5]]))
    if vision_results.get("labels"):
        prompt_description_parts.append("Etiquetas generales: " + ", ".join([f"{l['description']}" for l in vision_results["labels"][:5]]))
    if extracted_ocr_text:
        prompt_description_parts.append(f"Texto extraído de la imagen: '{extracted_ocr_text[:300]}'") # Aumentar un poco el límite para el prompt

    if not prompt_description_parts:
         logger.info(f"ℹ️ [RECLAMO] No hay suficiente información visual o textual para enviar al LLM para Análisis ID: {analisis.id}.")
         analisis.estado_analisis = "completado"
         # Si no hay keywords pero sí OCR, podría no ser 'imagen_general' aún.
         # Se decide más adelante si el LLM tampoco lo ve.
         # Por ahora, si no hay nada para el prompt, y no hubo keywords, es general.
         if not found_keywords:
            analisis.tipo_analisis = 'imagen_general_vision_v1'
         db.session.commit()
         return {'es_reclamo': False, 'motivo': 'Información visual/textual insuficiente para LLM.', 'vision_results': vision_results, 'analisis_id': analisis.id}

    imagen_descripcion_para_llm = ". ".join(prompt_description_parts) + "."
    logger.info(f"📝 [RECLAMO] Descripción para LLM: {imagen_descripcion_para_llm} (Análisis ID: {analisis.id})")

    detalles_llm = extract_complaint_details_llm(imagen_descripcion_para_llm)
    current_datos_estructurados['llm_complaint_extraction'] = detalles_llm
    analisis.datos_estructurados = current_datos_estructurados

    es_reclamo_confirmado_por_llm = bool(detalles_llm.get("tipo_problema") or detalles_llm.get("descripcion_problema"))

    if es_reclamo_confirmado_por_llm:
        logger.info(f"✅ [RECLAMO] LLM confirmó/interpretó como reclamo. Tipo: {detalles_llm.get('tipo_problema')}, Desc: {detalles_llm.get('descripcion_problema')} (Análisis ID: {analisis.id})")
        analisis.estado_analisis = "completado"
        # tipo_analisis ya es 'reclamo_vision_llm_v1' o similar.
        db.session.commit()
        return {
            'es_reclamo': True,
            'tipo_sugerido': detalles_llm.get("tipo_problema", "No especificado"),
            'descripcion_sugerida': detalles_llm.get("descripcion_problema", "Por favor, describe el problema que ves en la imagen."),
            'ubicacion_sugerida': detalles_llm.get("ubicacion_problema", ""),
            'detalles_llm': detalles_llm,
            'vision_results': vision_results, # Contiene full_text_annotation
            'analisis_id': analisis.id,
            'error': None
        }
    else:
        logger.info(f"ℹ️ [RECLAMO] LLM no interpretó la descripción de la imagen como un reclamo claro. (Análisis ID: {analisis.id})")
        analisis.estado_analisis = "completado"
        if not found_keywords: # Si ni Vision (keywords) ni LLM vieron nada claro
             analisis.tipo_analisis = 'imagen_general_vision_v1'
        # Si Vision encontró keywords pero LLM no, mantenemos el tipo_analisis de reclamo (ej. 'reclamo_vision_llm_v1')
        # pero devolvemos es_reclamo: False. Esto indica que hubo indicios pero no confirmación.
        db.session.commit()
        return {
            'es_reclamo': False,
            'motivo': 'El análisis por IA no pudo confirmar un reclamo específico a partir de la imagen, aunque se detectaron algunos elementos visuales o textuales.',
            'detalles_llm': detalles_llm,
            'vision_results': vision_results,
            'analisis_id': analisis.id,
            'error': None
        }

# --- Lógica para Interpretación de Pedidos PYME ---
def _procesar_interpretacion_pedido_pyme(
    analisis: AnalisisArchivo,
    vision_results: Dict[str, Any],
    extracted_ocr_text: str,
    pyme_user: User
) -> Dict[str, Any]:
    """Lógica específica para interpretar una imagen como un pedido para una PYME."""
    logger.info(f"⚙️ Procesando como PEDIDO PYME para Análisis ID: {analisis.id}, PYME ID: {pyme_user.id}")
    analisis.tipo_analisis = 'pedido_pyme_vision_ocr_v1' # Tipo específico para esta interpretación

    items_pedido_detectados = []
    items_no_encontrados_catalogo = []
    resumen_ocr = ""

    if not extracted_ocr_text:
        logger.info(f"ℹ️ [PEDIDO] No se detectó texto OCR en la imagen para Análisis ID: {analisis.id}.")
        analisis.estado_analisis = "completado"
        analisis.tipo_analisis = 'imagen_general_vision_v1' # No hay texto, no puede ser pedido
        db.session.commit()
        return {
            'es_pedido': False,
            'motivo': 'No se detectó texto en la imagen que pueda interpretarse como un pedido.',
            'items_detectados': [],
            'items_no_encontrados': [],
            'resumen_ocr': None,
            'analisis_id': analisis.id,
            'error': None
        }

    resumen_ocr = extracted_ocr_text # Guardar el texto completo para referencia

    # Heurística simple para parsear el texto del pedido:
    # Asumimos que cada línea puede ser un ítem.
    # Buscamos patrones como "texto [separador] cantidad" o "cantidad [separador] texto"
    # Esto es muy básico y puede mejorarse mucho (ej. con regex más robustos o LLM).
    lineas = extracted_ocr_text.split('\n')
    posibles_items_texto = []

    for i, linea_raw in enumerate(lineas):
        linea = limpiar_texto_base(linea_raw)
        if not linea:
            continue

        # Intentar extraer una cantidad al final o al principio de la línea
        # Regex para encontrar un número (posiblemente con decimales) al final, opcionalmente precedido por 'x', 'X', '*' o espacio.
        # Y que antes del número haya algo de texto (nombre del producto).
        match_cantidad_al_final = re.search(r"^(.*?)(?:[\sxX*])?\s*(\d+[\.,]?\d*)\s*$", linea)
        # Regex para encontrar un número al principio, seguido de texto.
        match_cantidad_al_principio = re.search(r"^\s*(\d+[\.,]?\d*)\s*(?:[\sxX*])?\s*(.+)", linea)

        nombre_producto_ocr = None
        cantidad_ocr_str = None

        if match_cantidad_al_final:
            nombre_producto_ocr = limpiar_texto_base(match_cantidad_al_final.group(1))
            cantidad_ocr_str = match_cantidad_al_final.group(2).replace(',', '.')
        elif match_cantidad_al_principio:
            cantidad_ocr_str = match_cantidad_al_principio.group(1).replace(',', '.')
            nombre_producto_ocr = limpiar_texto_base(match_cantidad_al_principio.group(2))
        else:
            # Si no hay un número claro, asumir que toda la línea es el nombre y cantidad es 1 por defecto
            # O podríamos marcarlo como no parseable si no hay cantidad explícita.
            # Por ahora, si no hay número, lo ignoramos o lo ponemos como "nombre_solo"
            logger.debug(f"[PEDIDO] Línea OCR sin cantidad clara: '{linea}' (Análisis ID: {analisis.id})")
            # Podríamos añadirlo a una lista de "lineas_no_parseadas"
            continue

        if nombre_producto_ocr and cantidad_ocr_str:
            try:
                cantidad_float = float(cantidad_ocr_str)
                cantidad_int = int(round(cantidad_float)) # Redondear y luego convertir a int
                if cantidad_int <= 0:
                    logger.warning(f"[PEDIDO] Cantidad no positiva '{cantidad_ocr_str}' en línea: '{linea}'. Se ignora.")
                    continue
                posibles_items_texto.append({
                    "nombre_ocr": nombre_producto_ocr,
                    "cantidad_ocr": cantidad_int,
                    "linea_original_ocr": linea_raw,
                    "linea_idx_ocr": i
                })
            except ValueError:
                logger.warning(f"[PEDIDO] No se pudo convertir cantidad '{cantidad_ocr_str}' a número en línea: '{linea}'.")

    if not posibles_items_texto:
        logger.info(f"ℹ️ [PEDIDO] OCR no produjo items parseables con nombre y cantidad. Texto OCR: {extracted_ocr_text[:200]} (Análisis ID: {analisis.id})")
        # Guardar datos en AnalisisArchivo
        current_datos_estructurados = analisis.datos_estructurados if isinstance(analisis.datos_estructurados, dict) else {}
        current_datos_estructurados.update({
            'items_parseados_ocr': [],
            'items_encontrados_catalogo': [],
            'items_no_encontrados_catalogo': [],
            'resumen_ocr_completo': resumen_ocr,
        })
        analisis.datos_estructurados = current_datos_estructurados
        analisis.estado_analisis = "completado"
        db.session.commit()
        return {
            'es_pedido': False, # No se pudieron parsear items
            'motivo': 'El texto de la imagen no pudo ser interpretado como una lista de productos y cantidades.',
            'items_detectados': [],
            'items_no_encontrados': [],
            'resumen_ocr': resumen_ocr,
            'analisis_id': analisis.id,
            'error': None
        }

    logger.info(f"📝 [PEDIDO] Items parseados del OCR: {posibles_items_texto} (Análisis ID: {analisis.id})")

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

    # Guardar resultados en AnalisisArchivo
    current_datos_estructurados = analisis.datos_estructurados if isinstance(analisis.datos_estructurados, dict) else {}
    current_datos_estructurados.update({
        'items_parseados_ocr': posibles_items_texto,
        'items_encontrados_catalogo': items_pedido_detectados,
        'items_no_encontrados_catalogo': items_no_encontrados_catalogo,
        'resumen_ocr_completo': resumen_ocr,
    })
    analisis.datos_estructurados = current_datos_estructurados
    analisis.estado_analisis = "completado"
    db.session.commit()

    if not items_pedido_detectados and not items_no_encontrados_catalogo: # Si el OCR parseo algo pero nada se busco (raro) o nada se encontro
         motivo_final = 'El texto de la imagen no parece corresponder a productos de nuestro catálogo.'
    elif not items_pedido_detectados and items_no_encontrados_catalogo:
        motivo_final = 'Algunos productos mencionados en la imagen no se encontraron en el catálogo o no tienen precio.'
    else:
        motivo_final = 'Pedido parcialmente interpretado desde la imagen.'


    return {
        'es_pedido': bool(items_pedido_detectados), # Es pedido si al menos un item se pudo matchear y tiene precio
        'motivo': motivo_final if not items_pedido_detectados else "Pedido interpretado desde la imagen.",
        'items_detectados': items_pedido_detectados,
        'items_no_encontrados_catalogo': items_no_encontrados_catalogo, # Para informar al usuario
        'resumen_ocr': resumen_ocr, # El texto completo para mostrar si es necesario
        'analisis_id': analisis.id,
        'error': None
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

    resultado_interpretacion = interpretar_imagen_reclamo(archivo_prueba)

    logger.info("\n--- Resultado de la Interpretación ---")
    import json as json_parser # para evitar conflicto con el modulo json de credenciales
    logger.info(json_parser.dumps(resultado_interpretacion, indent=2, ensure_ascii=False))
    logger.info("--- Fin de la Prueba Local ---")

    # Restaurar db.session si lo habíamos mockeado y existía antes
    if original_db_session:
        db.session = original_db_session
