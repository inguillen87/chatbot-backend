import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Dict, Any, List, Optional
from tests.mocks import MockGeminiResponse
import google.generativeai as genai
from google.auth import exceptions
from google.oauth2 import service_account
from tenacity import retry, stop_after_attempt, wait_fixed
from vertexai.preview.generative_models import GenerativeModel, GenerationConfig, HarmCategory, HarmBlockThreshold
import vertexai
from services.chatbot_prompts import JULES_SYSTEM_PROMPT

# Configuración del logger
logger = logging.getLogger(__name__)

# --- Modelos de Gemini ---
GEMINI_MODEL_PRESTAMOS = "gemini-2.5-flash"
GEMINI_MODEL_STANDARD = "gemini-2.5-flash"
MAX_HISTORIAL_MESSAGES = 10

def _limpiar_historial_gemini(historial: list) -> list:
    """
    Asegura que el historial no exceda el máximo de mensajes,
    manteniendo los más recientes.
    """
    if len(historial) > MAX_HISTORIAL_MESSAGES:
        return historial[-MAX_HISTORIAL_MESSAGES:]
    return historial

# --- Configuración de Seguridad de Gemini ---
GEMINI_SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# --- Inicialización del Executor para llamadas asíncronas ---
executor = ThreadPoolExecutor(max_workers=5)

def _get_credentials():
    """Busca las credenciales de Google en el entorno."""
    try:
        # Busca en el entorno las credenciales (para Render, local con .env)
        credentials_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if credentials_json_str:
            credentials_info = json.loads(credentials_json_str)
            return service_account.Credentials.from_service_account_info(credentials_info)

        # Si no, intenta con las credenciales por defecto (para desarrollo local con gcloud auth)
        credentials, project_id = google.auth.default()
        return credentials
    except (exceptions.DefaultCredentialsError, json.JSONDecodeError, KeyError) as e:
        logger.error(f"Error al cargar credenciales de Google: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_fixed(1), reraise=True)
def robust_chat(model, *args, **kwargs):
    """
    Wrapper para la llamada a `generate_content` con reintentos.
    """
    return model.generate_content(*args, **kwargs)

def _llamar_gemini_impl(mensaje_usuario: str = None, usuario: dict = None, historial: list = None, mensaje: str = None) -> dict:
    logger = logging.getLogger(__name__)
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig, HarmCategory, HarmBlockThreshold

        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        location = "us-central1"

        if not project_id:
            logger.error("GOOGLE_PROJECT_ID no está configurado. No se puede inicializar Gemini GenAI.")
            raise EnvironmentError("GOOGLE_PROJECT_ID no configurado.")

        try:
            vertexai.init(project=project_id, location=location)
        except ValueError as e:
            logger.error(f"Error al inicializar Vertex AI: {e}")
            return {
                "message_body": "Error de configuración del servicio de IA (región no soportada). Por favor, contacta al administrador.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {"error_detalle": str(e), "mensaje_original": mensaje_usuario},
                "pedir_info": None, "botones": []
            }

        genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

        model_name = "gemini-2.5-flash"

        model = GenerativeModel(
            model_name,
            system_instruction=[JULES_SYSTEM_PROMPT]
        )

        if mensaje and not mensaje_usuario:
            mensaje_usuario = mensaje

        mensaje_usuario_obj = {}
        texto_mensaje = ""
        try:
            mensaje_usuario_obj = json.loads(mensaje_usuario)
            if isinstance(mensaje_usuario_obj, dict):
                texto_mensaje = mensaje_usuario_obj.get("texto", "")
            else:
                texto_mensaje = str(mensaje_usuario_obj)
                mensaje_usuario_obj = {"texto": texto_mensaje}
        except (json.JSONDecodeError, TypeError):
            texto_mensaje = mensaje_usuario
            mensaje_usuario_obj = {"texto": texto_mensaje}

        contents_for_api = [
            f"USUARIO: {json.dumps(usuario, ensure_ascii=False)}\nHISTORIAL PREVIO: {json.dumps(historial, ensure_ascii=False)}\nMENSAJE ACTUAL: {json.dumps(mensaje_usuario_obj, ensure_ascii=False)}"
        ]
        if usuario and usuario.get("datos_interpretados_archivo"):
            contents_for_api.append(f"\nDATOS EXTRAIDOS DE ARCHIVO ADJUNTO: {json.dumps(usuario.get('datos_interpretados_archivo'), ensure_ascii=False)}")

        logger.info(f"Enviando a Gemini ({model_name}). Mensaje: {texto_mensaje[:100]}...")

        generation_config = GenerationConfig(
            temperature=0.2,
            top_p=0.95,
            top_k=40,
            max_output_tokens=8192,
            response_mime_type="application/json"
        )

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
        }

        response = model.generate_content(
            contents_for_api,
            generation_config=generation_config,
            safety_settings=safety_settings,
        )

        logger.info(f"Respuesta recibida de Gemini. Candidates count: {len(response.candidates)}")
        if not response.candidates:
            logger.error("Gemini no devolvió candidatos en la respuesta.")
            try:
                block_reason = response.prompt_feedback.block_reason
                block_reason_message = response.prompt_feedback.block_reason_message
                logger.error(f"Prompt feedback: block_reason={block_reason}, message='{block_reason_message}'")
            except Exception:
                pass
            raise ValueError("Respuesta de Gemini sin candidatos.")

        if response.candidates and response.candidates[0].content.parts:
            respuesta_texto_crudo = response.candidates[0].content.parts[0].text.strip()
            logger.info(f"Respuesta de Gemini (crudo): {respuesta_texto_crudo}")
        else:
            logger.error("Gemini no devolvió contenido en el primer candidato.")
            respuesta_texto_crudo = '{"message_body": "No pude procesar tu solicitud en este momento. Por favor, intenta de nuevo más tarde.", "accion_backend": "error"}'

    except ImportError as ie:
        logger.error(f"Error importando librería google.generativeai: {ie}. Asegúrate que google-genai está instalado.")
        return {
            "message_body": "Error de configuración del servicio de IA. Por favor, contacta al administrador.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Fallo de importación google.generativeai: {str(ie)}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }
    except EnvironmentError as ee:
        logger.error(f"Error de entorno para google.generativeai: {ee}")
        return {
            "message_body": "Error de configuración del servicio de IA (entorno). Por favor, contacta al administrador.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": str(ee), "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }
    except Exception as e_gemini_call:
        logger.error(f"Error en la llamada a Gemini API: {e_gemini_call}", exc_info=True)
        error_detail_from_api = str(e_gemini_call)
        try:
            if hasattr(e_gemini_call, 'message'): error_detail_from_api = e_gemini_call.message
        except: pass

        return {
            "message_body": "Lo siento, no pude procesar tu solicitud en este momento debido a un error con el asistente IA. Intenta de nuevo más tarde.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Error API Gemini: {error_detail_from_api}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }

    try:
        if respuesta_texto_crudo.startswith("```json"):
            respuesta_texto_crudo = respuesta_texto_crudo[len("```json"):].strip()
        if respuesta_texto_crudo.endswith("```"):
            respuesta_texto_crudo = respuesta_texto_crudo[:-len("```")].strip()

        logger.debug(f"Texto de Gemini para parsear a JSON: {respuesta_texto_crudo[:500]}...")
        parsed_response = json.loads(respuesta_texto_crudo)
        return parsed_response

    except json.JSONDecodeError as e_json:
        logger.error(f"Error parseando JSON de Gemini: {e_json}. Respuesta cruda: '{respuesta_texto_crudo}'")
        # Attempt to fix the JSON by adding the missing quote
        fixed_json_str = respuesta_texto_crudo.replace('id_archivo": null', 'id_archivo": null"')
        try:
            logger.info(f"Intentando parsear JSON reparado: {fixed_json_str[:500]}...")
            parsed_response = json.loads(fixed_json_str)
            return parsed_response
        except Exception as e_repair:
            logger.error(f"Error parseando JSON reparado: {e_repair}. Respuesta original: '{respuesta_texto_crudo}'")
            return {
                "message_body": "El asistente IA devolvió una respuesta inesperada. Por favor, intenta reformular tu consulta o contacta a soporte.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {
                    "error_detalle": f"Fallo al parsear JSON de LLM: {str(e_json)}",
                    "respuesta_llm_cruda": respuesta_texto_crudo,
                    "mensaje_original": mensaje_usuario
                },
                "pedir_info": None,
                "botones": []
            }
    except Exception as e_parse:
        logger.error(f"Error general post-llamada a Gemini: {e_parse}", exc_info=True)
        return {
            "message_body": "Lo siento, hubo un error técnico al procesar la respuesta del asistente IA. Un humano revisará tu caso.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Fallo general post-LLM: {str(e_parse)}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }


def llamar_gemini_para_generacion_texto(
    system_prompt_especifico: str,
    user_prompt: str,
    temperature: float = 0.5
) -> str:
    """
    Llama a Gemini para una tarea de generación de texto simple, sin esperar JSON.
    """
    logger = logging.getLogger(__name__)
    try:
        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        location = "us-central1"

        if not project_id:
            logger.error("GOOGLE_PROJECT_ID no está configurado.")
            raise EnvironmentError("GOOGLE_PROJECT_ID no configurado.")

        vertexai.init(project=project_id, location=location)
        model = GenerativeModel("gemini-1.5-flash-001", system_instruction=[system_prompt_especifico])
        
        generation_config = GenerationConfig(
            temperature=temperature,
            max_output_tokens=2048
        )

        response = model.generate_content(
            [user_prompt],
            generation_config=generation_config,
        )
        
        if response.candidates and response.candidates[0].content.parts:
            return response.candidates[0].content.parts[0].text
        else:
            logger.error("Respuesta de Gemini para generación de texto vacía.")
            return ""

    except Exception as e:
        logger.error(f"Error en llamar_gemini_para_generacion_texto: {e}", exc_info=True)
        return ""


def llamar_gemini(
    mensaje_usuario: str = None,
    usuario: dict = None,
    historial: list = None,
    mensaje: str = None,
    timeout_seconds: int = 50,
    delay_warning_seconds: int = 8,
) -> dict:
    """Wrapper con timeout y logging para la llamada al LLM."""

    logger = logging.getLogger(__name__)
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_llamar_gemini_impl, mensaje_usuario, usuario, historial, mensaje)
        try:
            respuesta = future.result(timeout=timeout_seconds)
        except TimeoutError:
            logger.error(f"Llamada a Gemini superó {timeout_seconds}s")
            return {
                "message_body": "En este momento hay mucha demanda. ¿Querés intentar de nuevo?",
                "accion_backend": "no_accion",
                "datos_estructura": {"error_detalle": "timeout"},
                "pedir_info": None,
                "botones": []
            }

    elapsed = time.time() - start_time
    logger.info(f"Tiempo de respuesta de Gemini: {elapsed:.2f}s")
    # if elapsed > delay_warning_seconds and isinstance(respuesta, dict) and respuesta.get("message_body"):
    #     respuesta["message_body"] = "Sigo buscando la mejor respuesta, dame unos segundos más… " + respuesta["message_body"]

    return respuesta

if __name__ == '__main__':
    # Configurar logging básico para pruebas locales si no está ya configurado
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

    # Simular que las variables de entorno están seteadas para prueba local
    # ¡NO HACER ESTO EN PRODUCCIÓN! Usar un archivo .env o setearlas realmente.
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'path/to/your/service-account-file.json' # Reemplazar con path real o asegurar que ADC funcione
    os.environ['GOOGLE_PROJECT_ID'] = 'your-gcp-project-id' # Reemplazar con tu Project ID

    logger_main = logging.getLogger(__name__)
    logger_main.info("IMPORTANTE: Para probar la llamada real a Gemini, asegúrate de que las variables de entorno "
                 "GOOGLE_APPLICATION_CREDENTIALS (apuntando a tu JSON de credenciales) y "
                 "GOOGLE_PROJECT_ID estén configuradas correctamente en tu entorno local.")
    logger_main.info("Si GOOGLE_APPLICATION_CREDENTIALS no está, se intentará usar Application Default Credentials (ADC).")

    # Ejemplos de uso para prueba rápida
    usuario_ejemplo_pyme = {
        "nombre": "Juan Pérez",
        "tipo_entidad": "pyme",
        "ubicacion": "Calle Falsa 123",
        "contacto": {"email": "juan.perez@example.com", "telefono": "+5491122334455"}
    }
    historial_ejemplo = [
        {"role": "user", "parts": [{"text": "Hola"}]},
        {"role": "model", "parts": [{"text": "Hola Juan, ¿en qué puedo ayudarte?"}]}
    ]

    mensaje1 = "necesito un préstamo para terminar la finca"
    respuesta1 = llamar_gemini(mensaje1, usuario_ejemplo_pyme, historial_ejemplo)
    print(f"Mensaje: {mensaje1}\nRespuesta LLM (mock): {json.dumps(respuesta1, indent=2, ensure_ascii=False)}\n")

    usuario_ejemplo_municipio = {
        "nombre": "Ana Gómez",
        "tipo_entidad": "municipio",
        "ubicacion": "Barrio Sol",
        "contacto": {"web": "ana.gomez.vecinos.com"}
    }
    mensaje2 = "Hay una luz quemada en la esquina de San Martín y Rivadavia"
    respuesta2 = llamar_gemini(mensaje2, usuario_ejemplo_municipio, [])
    print(f"Mensaje: {mensaje2}\nRespuesta LLM (mock): {json.dumps(respuesta2, indent=2, ensure_ascii=False)}\n")

    mensaje3 = "Quiero saber el estado de mi reclamo"
    respuesta3 = llamar_gemini(mensaje3, usuario_ejemplo_municipio, [])
    print(f"Mensaje: {mensaje3}\nRespuesta LLM (mock): {json.dumps(respuesta3, indent=2, ensure_ascii=False)}\n")

    mensaje4 = "vender cosas"
    respuesta4 = llamar_gemini(mensaje4, usuario_ejemplo_pyme, [])
    print(f"Mensaje: {mensaje4}\nRespuesta LLM (mock): {json.dumps(respuesta4, indent=2, ensure_ascii=False)}\n")

"""
Este script incluye:
- El `JULES_SYSTEM_PROMPT`.
- La función `llamar_gemini` que actualmente está *mockeada*. Devuelve diferentes respuestas estructuradas basadas en keywords simples en el mensaje del usuario. Esto nos permitirá probar el flujo sin una API real de Gemini por ahora.
- Una sección `if __name__ == '__main__':` para probar rápidamente la función `llamar_gemini` con algunos ejemplos.

**Importante sobre el mock:**
El mock actual es muy básico. En una fase de desarrollo más avanzada, podríamos querer:
- Usar una librería de mocking más sofisticada.
- Tener un conjunto más amplio de ejemplos de entrada/salida.
- Simular errores de la API de Gemini (ej. timeouts, respuestas malformadas).

Por ahora, esto debería ser suficiente para continuar con la integración.
Una vez que tengamos acceso real y configuración para la API de Gemini, la sección mockeada se reemplazará con la llamada real usando `vertexai` o la librería correspondiente.
"""
