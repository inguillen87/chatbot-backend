import logging
from typing import Any, Dict

from models import User
from services.interpretacion_imagen_service import interpretar_imagen_para_chat

logger = logging.getLogger(__name__)


def clasificar_adjunto_whatsapp(adjunto_info: Dict[str, Any], owner_user: User) -> Dict[str, Any]:
    """Clasifica un adjunto recibido por WhatsApp usando modelos de visión/LLM.

    Parameters
    ----------
    adjunto_info: dict
        Información del adjunto subido a GCS. Debe contener al menos ``url`` y ``mime_type``.
    owner_user: User
        Usuario propietario del bot (municipio o pyme) para decidir la lógica de clasificación.

    Returns
    -------
    Dict[str, Any]
        Resultado de ``interpretar_imagen_para_chat`` con la categoría sugerida y otros datos
        obtenidos del análisis. Si ocurre un error, se devuelve un diccionario con la clave ``error``.
    """
    if not adjunto_info or not adjunto_info.get("url"):
        return {"error": "Adjunto inválido"}

    tipo_interpretacion = "reclamo_auto_descripcion_categoria"
    extra_kwargs: Dict[str, Any] = {}

    # Para pymes intentamos interpretar como pedido automáticamente
    if getattr(owner_user, "tipo_chat", "") == "pyme":
        tipo_interpretacion = "pedido_pyme"
        extra_kwargs["pyme_user"] = owner_user

    try:
        logger.info(
            "Clasificando adjunto de WhatsApp con tipo_interpretacion='%s'", tipo_interpretacion
        )
        resultado = interpretar_imagen_para_chat(
            archivo_adjunto=adjunto_info,
            tipo_interpretacion=tipo_interpretacion,
            **extra_kwargs,
        )
        return resultado
    except Exception as exc:
        # Provider exceptions can contain signed media URLs, OCR text or
        # credentials. Keep both logs and the user-facing result bounded.
        logger.error(
            "WhatsApp media classification failed error_type=%s "
            "interpretation_type=%s",
            type(exc).__name__,
            tipo_interpretacion,
        )
        return {
            "error": "media_classification_failed",
            "error_type": type(exc).__name__,
        }
