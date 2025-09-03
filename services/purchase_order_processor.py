import logging
from typing import Dict, Any, List, Optional
from services.llm_utils import _clean_llm_json_output, llamar_llm_para_generacion_texto

logger = logging.getLogger(__name__)

def extraer_datos_orden_de_compra_con_llm(texto_ocr: str) -> Optional[Dict[str, Any]]:
    """
    Utiliza un LLM para extraer datos estructurados de una orden de compra a partir de su texto OCR.

    Args:
        texto_ocr: El texto completo extraído por OCR de una imagen de orden de compra.

    Returns:
        Un diccionario con los datos estructurados de la orden de compra, o None si falla.
        Ej: {
            "numero_orden": "OC-12345",
            "fecha_orden": "2024-07-15",
            "proveedor": "Proveedor S.A.",
            "cliente": "Cliente Final Ltda.",
            "items": [
                {"descripcion": "Producto A", "cantidad": 10, "precio_unitario": 50.00},
                {"descripcion": "Producto B", "cantidad": 5, "precio_unitario": 120.50}
            ],
            "total": 1102.50
        }
    """
    if not texto_ocr or not texto_ocr.strip():
        logger.warning("[LLM_OC_EXTRACT] texto_ocr vacío o solo espacios.")
        return None

    system_prompt_oc = (
        "Eres un asistente experto en procesar órdenes de compra (OC). "
        "Dado el siguiente texto extraído por OCR de una OC, extrae la información clave. "
        "Devuelve SOLAMENTE un objeto JSON con los siguientes campos:\n"
        "- \"numero_orden\": El número de la orden de compra (string).\n"
        "- \"fecha_orden\": La fecha de la orden (string, en formato YYYY-MM-DD si es posible).\n"
        "- \"proveedor\": El nombre de la empresa proveedora (string).\n"
        "- \"cliente\": El nombre de la empresa o persona que compra (string).\n"
        "- \"items\": Un array de objetos, donde cada objeto representa un ítem de la OC y debe tener "
        "\"descripcion\" (string), \"cantidad\" (float), y \"precio_unitario\" (float).\n"
        "- \"total\": El monto total de la orden de compra (float).\n"
        "Si un campo no se encuentra, puedes omitirlo del JSON."
    )
    user_prompt_oc = (
        "Por favor, procesa el siguiente texto OCR de una orden de compra y extrae los datos en formato JSON:\n"
        "Texto OCR:\n"
        "----------\n"
        f"{texto_ocr}\n"
        "----------\n"
        "Objeto JSON:"
    )

    logger.info(f"[LLM_OC_EXTRACT] Llamando al LLM para extraer de: {texto_ocr[:200]}...")
    respuesta_llm_texto = llamar_llm_para_generacion_texto(
        system_prompt_especifico=system_prompt_oc,
        user_prompt=user_prompt_oc,
        temperature=0.1
    )

    if not respuesta_llm_texto:
        logger.warning("[LLM_OC_EXTRACT] El LLM no devolvió respuesta para el texto OCR de la OC.")
        return None

    cleaned_json_str = _clean_llm_json_output(respuesta_llm_texto)
    try:
        datos_oc = json.loads(cleaned_json_str)
        if isinstance(datos_oc, dict):
            logger.info(f"[LLM_OC_EXTRACT] Datos de OC extraídos por LLM: {datos_oc}")
            return datos_oc
        else:
            logger.error(f"[LLM_OC_EXTRACT] LLM no devolvió un objeto JSON. Respuesta: {cleaned_json_str}")
            return None
    except json.JSONDecodeError as e:
        logger.error(f"[LLM_OC_EXTRACT] Error decodificando JSON de LLM para OC: {e}. Respuesta: {cleaned_json_str}")
        return None
    except Exception as e_gen:
        logger.error(f"[LLM_OC_EXTRACT] Error general procesando respuesta de LLM para OC: {e_gen}", exc_info=True)
        return None
