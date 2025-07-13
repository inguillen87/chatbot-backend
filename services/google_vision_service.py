# services/google_vision_service.py
import os
import json
import logging
from typing import List, Dict, Any, Optional

from google.cloud import vision
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

# --- 1. Carga de Credenciales (Centralizada) ---
from .google_auth_util import get_google_credentials

VISION_CLIENT: Optional[vision.ImageAnnotatorClient] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False

try:
    g_credentials = get_google_credentials()
    if g_credentials:
        VISION_CLIENT = vision.ImageAnnotatorClient(credentials=g_credentials)
        CREDENTIALS_LOADED_SUCCESSFULLY = True
        logger.info("✅ [VISION_SVC] Cliente de Google Cloud Vision inicializado exitosamente.")
    else:
        logger.error("❌ [VISION_SVC] No se pudieron cargar las credenciales de Google. El servicio de Vision no funcionará.")
        CREDENTIALS_LOADED_SUCCESSFULLY = False
except Exception as e:
    logger.error(f"❌ [VISION_SVC] Error crítico inicializando cliente de Vision: {e}", exc_info=True)
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

    results = {"objects": [], "labels": [], "full_text_annotation": None, "text_annotations_detail": []} # Cambiado

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
            # La primera anotación (índice 0) es generalmente el texto completo.
            full_text_annotation_obj = response.text_annotations[0]
            full_text = full_text_annotation_obj.description
            results["full_text_annotation"] = {
                "description": full_text,
                "locale": full_text_annotation_obj.locale,
                "bounding_poly": [
                    {"x": v.x, "y": v.y} for v in full_text_annotation_obj.bounding_poly.vertices
                ]
            }
            logger.info(f"✅ [VISION_SVC] Texto completo detectado (OCR). Longitud: {len(full_text)}")
            if len(full_text) > 150:
                logger.debug(f"[VISION_SVC] Texto completo (fragmento): {full_text[:150]}...")

            # Las anotaciones restantes (a partir del índice 1) son palabras individuales o bloques de texto más pequeños.
            # Las almacenamos en "text_annotations_detail" para uso futuro si es necesario.
            for i in range(1, len(response.text_annotations)):
                text_block = response.text_annotations[i]
                results["text_annotations_detail"].append({
                    "description": text_block.description,
                    "bounding_poly": [
                        {"x": v.x, "y": v.y} for v in text_block.bounding_poly.vertices
                    ]
                })
            logger.info(f"✅ [VISION_SVC] Detalles de texto adicionales (palabras/bloques): {len(results['text_annotations_detail'])}")


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
                    logger.info(f"  Texto Completo: {json.dumps(analysis_result.get('full_text_annotation', {}), indent=2)}")
                    logger.info(f"  Detalles de Texto: {json.dumps(analysis_result.get('text_annotations_detail', []), indent=2)}")
            except FileNotFoundError:
                logger.error(f"Archivo de imagen de prueba '{test_image_path}' no encontrado.")
            except Exception as e_test:
                logger.error(f"Error durante la prueba local: {e_test}", exc_info=True)
        else:
            logger.warning(f"No se encontró la imagen de prueba '{test_image_path}'. Por favor, crea una para probar.")

    logger.info("Pruebas locales de google_vision_service.py completadas.")
