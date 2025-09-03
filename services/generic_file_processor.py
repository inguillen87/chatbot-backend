import os
import logging
from typing import Optional, Dict, Any
import pandas as pd
from docx import Document
from services.llm_bridge import llamar_llm_para_generacion_texto

logger = logging.getLogger(__name__)

def procesar_archivo_generico(file_path: str, mime_type: str) -> Optional[Dict[str, Any]]:
    """
    Procesa un archivo genérico, extrayendo su contenido y analizándolo con un LLM.

    Args:
        file_path: La ruta local al archivo.
        mime_type: El tipo MIME del archivo.

    Returns:
        Un diccionario con el contenido extraído y el análisis del LLM, o None si falla.
    """
    try:
        texto_extraido = None
        if mime_type == 'application/pdf':
            # La lógica de procesamiento de PDF se manejará a través de Document AI
            # en analisis_archivo_service, por lo que aquí podemos omitirla o
            # usar una extracción de texto simple como fallback.
            logger.info(f"El procesamiento de PDF se delega a Document AI. Pasando por alto en procesador genérico por ahora: {file_path}")
            return None # O implementar una lógica de texto simple si es necesario
        elif mime_type in ['application/vnd.ms-excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'text/csv']:
            texto_extraido = _extraer_texto_de_excel_o_csv(file_path)
        elif mime_type in ['application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']:
            texto_extraido = _extraer_texto_de_word(file_path)
        elif mime_type.startswith('text/'):
            texto_extraido = _extraer_texto_plano(file_path)
        else:
            logger.warning(f"Tipo de archivo no soportado para procesamiento genérico: {mime_type}")
            return None

        if not texto_extraido:
            logger.error(f"No se pudo extraer texto del archivo: {file_path}")
            return None

        # Una vez extraído el texto, lo enviamos a Gemini para análisis
        # El prompt puede ser ajustado para ser más específico según el contexto
        # que se le pase a esta función en el futuro.
        prompt_para_llm = f"""
        Analiza el siguiente texto extraído de un documento y estructura la información clave.
        Si parece un catálogo de productos, extrae una lista de productos con su nombre, descripción y precio si es posible.
        Si parece una orden de compra, extrae el número de orden, los productos y las cantidades.
        Si es otro tipo de documento, proporciona un resumen de su contenido.

        Texto del documento:
        ---
        {texto_extraido}
        ---
        """

        analisis_llm = llamar_llm_para_generacion_texto(
            system_prompt_especifico="Eres un asistente de IA que extrae información estructurada de documentos.",
            user_prompt=prompt_para_llm
        )

        return {
            "texto_extraido": texto_extraido,
            "analisis_llm": analisis_llm
        }

    except Exception as e:
        logger.error(f"Error procesando archivo genérico {file_path}: {e}", exc_info=True)
        return None


def _extraer_texto_de_excel_o_csv(file_path: str) -> Optional[str]:
    """Extrae el contenido de un archivo Excel o CSV como una cadena de texto."""
    try:
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
        return df.to_string()
    except Exception as e:
        logger.error(f"Error leyendo archivo Excel/CSV {file_path}: {e}", exc_info=True)
        return None

def _extraer_texto_de_word(file_path: str) -> Optional[str]:
    """Extrae el contenido de un archivo Word (.docx) como una cadena de texto."""
    try:
        document = Document(file_path)
        return "\n".join([para.text for para in document.paragraphs])
    except Exception as e:
        logger.error(f"Error leyendo archivo Word {file_path}: {e}", exc_info=True)
        return None

def _extraer_texto_plano(file_path: str) -> Optional[str]:
    """Extrae el contenido de un archivo de texto plano."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error leyendo archivo de texto plano {file_path}: {e}", exc_info=True)
        return None
