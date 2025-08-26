import json
import logging
import os
import re
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
import eventlet
from flask import current_app
from sqlalchemy.orm import sessionmaker
from services.chatbot_prompts import JULES_SYSTEM_PROMPT
from models import LlmInteractionLog
from database import db

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

# --- Logging helper -------------------------------------------------------

def _log_llm_interaction_async(app, chat_session_id: str, user_query: str, llm_response: dict) -> None:
    """Persist LLM interactions in a background greenlet."""
    with app.app_context():
        Session = sessionmaker(bind=db.engine)
        session = Session()
        try:
            entry = LlmInteractionLog(
                chat_session_id=chat_session_id,
                user_query=user_query,
                llm_response_raw=llm_response,
                status="pending_review",
            )
            session.add(entry)
            session.commit()
            app.logger.info(
                f"LLM interaction logged for session {chat_session_id}"
            )
        except Exception as e:
            app.logger.error(
                f"Failed to log LLM interaction for session {chat_session_id}: {e}",
                exc_info=True,
            )
            session.rollback()
        finally:
            session.close()

# --- Configuración de Seguridad de Gemini ---
# Allow benign personal information (e.g., phone numbers or emails) to pass
# through without being blocked by the safety system. The backend still
# validates and sanitizes user data before use.
GEMINI_SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_UNSPECIFIED: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    # HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY: HarmBlockThreshold.BLOCK_NONE, # Deprecated
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


def _repair_json_response(respuesta_texto_crudo: str) -> str:
    """
    Intenta reparar JSONs parcialmente truncados o con errores comunes.
    """
    fixed = respuesta_texto_crudo.strip()

    # Intenta corregir cadenas sin cerrar al final del JSON
    # Cubre casos como `"url": "https://...`
    fixed = re.sub(r'(":\s*)"([^"]*)$', r'\1"\2"', fixed)

    # Si 'botones' no tiene valor, asumimos una lista vacía.
    if re.search(r'"botones"\s*:\s*$', fixed, re.IGNORECASE):
        fixed += "[]"

    # Si 'botones' está truncado, intenta cerrarlo
    if re.search(r'"botones"\s*:\s*\[\s*\{', fixed, re.IGNORECASE) and not re.search(r'\}\s*\]\s*$', fixed):
         fixed += "}]"

    # Eliminar comas sobrantes antes de un cierre de objeto o array
    fixed = re.sub(r",\s*(\}|\])", r"\1", fixed)

    # Balancear llaves y corchetes
    brace_diff = fixed.count('{') - fixed.count('}')
    if brace_diff > 0:
        fixed += '}' * brace_diff

    bracket_diff = fixed.count('[') - fixed.count(']')
    if bracket_diff > 0:
        fixed += ']' * bracket_diff

    return fixed

def _llamar_gemini_impl(mensaje_usuario: str = None, usuario: dict = None, historial: list = None, mensaje: str = None, chat_session_id: str = None) -> tuple[dict, dict]:
    """Función interna que llama a Gemini y siempre devuelve una tupla (respuesta, contexto)."""
    logger = logging.getLogger(__name__)
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig, HarmCategory, HarmBlockThreshold, Content, Part

        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        location = "us-central1"

        if not project_id:
            logger.error("GOOGLE_PROJECT_ID no está configurado. No se puede inicializar Gemini GenAI.")
            raise EnvironmentError("GOOGLE_PROJECT_ID no configurado.")

        vertexai.init(project=project_id, location=location)

        model_name = "gemini-2.5-flash"
        model = GenerativeModel(model_name, system_instruction=[JULES_SYSTEM_PROMPT])

        if mensaje and not mensaje_usuario:
            mensaje_usuario = mensaje

        texto_mensaje = ""
        try:
            mensaje_usuario_obj = json.loads(mensaje_usuario)
            texto_mensaje = mensaje_usuario_obj.get("texto", str(mensaje_usuario_obj))
        except (json.JSONDecodeError, TypeError):
            texto_mensaje = str(mensaje_usuario)
            mensaje_usuario_obj = {"texto": texto_mensaje}

        chat_historial_limpio = _limpiar_historial_gemini(historial or [])
        formatted_history = []
        if chat_historial_limpio:
            for item in chat_historial_limpio:
                try:
                    # The history items are dicts like {'role': 'user', 'parts': [{'text': '...'}]}
                    # We need to convert them into Content objects.
                    if isinstance(item, dict) and 'role' in item and 'parts' in item:
                        # Part.from_dict is a convenient way to construct Part objects
                        parts = [Part.from_dict(p) for p in item['parts']]
                        formatted_history.append(Content(role=item['role'], parts=parts))
                except Exception as e:
                    logger.warning(f"Skipping malformed history item: {item}. Error: {e}")

        chat = model.start_chat(history=formatted_history)

        mensaje_actual_completo = (
            f"MENSAJE ACTUAL: {json.dumps(mensaje_usuario_obj, ensure_ascii=False)}\n\n"
            f"DATOS DEL USUARIO (para referencia): {json.dumps(usuario, ensure_ascii=False)}"
        )
        if usuario and usuario.get("datos_interpretados_archivo"):
            mensaje_actual_completo += f"\nDATOS EXTRAIDOS DE ARCHIVO ADJUNTO: {json.dumps(usuario.get('datos_interpretados_archivo'), ensure_ascii=False)}"

        logger.info(f"Enviando a Gemini ({model_name}). Mensaje: {texto_mensaje[:100]}...")

        generation_config = GenerationConfig(
            temperature=0.2, top_p=0.9, top_k=40,
            max_output_tokens=1024
        )

        response = chat.send_message(
            mensaje_actual_completo,
            generation_config=generation_config,
            safety_settings=GEMINI_SAFETY_SETTINGS,
        )

        logger.info(f"Respuesta recibida de Gemini. Candidates count: {len(response.candidates)}")
        if not response.candidates or not response.candidates[0].content.parts:
            logger.error("Gemini no devolvió contenido válido.")
            raise ValueError("Respuesta de Gemini sin contenido válido.")

        respuesta_texto_crudo = response.candidates[0].content.parts[0].text.strip()
        logger.info(f"Respuesta de Gemini (crudo): {respuesta_texto_crudo}")

    except (ImportError, EnvironmentError, ValueError) as e:
        logger.error(f"Error de configuración o llamada a Gemini API: {e}", exc_info=True)
        error_response = {
            "message_body": "Error de configuración del servicio de IA. Por favor, contacta al administrador.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": str(e), "mensaje_original": mensaje_usuario},
        }
        return error_response, {}
    except Exception as e_gemini_call:
        logger.error(f"Error inesperado en la llamada a Gemini API: {e_gemini_call}", exc_info=True)
        error_response = {
            "message_body": "Lo siento, no pude procesar tu solicitud en este momento debido a un error con el asistente IA.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": str(e_gemini_call), "mensaje_original": mensaje_usuario},
        }
        return error_response, {}

    try:
        if respuesta_texto_crudo.startswith("```json"):
            respuesta_texto_crudo = respuesta_texto_crudo[len("```json"):].strip()
        if respuesta_texto_crudo.endswith("```"):
            respuesta_texto_crudo = respuesta_texto_crudo[:-len("```")].strip()

        parsed_response = json.loads(respuesta_texto_crudo)
        return parsed_response, {} # Devuelve tupla en caso de éxito

    except json.JSONDecodeError as e_json:
        logger.warning(f"Fallo al parsear JSON de Gemini, intentando reparar. Error: {e_json}")
        fixed_json_str = _repair_json_response(respuesta_texto_crudo)
        try:
            parsed_response = json.loads(fixed_json_str)
            return parsed_response, {} # Devuelve tupla en caso de éxito con reparación
        except Exception as e_repair:
            logger.error(f"Error parseando JSON reparado: {e_repair}. Respuesta original: '{respuesta_texto_crudo}'")
            error_response = {
                "message_body": "El asistente IA devolvió una respuesta inesperada. Por favor, intenta reformular tu consulta.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {"error_detalle": str(e_repair), "respuesta_llm_cruda": respuesta_texto_crudo},
            }
            return error_response, {}


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
        model = GenerativeModel("gemini-1.5-flash", system_instruction=[system_prompt_especifico])
        
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
    app,
    mensaje_usuario: str = None,
    usuario: dict = None,
    historial: list = None,
    mensaje: str = None,
    timeout_seconds: int = 50,
    delay_warning_seconds: int = 8,
    chat_session_id: str = None,
) -> tuple[dict, dict]:
    """
    Wrapper con timeout y logging para la llamada al LLM.
    Devuelve siempre una tupla (respuesta_dict, contexto_dict).
    """
    logger = logging.getLogger(__name__)
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_llamar_gemini_impl, mensaje_usuario, usuario, historial, mensaje, chat_session_id)
        try:
            # _llamar_gemini_impl ahora devuelve una tupla
            respuesta, context_dict = future.result(timeout=timeout_seconds)
        except TimeoutError:
            logger.error(f"Llamada a Gemini superó los {timeout_seconds} segundos de timeout.")
            error_response = {
                "message_body": "El asistente IA está tardando más de lo normal en responder. Por favor, intenta de nuevo en unos momentos.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {"error_detalle": "timeout"},
                "pedir_info": None,
                "botones": []
            }
            return error_response, {}
        except Exception as e:
            logger.error(f"Excepción inesperada durante la ejecución de _llamar_gemini_impl: {e}", exc_info=True)
            error_response = {
                "message_body": "Ocurrió un error inesperado al comunicarse con el asistente de IA.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {"error_detalle": "Future execution exception"},
                "pedir_info": None,
                "botones": []
            }
            return error_response, {}


    elapsed = time.time() - start_time
    logger.info(f"Tiempo de respuesta de Gemini: {elapsed:.2f}s")

    if chat_session_id:
        user_query = mensaje_usuario or mensaje
        # Pasamos solo el diccionario de respuesta para el logging
        eventlet.spawn_n(_log_llm_interaction_async, app, chat_session_id, user_query, respuesta)

    logger.info(f"Retornando de llamar_gemini: {respuesta}")
    return respuesta, context_dict

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
    respuesta1, _ = llamar_gemini(None, mensaje1, usuario_ejemplo_pyme, historial_ejemplo)
    print(f"Mensaje: {mensaje1}\nRespuesta LLM (mock): {json.dumps(respuesta1, indent=2, ensure_ascii=False)}\n")

    usuario_ejemplo_municipio = {
        "nombre": "Ana Gómez",
        "tipo_entidad": "municipio",
        "ubicacion": "Barrio Sol",
        "contacto": {"web": "ana.gomez.vecinos.com"}
    }
    mensaje2 = "Hay una luz quemada en la esquina de San Martín y Rivadavia"
    respuesta2, _ = llamar_gemini(None, mensaje2, usuario_ejemplo_municipio, [])
    print(f"Mensaje: {mensaje2}\nRespuesta LLM (mock): {json.dumps(respuesta2, indent=2, ensure_ascii=False)}\n")

    mensaje3 = "Quiero saber el estado de mi reclamo"
    respuesta3, _ = llamar_gemini(None, mensaje3, usuario_ejemplo_municipio, [])
    print(f"Mensaje: {mensaje3}\nRespuesta LLM (mock): {json.dumps(respuesta3, indent=2, ensure_ascii=False)}\n")

    mensaje4 = "vender cosas"
    respuesta4, _ = llamar_gemini(None, mensaje4, usuario_ejemplo_pyme, [])
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
