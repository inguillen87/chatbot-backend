import logging
import os
from typing import Optional
from services.openai_bridge import client as openai_client

logger = logging.getLogger(__name__)

def generar_imagen(prompt: str, size: str = "1024x1024", quality: str = "standard", model: str = "dall-e-3") -> Optional[str]:
    """
    Genera una imagen usando el modelo DALL-E 3 de OpenAI.
    Ideal para banners, flyers y assets de campaña.

    Args:
        prompt: Descripción detallada de la imagen a generar.
        size: Tamaño de la imagen ("1024x1024", "1024x1792", etc).
        quality: "standard" o "hd".
        model: Modelo a usar (default: dall-e-3).

    Returns:
        URL de la imagen generada o None si falla.
    """
    if not openai_client:
        logger.error("OpenAI client not initialized.")
        return None

    try:
        logger.info(f"Generando imagen con prompt: {prompt[:50]}...")
        response = openai_client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            quality=quality,
            n=1,
        )
        image_url = response.data[0].url
        logger.info("Imagen generada exitosamente.")
        return image_url
    except Exception as e:
        logger.error(f"Error generando imagen: {e}", exc_info=True)
        return None

def editar_imagen(image_path: str, prompt: str, mask_path: Optional[str] = None, size: str = "1024x1024") -> Optional[str]:
    """
    Edita una imagen existente usando DALL-E 2 (DALL-E 3 no soporta edits vía API actualmente).

    Args:
        image_path: Ruta al archivo de imagen local (PNG, < 4MB).
        prompt: Descripción de la edición.
        mask_path: Ruta a la máscara (opcional, área transparente a editar).

    Returns:
        URL de la imagen editada.
    """
    if not openai_client:
        return None

    try:
        # Nota: La API de edits requiere archivos abiertos en binario
        # Implementación simplificada asumiendo rutas válidas
        if not os.path.exists(image_path):
            logger.error(f"Archivo de imagen no encontrado: {image_path}")
            return None

        with open(image_path, "rb") as image_file:
            args = {
                "image": image_file,
                "prompt": prompt,
                "n": 1,
                "size": size
            }
            if mask_path and os.path.exists(mask_path):
                args["mask"] = open(mask_path, "rb") # Handle close properly in prod

            response = openai_client.images.edit(**args)

            if "mask" in args:
                args["mask"].close()

            return response.data[0].url
    except Exception as e:
        logger.error(f"Error editando imagen: {e}", exc_info=True)
        return None
