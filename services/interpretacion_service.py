import logging
import json
import requests
from typing import Dict, Any, List, Optional

# from models import AnalisisArchivo # Movido para evitar importación circular
# Asumiendo que robust_chat está en llm_utils o cohere_ai
from services.llm_utils import robust_chat, _clean_llm_json_output # _clean_llm_json_output es de llm_utils
from services.google_speech_to_text import SpeechToTextService
from services.vision_fallback_service import analyze_image_smart
from services.categorias_municipio import CATEGORIAS_RECLAMO

logger = logging.getLogger(__name__)

class InterpretacionService:

    def interpretar_archivo(self, archivo_adjunto) -> Dict[str, Any]:
        """Interpreta un archivo adjunto y extrae texto si es posible.

        Actualmente solo se soporta la transcripción de archivos de audio. Para
        otros tipos de archivos se devuelve un resultado vacío para no
        introducir lógica basada en palabras clave en Python, tal como indica
        la arquitectura del proyecto.
        """
        resultado = {"datos_estructurados": None, "texto_extraido": None}
        if not archivo_adjunto or not getattr(archivo_adjunto, "mime", None):
            return resultado

        mime = archivo_adjunto.mime.lower()
        if mime.startswith("audio/"):
            stt_service = SpeechToTextService()
            try:
                texto = stt_service.transcribe_audio_url(archivo_adjunto.url, mime)
                if texto:
                    resultado["texto_extraido"] = texto
            except requests.exceptions.RequestException as e:
                logger.error(f"Error de red al descargar audio {archivo_adjunto.url}: {e}", exc_info=True)
                resultado["error"] = "No se pudo descargar el archivo de audio para transcribir."
            except Exception as e:
                from google.api_core.exceptions import PermissionDenied
                if isinstance(e, PermissionDenied) and "billing" in str(e).lower():
                    logger.error("Error de facturación de Google STT: %s", e)
                    resultado["error"] = "El servicio de transcripción de audio no está disponible en este momento."
                else:
                    logger.error(f"Error inesperado transcribiendo audio {archivo_adjunto.url}: {e}", exc_info=True)
                    resultado["error"] = "Ocurrió un error al procesar el audio."

        return resultado

    def interpretar_audio_para_reclamo(
        self, audio_url: str, mime_type: str, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Transcribe un audio y extrae datos estructurados para un reclamo municipal.

        Parameters
        ----------
        audio_url: str
            URL directa al archivo de audio.
        mime_type: str
            Tipo MIME del audio (por ejemplo, ``"audio/ogg"``).
        user_id: Optional[int]
            Identificador de usuario para pasar al LLM en caso necesario.

        Returns
        -------
        Dict[str, Any]
            Diccionario con ``texto_transcrito`` y ``datos_estructurados`` con los
            campos extraídos. Si ocurre algún error, ambos campos pueden estar
            vacíos.
        """

        if not audio_url or not mime_type:
            return {"texto_transcrito": "", "datos_estructurados": {}}

        stt_service = SpeechToTextService()
        try:
            texto = stt_service.transcribe_audio_url(audio_url, mime_type)
        except Exception as e:
            logger.error(f"Error transcribiendo audio {audio_url}: {e}", exc_info=True)
            texto = ""

        datos = {}
        if texto:
            datos = self._llamar_llm_para_extraccion_ticket_municipal(texto, user_id)

        return {"texto_transcrito": texto, "datos_estructurados": datos}

    def interpretar_imagen_para_reclamo(
        self, image_url: str, mime_type: str, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Analiza una imagen y extrae datos estructurados para un reclamo municipal.

        La imagen se envía a un servicio de visión para obtener etiquetas y
        objetos detectados. Esa información se resume y se pasa a un LLM para
        clasificarla dentro de las categorías admitidas por la aplicación.
        """

        if not image_url or not mime_type:
            return {"palabras_clave": [], "datos_estructurados": {}}

        try:
            resp = requests.get(image_url, timeout=10)
            resp.raise_for_status()
            image_bytes = resp.content
        except Exception as e:
            logger.error(f"Error descargando imagen {image_url}: {e}", exc_info=True)
            return {"palabras_clave": [], "datos_estructurados": {}}

        vision_data = analyze_image_smart(image_bytes)
        keywords: List[str] = []
        for lbl in vision_data.get("labels", []):
            desc = lbl.get("description")
            if desc:
                keywords.append(desc)
        for obj in vision_data.get("objects", []):
            name = obj.get("name")
            if name:
                keywords.append(name)
        ocr_text = vision_data.get("full_text_annotation", {}).get("description", "").strip()
        if ocr_text:
            keywords.extend([w for w in ocr_text.split() if w])

        datos = {}
        if keywords:
            categorias_str = ", ".join(CATEGORIAS_RECLAMO)
            prompt = (
                "Eres un asistente que clasifica imágenes para un sistema de reclamos municipales. "
                f"Palabras clave detectadas: {', '.join(keywords)}. "
                "Devuelve un JSON válido con las claves: categoria (una de las categorías permitidas), "
                "descripcion_corta_problema y palabras_clave (lista). "
                f"Las categorías permitidas son: {categorias_str}."
            )
            try:
                response_content = robust_chat(message=prompt, user_id=user_id)
                if response_content:
                    cleaned = _clean_llm_json_output(response_content)
                    if cleaned:
                        datos = json.loads(cleaned)
            except Exception as e:
                logger.error(
                    f"Error llamando al LLM para extracción desde imagen {image_url}: {e}",
                    exc_info=True,
                )

        return {"palabras_clave": keywords, "datos_estructurados": datos}

    def _llamar_llm_para_extraccion_ticket_municipal(self, texto_completo: str, user_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Llama a un LLM para extraer detalles de un reclamo municipal desde texto.
        """
        if not texto_completo:
            return {}

        campos_esperados = [
            "tipo_solicitud",
            "tipo_problema",
            "descripcion_corta_problema",
            "direccion_problema",
            "nombre_ciudadano",
            "email_ciudadano",
            "telefono_ciudadano",
            "detalles_adicionales",
        ]

        prompt = (
            "Eres un asistente experto en procesar reclamos ciudadanos a partir de texto. "
            "Analiza el siguiente TEXTO DEL RECLAMO y extrae la información relevante "
            f"correspondiente a los siguientes campos: {', '.join(campos_esperados)}. \n"
            "Devuelve la información ÚNICAMENTE como un objeto JSON válido. Las claves del JSON deben ser "
            f"los nombres de los campos de la lista: {campos_esperados}.\n"
            "Si un campo no se encuentra en el texto, omite esa clave del JSON.\n"
            "Prioriza la información más específica y relevante para cada campo.\n"
            "Para 'tipo_solicitud', indica si el texto describe un reclamo o una sugerencia.\n"
            "Para 'descripcion_corta_problema', extrae la esencia del reclamo o sugerencia.\n"
            "Para 'direccion_problema', sé lo más específico posible con la ubicación.\n"
            "Incluye 'email_ciudadano' solo si aparece explícitamente en el texto.\n\n"
            f"TEXTO DEL RECLAMO:\n'''{texto_completo[:8000]}'''\n\n"
            "JSON RESPONSE:"
        )

        try:
            # robust_chat podría necesitar user_id para contextos específicos o límites de uso
            # response_content = robust_chat(message=prompt, model_override="gpt-4o-mini", user_id=user_id)
            response_content = robust_chat(message=prompt, user_id=user_id)
            if response_content:
                cleaned_response = _clean_llm_json_output(response_content)
                if cleaned_response:
                    extracted_data = json.loads(cleaned_response)
                    return {k: v for k, v in extracted_data.items() if k in campos_esperados and v}
            logger.warning(f"LLM no devolvió contenido o contenido vacío tras limpiar para extracción de ticket municipal. Texto: {texto_completo[:200]}")
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"JSONDecodeError al parsear respuesta de LLM para extracción municipal: {e}. Respuesta: '{response_content}'. Texto: {texto_completo[:200]}")
            return {}
        except Exception as e:
            logger.error(f"Error llamando a LLM para extracción municipal: {e}. Texto: {texto_completo[:200]}", exc_info=True)
            return {}

    def _llamar_llm_para_extraccion_pedido_pyme(self, texto_completo: str, user_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Llama a un LLM para extraer detalles de un pedido PYME desde texto.
        """
        if not texto_completo:
            return {}

        campos_esperados = [
            "nombre_cliente",
            "telefono_cliente",
            "email_cliente",
            "direccion_entrega",
            "items_pedido",
            "notas_adicionales"
        ]

        prompt = (
            "Eres un asistente experto en procesar pedidos para PYMEs a partir de texto. "
            "Analiza el siguiente TEXTO DEL PEDIDO y extrae la información relevante "
            f"correspondiente a los siguientes campos: {', '.join(campos_esperados)}. \n"
            "Para 'items_pedido', extrae cada producto con su cantidad y unidad si se especifica, como una lista de objetos JSON, cada uno con 'producto', 'cantidad' y opcionalmente 'unidad'.\n"
            "Devuelve TODA la información ÚNICAMENTE como un objeto JSON válido. Las claves del JSON deben ser "
            f"los nombres de los campos de la lista: {campos_esperados}.\n"
            "Si un campo no se encuentra en el texto, omite esa clave del JSON.\n"
            "Presta especial atención a las cantidades y nombres de productos.\n\n"
            f"TEXTO DEL PEDIDO:\n'''{texto_completo[:8000]}'''\n\n"
            "JSON RESPONSE:"
        )

        try:
            response_content = robust_chat(message=prompt, user_id=user_id)
            if response_content:
                cleaned_response = _clean_llm_json_output(response_content)
                if cleaned_response:
                    extracted_data = json.loads(cleaned_response)
                    if "items_pedido" in extracted_data and not isinstance(extracted_data["items_pedido"], list):
                        logger.warning(f"LLM devolvió 'items_pedido' pero no es una lista: {extracted_data['items_pedido']}")
                        # Intentar convertir a lista si es un string que parece una lista de JSON
                        if isinstance(extracted_data["items_pedido"], str):
                            try:
                                potential_list = json.loads(extracted_data["items_pedido"])
                                if isinstance(potential_list, list):
                                    extracted_data["items_pedido"] = potential_list
                                else:
                                     del extracted_data["items_pedido"]
                            except json.JSONDecodeError:
                                del extracted_data["items_pedido"]
                        else:
                            del extracted_data["items_pedido"]
                    return {k: v for k, v in extracted_data.items() if k in campos_esperados and v}
            logger.warning(f"LLM no devolvió contenido o contenido vacío tras limpiar para extracción de pedido pyme. Texto: {texto_completo[:200]}")
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"JSONDecodeError al parsear respuesta de LLM para extracción pyme: {e}. Respuesta: '{response_content}'. Texto: {texto_completo[:200]}")
            return {}
        except Exception as e:
            logger.error(f"Error llamando a LLM para extracción pyme: {e}. Texto: {texto_completo[:200]}", exc_info=True)
            return {}

    def interpretar_analisis_para_datos_ticket(
        self,
        analisis_archivo, #: AnalisisArchivo, # Type hint removido para evitar importación temprana
        tipo_contexto: str, # "municipio" o "pyme"
        user_id: Optional[int] = None # Para pasar a las llamadas LLM si es necesario
    ) -> Dict[str, Any]:
        from models import AnalisisArchivo # Importación local para evitar ciclo
        """
        Interpreta el contenido de un AnalisisArchivo para extraer datos estructurados
        útiles para pre-llenar un ticket o pedido.
        """
        if not analisis_archivo:
            return {}

        datos_interpretados = {}

        if analisis_archivo.datos_estructurados and isinstance(analisis_archivo.datos_estructurados, dict):
            logger.info(f"Usando datos_estructurados preexistentes del AnalisisArchivo ID: {analisis_archivo.id}")
            datos_interpretados = analisis_archivo.datos_estructurados.copy()
            datos_interpretados["_fuente_interpretacion"] = "datos_estructurados_originales"
            # Si los datos estructurados son de DocumentAI, es posible que ya no necesitemos LLM.
            # Podríamos añadir una lógica para ver si son 'suficientes'.

        if not datos_interpretados and analisis_archivo.texto_extraido: # Si no hay datos estructurados, o si queremos complementar.
            logger.info(f"Interpretando texto_extraido del AnalisisArchivo ID: {analisis_archivo.id} usando LLM para contexto: {tipo_contexto}")
            texto_a_procesar = analisis_archivo.texto_extraido

            datos_llm = {}
            if tipo_contexto == "municipio":
                datos_llm = self._llamar_llm_para_extraccion_ticket_municipal(texto_a_procesar, user_id)
                if datos_llm:
                    datos_interpretados.update(datos_llm) # Usar update para no sobreescribir "_fuente_interpretacion" si ya existía
                    datos_interpretados["_fuente_interpretacion"] = datos_interpretados.get("_fuente_interpretacion", "") + "+llm_municipal"
            elif tipo_contexto == "pyme":
                datos_llm = self._llamar_llm_para_extraccion_pedido_pyme(texto_a_procesar, user_id)
                if datos_llm:
                    datos_interpretados.update(datos_llm)
                    datos_interpretados["_fuente_interpretacion"] = datos_interpretados.get("_fuente_interpretacion", "") + "+llm_pyme"
            else:
                logger.warning(f"Tipo de contexto desconocido '{tipo_contexto}' para interpretación LLM.")

        elif not analisis_archivo.texto_extraido and not analisis_archivo.datos_estructurados:
            logger.info(f"AnalisisArchivo ID: {analisis_archivo.id} no tiene texto_extraido ni datos_estructurados para interpretar.")
            return {}

        # Limpiar el prefijo "+" si solo hubo una fuente de interpretación LLM
        if "_fuente_interpretacion" in datos_interpretados and datos_interpretados["_fuente_interpretacion"].startswith("+"):
            datos_interpretados["_fuente_interpretacion"] = datos_interpretados["_fuente_interpretacion"][1:]


        logger.info(f"Datos interpretados para AnalisisArchivo ID {analisis_archivo.id}: {datos_interpretados}")
        return datos_interpretados


interpretacion_service = InterpretacionService()
