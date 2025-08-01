import os
import logging
from google.cloud import vision
from google.api_core import exceptions as core_exceptions
from werkzeug.utils import secure_filename

# Configuración del logger
logger = logging.getLogger(__name__)

class GoogleVisionService:
    def __init__(self, credentials_path=None):
        self.client = None
        try:
            if credentials_path and os.path.exists(credentials_path):
                self.client = vision.ImageAnnotatorClient.from_service_account_file(credentials_path)
                logger.info("✅ [VISION_SVC] Cliente de Google Vision inicializado con credenciales de archivo.")
            else:
                # Intenta usar las credenciales de entorno por defecto (Application Default Credentials)
                logger.warning("⚠️ No explicit credentials found. Attempting to use Application Default Credentials (ADC).")
                self.client = vision.ImageAnnotatorClient()
                # La siguiente línea verificará si ADC funcionó, si no, lanzará una excepción
                self.client.feature_level_lfp_response_handler()
                logger.info("✅ [VISION_SVC] Cliente de Google Vision inicializado con ADC.")
        except Exception as e:
            logger.error(f"❌ ADC not found. Could not automatically determine credentials.")
            logger.error(f"❌ [VISION_SVC] No se pudieron cargar las credenciales de Google. El servicio de Vision no funcionará.", exc_info=True)
            self.client = None

    def analyze_image(self, image_content, features):
        if not self.client:
            raise ConnectionError("El cliente de Google Vision no está inicializado.")
        if not image_content:
            logger.error("❌ [VISION_SVC] Contenido de imagen vacío.")
            return None

        try:
            image = vision.Image(content=image_content)
            logger.info("➡️ [VISION_SVC] Enviando imagen a Google Cloud Vision API...")
            response = self.client.annotate_image({"image": image, "features": features})

            if response.error.message:
                raise GoogleAPICallError(response.error.message)

            return response
        except core_exceptions.GoogleAPICallError as e:
            logger.error(f"❌ [VISION_SVC] Error de Vision API: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"❌ [VISION_SVC] Error inesperado en el servicio de Vision: {e}", exc_info=True)
            raise

def analyze_image_from_content(image_content):
    """
    Analiza una imagen a partir de su contenido en bytes.
    """
    try:
        service = GoogleVisionService()
        if not service.client:
            return {"error": "El servicio de Vision no está configurado."}

        features = [
            {"type_": vision.Feature.Type.LABEL_DETECTION, "max_results": 10},
            {"type_": vision.Feature.Type.OBJECT_LOCALIZATION, "max_results": 10},
            {"type_": vision.Feature.Type.TEXT_DETECTION},
        ]

        response = service.analyze_image(image_content, features)

        if not response:
            return {"error": "No se pudo obtener respuesta del servicio de Vision."}

        # Procesar y devolver una respuesta estructurada
        result = {}
        if response.label_annotations:
            result["labels"] = [label.description for label in response.label_annotations]
        if response.localized_object_annotations:
            result["objects"] = [obj.name for obj in response.localized_object_annotations]
        if response.text_annotations:
            result["text"] = response.text_annotations[0].description

        return result

    except ConnectionError as e:
        logger.error(f"Error de conexión con Vision API: {e}")
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error inesperado al analizar imagen: {e}", exc_info=True)
        return {"error": "Error interno al procesar la imagen."}

# Instancia global del servicio para ser usada en otras partes de la aplicación si es necesario
# Se recomienda crear la instancia donde se vaya a usar para mejor manejo de la configuración.
# vision_service = GoogleVisionService(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
