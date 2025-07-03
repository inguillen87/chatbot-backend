# services/interpretacion_imagen_service.py
import logging
import requests
from typing import Dict, Any, List, Optional

from models import ArchivoAdjunto, AnalisisArchivo, User, db
from services.google_vision_service import analyze_image_from_content
from services.llm_utils import extract_complaint_details_llm # Usaremos este para el reclamo

logger = logging.getLogger(__name__)

# Palabras clave que sugieren un reclamo municipal (en minúsculas para comparación)
# Esto puede expandirse y refinarse. Considerar categorías.
PALABRAS_CLAVE_RECLAMO_OBJETOS = {
    "streetlight", "street light", "light pole", "lamp post", "traffic light", "traffic signal", # Alumbrado y semáforos
    "pothole", "hole", "crack", "broken pavement", # Bacheo y calles
    "trash", "garbage", "waste", "dumpster", "overflowing bin", # Basura
    "leak", "pipe burst", "water leak", "flooding", # Fugas de agua
    "fallen tree", "broken branch", # Árboles caídos
    "graffiti", # Vandalismo
    "broken sign", "street sign", # Señalización
    "blocked drain", "sewer", # Alcantarillado
}

PALABRAS_CLAVE_RECLAMO_ETIQUETAS = {
    "public utility", "infrastructure", "road", "street", "sidewalk",
    "hazard", "danger", "damage", "broken", "fallen", "overflowing",
    "vandalism", "neglect",
}


def _descargar_imagen(url: str) -> Optional[bytes]:
    """Descarga el contenido de una imagen desde una URL."""
    try:
        response = requests.get(url, timeout=10) # Timeout de 10 segundos
        response.raise_for_status() # Lanza excepción para códigos de error HTTP
        return response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Error al descargar imagen desde {url}: {e}", exc_info=True)
        return None

def interpretar_imagen_reclamo(archivo_adjunto: ArchivoAdjunto, current_user: Optional[User] = None) -> Dict[str, Any]:
    """
    Interpreta una imagen para determinar si es un reclamo municipal y extrae detalles.

    Args:
        archivo_adjunto: El objeto ArchivoAdjunto que contiene la URL de la imagen.
        current_user: El usuario que subió la imagen (opcional, para contexto o permisos futuros).

    Returns:
        Un diccionario con la interpretación.
        Ej: {'es_reclamo': True, 'tipo_sugerido': 'Alumbrado',
             'descripcion_sugerida': 'Parece un semáforo roto.', 'detalles_llm': {...},
             'vision_results': {...}, 'error': None}
        o {'es_reclamo': False, 'error': 'Mensaje de error si aplica'}
    """
    if not archivo_adjunto or not archivo_adjunto.url:
        return {'es_reclamo': False, 'error': 'Archivo adjunto o URL no válidos.'}

    # Obtener o crear el registro de AnalisisArchivo
    analisis = AnalisisArchivo.query.filter_by(archivo_adjunto_id=archivo_adjunto.id).first()
    if not analisis:
        analisis = AnalisisArchivo(
            archivo_adjunto_id=archivo_adjunto.id,
            tipo_analisis='reclamo_vision_v1' # Provisional, podría cambiar si no es reclamo
        )
        db.session.add(analisis)

    analisis.estado_analisis = "procesando"
    analisis.error_analisis = None # Limpiar errores previos
    db.session.commit()

    logger.info(f"➡️ Iniciando interpretación de imagen para reclamo: Archivo ID {archivo_adjunto.id}, URL: {archivo_adjunto.url}")

    # 1. Descargar la imagen
    image_content = _descargar_imagen(archivo_adjunto.url)
    if not image_content:
        analisis.estado_analisis = "error"
        analisis.error_analisis = "Fallo al descargar la imagen."
        db.session.commit()
        return {'es_reclamo': False, 'error': analisis.error_analisis, 'analisis_id': analisis.id}

    # 2. Analizar con Google Vision
    logger.info(f"🖼️  Enviando imagen (tamaño: {len(image_content)} bytes) a Vision API...")
    vision_results = analyze_image_from_content(image_content)
    analisis.datos_estructurados = {'vision_api_raw': vision_results} # Guardar resultado crudo por ahora

    if vision_results.get("error"):
        logger.error(f"❌ Error de Vision API: {vision_results['error']}")
        analisis.estado_analisis = "error"
        analisis.error_analisis = f"Error de Vision API: {vision_results['error']}"
        db.session.commit()
        return {'es_reclamo': False, 'error': analisis.error_analisis, 'analisis_id': analisis.id}

    # Extraer texto de Vision (si existe)
    extracted_ocr_text = ""
    if vision_results.get("text_annotations"):
        # El primer text_annotation suele ser el texto completo
        extracted_ocr_text = vision_results["text_annotations"][0].get("description", "").strip()
        analisis.texto_extraido = extracted_ocr_text
        logger.info(f" टेक्स्ट OCR detectado: '{extracted_ocr_text[:200]}...'")


    # 3. Analizar resultados de Vision para palabras clave
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
    current_datos_estructurados['keywords_detectadas'] = found_keywords
    analisis.datos_estructurados = current_datos_estructurados

    if not found_keywords and not extracted_ocr_text: # Si no hay keywords ni texto OCR, probablemente no sea un reclamo claro
        logger.info(f"ℹ️ No se encontraron palabras clave de reclamo relevantes ni texto OCR en la imagen {archivo_adjunto.id}.")
        analisis.estado_analisis = "completado"
        analisis.tipo_analisis = 'imagen_general_vision_v1' # Cambiar tipo si no es reclamo
        db.session.commit()
        return {'es_reclamo': False, 'motivo': 'No se detectaron elementos visuales o textuales de reclamo claros.', 'vision_results': vision_results, 'analisis_id': analisis.id}

    logger.info(f"🔑 Palabras clave/elementos detectados: {found_keywords}")

    # 4. Usar LLM para confirmar y extraer detalles del reclamo
    # Construir una descripción de la imagen para el LLM
    prompt_description_parts = []
    if vision_results.get("objects"):
        prompt_description_parts.append("Objetos detectados: " + ", ".join([f"{o['name']}" for o in vision_results["objects"][:5]])) # Limitar a 5 para brevedad
    if vision_results.get("labels"):
        prompt_description_parts.append("Etiquetas generales: " + ", ".join([f"{l['description']}" for l in vision_results["labels"][:5]]))
    if extracted_ocr_text:
        prompt_description_parts.append(f"Texto extraído de la imagen: '{extracted_ocr_text[:200]}'") # Limitar longitud del texto en prompt

    if not prompt_description_parts: # Si, a pesar de todo, no hay nada que describir
         logger.info(f"ℹ️ No hay suficiente información visual o textual para enviar al LLM para la imagen {archivo_adjunto.id}.")
         analisis.estado_analisis = "completado"
         analisis.tipo_analisis = 'imagen_general_vision_v1'
         db.session.commit()
         return {'es_reclamo': False, 'motivo': 'Información visual/textual insuficiente para LLM.', 'vision_results': vision_results, 'analisis_id': analisis.id}

    imagen_descripcion_para_llm = ". ".join(prompt_description_parts) + "."
    logger.info(f"📝 Descripción para LLM: {imagen_descripcion_para_llm}")

    # Llamar a la función de llm_utils para extraer detalles del reclamo
    # Esta función espera el texto de la queja del usuario. Aquí, la "queja" es nuestra descripción de la imagen.
    detalles_llm = extract_complaint_details_llm(imagen_descripcion_para_llm)

    current_datos_estructurados = analisis.datos_estructurados if isinstance(analisis.datos_estructurados, dict) else {}
    current_datos_estructurados['llm_complaint_extraction'] = detalles_llm
    analisis.datos_estructurados = current_datos_estructurados

    es_reclamo_confirmado_por_llm = bool(detalles_llm.get("tipo_problema") or detalles_llm.get("descripcion_problema"))

    if es_reclamo_confirmado_por_llm:
        logger.info(f"✅ LLM confirmó/interpretó como reclamo. Tipo: {detalles_llm.get('tipo_problema')}, Desc: {detalles_llm.get('descripcion_problema')}")
        analisis.estado_analisis = "completado"
        analisis.tipo_analisis = 'reclamo_vision_llm_v1' # Tipo más específico
        db.session.commit()
        return {
            'es_reclamo': True,
            'tipo_sugerido': detalles_llm.get("tipo_problema", "No especificado"),
            'descripcion_sugerida': detalles_llm.get("descripcion_problema", "Por favor, describe el problema que ves en la imagen."),
            'ubicacion_sugerida': detalles_llm.get("ubicacion_problema", ""), # El LLM podría inferir algo si hay texto
            'detalles_llm': detalles_llm,
            'vision_results': vision_results,
            'analisis_id': analisis.id,
            'error': None
        }
    else:
        # Si el LLM no pudo extraer detalles de reclamo convincentes, pero Vision sí encontró algo.
        # Podríamos marcarlo como "potencial_reclamo_necesita_revision_humana" o simplemente no reclamo.
        # Por ahora, si el LLM no lo ve claro, lo tomamos como no reclamo para ser conservadores.
        logger.info(f"ℹ️ LLM no interpretó la descripción de la imagen como un reclamo claro. Imagen ID: {archivo_adjunto.id}")
        analisis.estado_analisis = "completado"
        # Mantener tipo_analisis como 'reclamo_vision_v1' si Vision encontró keywords,
        # o cambiar a 'imagen_general_vision_v1' si queremos ser más estrictos.
        # Por ahora, si Vision vio algo pero LLM no, lo dejamos como 'reclamo_vision_v1'
        # para posible revisión o ajuste de umbrales.
        if not found_keywords : # Si ni Vision ni LLM vieron nada claro
             analisis.tipo_analisis = 'imagen_general_vision_v1'

        db.session.commit()
        return {
            'es_reclamo': False,
            'motivo': 'El análisis por IA no pudo confirmar un reclamo específico a partir de la imagen, aunque se detectaron algunos elementos visuales.',
            'detalles_llm': detalles_llm,
            'vision_results': vision_results,
            'analisis_id': analisis.id,
            'error': None # No es un error técnico, sino de interpretación
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

