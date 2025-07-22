import logging
from google.cloud import vision

logger = logging.getLogger(__name__)

class ImageProcessingService:
    def __init__(self):
        self.client = vision.ImageAnnotatorClient()

    def analyze_image(self, image_content: bytes) -> dict:
        """
        Analiza una imagen utilizando la API de Google Cloud Vision.
        """
        logger.info("Analizando imagen con Google Cloud Vision...")
        image = vision.Image(content=image_content)

        features = [
            {"type_": vision.Feature.Type.LABEL_DETECTION},
            {"type_": vision.Feature.Type.TEXT_DETECTION},
            {"type_": vision.Feature.Type.OBJECT_LOCALIZATION},
        ]

        try:
            response = self.client.annotate_image({"image": image, "features": features})

            if response.error.message:
                logger.error(f"Error en la API de Vision: {response.error.message}")
                return {"error": response.error.message}

            labels = [label.description for label in response.label_annotations]
            texts = [text.description for text in response.text_annotations]
            objects = [obj.name for obj in response.localized_object_annotations]

            logger.info(f"Análisis de imagen completado. Labels: {labels}, Texts: {texts}, Objects: {objects}")

            return {
                "labels": labels,
                "texts": texts,
                "objects": objects,
            }
        except Exception as e:
            logger.error(f"Error inesperado al analizar la imagen: {e}", exc_info=True)
            return {"error": "Error inesperado al procesar la imagen."}

image_processing_service = ImageProcessingService()
