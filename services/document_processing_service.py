import os
import logging
from typing import Optional

# Asumimos que los servicios necesarios están en el mismo directorio 'services'
# y que el path está correctamente configurado para permitir estas importaciones.
try:
    from .google_docai import _obtener_documento_ai
    from .gemini_bridge import llamar_gemini_para_generacion_texto
except ImportError:
    # Fallback para pruebas o si la estructura de importación es diferente
    # En un entorno de producción con la app Flask, esto no debería ocurrir.
    _obtener_documento_ai = None
    llamar_gemini_para_generacion_texto = None

logger = logging.getLogger(__name__)

# --- Constantes para Prompts ---
PROMPT_RESUMEN_PDF = """
Eres un experto en resumir documentos de negocios. Tu tarea es leer el siguiente texto, que ha sido extraído de un documento PDF, y generar un resumen conciso y claro.

**Instrucciones:**
1.  **Identifica el Propósito Principal**: ¿Es un catálogo de productos, un reporte financiero, un contrato, un manual de usuario, o algo más? Menciona el tipo de documento al inicio del resumen.
2.  **Extrae Puntos Clave**: Enumera los 3-5 puntos más importantes del documento. Si es un catálogo, menciona las categorías de productos principales. Si es un reporte, las conclusiones más relevantes.
3.  **Mantén la Brevedad**: El resumen no debe exceder las 150 palabras.
4.  **Lenguaje Profesional**: Usa un tono claro, profesional y directo.

A continuación, el texto extraído del documento:
---
{texto_extraido}
---
"""

def _extraer_texto_de_pdf(pdf_path: str) -> Optional[str]:
    """
    Usa el servicio de Document AI para extraer el texto completo de un archivo PDF.

    Args:
        pdf_path: La ruta local al archivo PDF.

    Returns:
        El texto extraído como una cadena, o None si falla la extracción.
    """
    if not _obtener_documento_ai:
        logger.error("[DOC_PROC_SVC] La función '_obtener_documento_ai' no está disponible.")
        return None

    logger.info(f"[DOC_PROC_SVC] Iniciando extracción de texto para: {pdf_path}")

    try:
        document_obj = _obtener_documento_ai(pdf_path, mime_type="application/pdf")

        if document_obj and document_obj.text:
            logger.info(f"[DOC_PROC_SVC] Texto extraído exitosamente de '{os.path.basename(pdf_path)}'. Longitud: {len(document_obj.text)} caracteres.")
            return document_obj.text
        else:
            logger.warning(f"[DOC_PROC_SVC] Document AI no devolvió texto para el archivo: {pdf_path}")
            return None
    except Exception as e:
        logger.error(f"[DOC_PROC_SVC] Excepción durante la extracción de texto con DocAI: {e}", exc_info=True)
        return None

def resumir_pdf(pdf_path: str) -> Optional[str]:
    """
    Función principal que orquesta la extracción de texto de un PDF y su resumen con Gemini.

    Args:
        pdf_path: La ruta local al archivo PDF a resumir.

    Returns:
        Un string con el resumen del PDF, o un mensaje de error si el proceso falla.
    """
    texto_extraido = _extraer_texto_de_pdf(pdf_path)

    if not texto_extraido:
        return "No se pudo extraer texto del documento PDF para resumir."

    if not llamar_gemini_para_generacion_texto:
         logger.error("[DOC_PROC_SVC] La función 'llamar_gemini_para_generacion_texto' no está disponible.")
         return "El servicio de resumen no está disponible en este momento."

    logger.info("[DOC_PROC_SVC] Enviando texto extraído a Gemini para resumen...")

    user_prompt_for_summary = PROMPT_RESUMEN_PDF.format(texto_extraido=texto_extraido)

    # El system_prompt ya está dentro del user_prompt en este caso,
    # por lo que el system_prompt_especifico para la llamada a Gemini puede ser simple.
    system_prompt_for_gemini = "Tu única función es responder a la solicitud del usuario."

    resumen = llamar_gemini_para_generacion_texto(
        system_prompt_especifico=system_prompt_for_gemini,
        user_prompt=user_prompt_for_summary,
        model_name="gemini-1.5-pro-preview-0409" # Usar el modelo potente
    )

    if resumen:
        logger.info(f"[DOC_PROC_SVC] Resumen generado exitosamente.")
        return resumen
    else:
        logger.error("[DOC_PROC_SVC] La llamada a Gemini para resumir no devolvió un resultado.")
        return "No se pudo generar un resumen para el documento."
