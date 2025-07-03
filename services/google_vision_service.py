# services/google_vision_service.py
import os
import json
import logging
from typing import List, Dict, Any, Optional

from google.cloud import vision
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

# --- 1. Carga de Credenciales (Adaptado de google_docai.py) ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
VISION_CLIENT: Optional[vision.ImageAnnotatorClient] = None

try:
    # Priorizar la variable de entorno si está disponible (para Render/contenedores)
    google_creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
    ruta_cred_final = None

    if google_creds_json_str:
        logger.info("✅ [VISION_SVC] Usando credenciales de GOOGLE_CREDENTIALS_JSON.")
        credentials_info = json.loads(google_creds_json_str)
    else:
        # Rutas locales como fallback (para desarrollo)
        ruta_cred_render = "/etc/secrets/google_service_key.json" # Común en Render
        ruta_cred_local_instance = os.path.join(os.getcwd(), "instance", "google-credentials.json")

        if os.path.exists(ruta_cred_render):
            ruta_cred_final = ruta_cred_render
        elif os.path.exists(ruta_cred_local_instance):
            ruta_cred_final = ruta_cred_local_instance

        if ruta_cred_final:
            logger.info(f"✅ [VISION_SVC] Cargando credenciales desde archivo: {ruta_cred_final}")
            with open(ruta_cred_final, "r", encoding="utf-8") as f:
                credentials_info = json.load(f)
        else:
            credentials_info = None
            logger.warning("⚠️ [VISION_SVC] No se encontró GOOGLE_CREDENTIALS_JSON ni archivos locales de credenciales (google_service_key.json, instance/google-credentials.json). El servicio podría no funcionar.")

    if credentials_info:
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        VISION_CLIENT = vision.ImageAnnotatorClient(credentials=GOOGLE_CREDENTIALS)
        CREDENTIALS_LOADED_SUCCESSFULLY = True
        logger.info("✅ [VISION_SVC] Cliente de Google Cloud Vision inicializado exitosamente.")
    else:
        # Intentar inicializar sin credenciales explícitas (puede funcionar en entornos GCP con ADC)
        try:
            VISION_CLIENT = vision.ImageAnnotatorClient()
            CREDENTIALS_LOADED_SUCCESSFULLY = True # Asumimos que ADC funciona si no hay error
            logger.info("✅ [VISION_SVC] Cliente de Google Cloud Vision inicializado con Application Default Credentials (ADC).")
        except Exception as e_adc:
            logger.error(f"❌ [VISION_SVC] Falló la inicialización con credenciales explícitas y también con ADC: {e_adc}")
            CREDENTIALS_LOADED_SUCCESSFULLY = False


except json.JSONDecodeError as e_json:
    logger.error(f"❌ [VISION_SVC] Error de JSON al decodificar GOOGLE_CREDENTIALS_JSON: {e_json}", exc_info=True)
    CREDENTIALS_LOADED_SUCCESSFULLY = False
except Exception as e:
    logger.error(f"❌ [VISION_SVC] Error crítico cargando credenciales o inicializando cliente de Vision: {e}", exc_info=True)
    CREDENTIALS_LOADED_SUCCESSFULLY = False


# --- 2. Funciones de Análisis de Imagen ---

def analyze_image_from_content(image_content: bytes, min_confidence: float = 0.5) -> Dict[str, Any]:
    """
    Analiza el contenido de una imagen para detección de objetos y etiquetas.

    Args:
        image_content: Los bytes de la imagen.
        min_confidence: La puntuación de confianza mínima para incluir un resultado.

    Returns:
        Un diccionario con 'objects' y 'labels' detectados, o mensajes de error.
    """
    if not VISION_CLIENT:
        logger.error("❌ [VISION_SVC] Cliente de Vision no inicializado. No se puede analizar la imagen.")
        return {"error": "Cliente de Vision no inicializado."}
    if not image_content:
        logger.error("❌ [VISION_SVC] Contenido de imagen vacío.")
        return {"error": "Contenido de imagen vacío."}

    image = vision.Image(content=image_content)

    features = [
        {"type_": vision.Feature.Type.OBJECT_LOCALIZATION},
        {"type_": vision.Feature.Type.LABEL_DETECTION},
        {"type_": vision.Feature.Type.TEXT_DETECTION}, # Añadimos detección de texto
    ]

    request = vision.AnnotateImageRequest(image=image, features=features)

    results = {"objects": [], "labels": [], "text_annotations": []}

    try:
        logger.info("➡️ [VISION_SVC] Enviando imagen a Google Cloud Vision API...")
        response = VISION_CLIENT.annotate_image(request=request)

        if response.error.message:
            logger.error(f"❌ [VISION_SVC] Error de Vision API: {response.error.message}")
            return {"error": f"Vision API error: {response.error.message}"}

        # Procesar Object Localization
        if response.localized_object_annotations:
            for obj in response.localized_object_annotations:
                if obj.score >= min_confidence:
                    results["objects"].append({
                        "name": obj.name,
                        "confidence": obj.score,
                        # Los vértices normalizados van de 0 a 1
                        "bounding_poly_normalized": [
                            {"x": v.x, "y": v.y} for v in obj.bounding_poly.normalized_vertices
                        ]
                    })
            logger.info(f"✅ [VISION_SVC] Objetos detectados: {len(results['objects'])}")

        # Procesar Label Detection
        if response.label_annotations:
            for label in response.label_annotations:
                if label.score >= min_confidence:
                    results["labels"].append({
                        "description": label.description,
                        "confidence": label.score
                    })
            logger.info(f"✅ [VISION_SVC] Etiquetas detectadas: {len(results['labels'])}")

        # Procesar Text Detection (OCR)
        if response.text_annotations:
            # La primera anotación suele ser el texto completo
            full_text = response.text_annotations[0].description if response.text_annotations else ""
            results["text_annotations"].append({
                "description": full_text,
                "locale": response.text_annotations[0].locale if response.text_annotations and response.text_annotations[0].locale else "",
                # Podríamos añadir bounding_poly si es necesario para el texto completo
            })
            # También podríamos iterar sobre el resto de text_annotations si necesitamos palabras individuales y sus geometrías
            logger.info(f"✅ [VISION_SVC] Texto detectado (OCR). Longitud: {len(full_text)}")
            if len(full_text) > 150: # Loguear solo un fragmento si es muy largo
                logger.debug(f"[VISION_SVC] Texto detectado (fragmento): {full_text[:150]}...")


    except Exception as e:
        logger.error(f"❌ [VISION_SVC] Excepción durante la llamada a Vision API o procesando respuesta: {e}", exc_info=True)
        return {"error": f"Excepción en Vision API: {str(e)}"}

    return results

# --- Funciones de prueba o utilidades adicionales pueden ir aquí ---

if __name__ == '__main__':
    # Para pruebas locales rápidas (requiere que las credenciales se carguen correctamente)
    # y una imagen de prueba.
    logging.basicConfig(level=logging.INFO)
    logger.info("Ejecutando pruebas locales de google_vision_service.py...")

    if not CREDENTIALS_LOADED_SUCCESSFULLY or not VISION_CLIENT:
        logger.error("Las credenciales no se cargaron o el cliente no se inicializó. Abortando prueba.")
    else:
        # Debes tener una imagen de prueba, por ejemplo 'test_image.jpg' en la raíz del proyecto.
        # O proporciona una ruta absoluta.
        test_image_path = "test_image.jpg" # CAMBIA ESTO A UNA IMAGEN DE PRUEBA VÁLIDA

        # Intentar crear un archivo de imagen de prueba si no existe (solo para demostración)
        # En un caso real, usarías una imagen existente.
        if not os.path.exists(test_image_path):
            try:
                from PIL import Image, ImageDraw
                img = Image.new('RGB', (200, 100), color = 'red')
                d = ImageDraw.Draw(img)
                d.text((10,10), "Hello Vision", fill=(255,255,0))
                img.save(test_image_path, "JPEG")
                logger.info(f"Imagen de prueba '{test_image_path}' creada.")
            except ImportError:
                logger.warning("PIL/Pillow no instalado. No se pudo crear imagen de prueba. Por favor, crea una manualmente.")
            except Exception as e_img:
                logger.error(f"Error creando imagen de prueba: {e_img}")


        if os.path.exists(test_image_path):
            try:
                with open(test_image_path, "rb") as image_file:
                    content = image_file.read()

                logger.info(f"Analizando imagen de prueba: {test_image_path}")
                analysis_result = analyze_image_from_content(content)

                if "error" in analysis_result:
                    logger.error(f"Error en el análisis: {analysis_result['error']}")
                else:
                    logger.info("Resultados del Análisis:")
                    logger.info(f"  Objetos: {json.dumps(analysis_result.get('objects', []), indent=2)}")
                    logger.info(f"  Etiquetas: {json.dumps(analysis_result.get('labels', []), indent=2)}")
                    logger.info(f"  Texto: {json.dumps(analysis_result.get('text_annotations', []), indent=2)}")
            except FileNotFoundError:
                logger.error(f"Archivo de imagen de prueba '{test_image_path}' no encontrado.")
            except Exception as e_test:
                logger.error(f"Error durante la prueba local: {e_test}", exc_info=True)
        else:
            logger.warning(f"No se encontró la imagen de prueba '{test_image_path}'. Por favor, crea una para probar.")

    logger.info("Pruebas locales de google_vision_service.py completadas.")
