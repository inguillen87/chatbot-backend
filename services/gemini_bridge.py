import json
import logging # Import logging
# Importar GenerativeModel si se va a usar directamente, o el cliente de Vertex AI
import os # Ensure os is imported for environment variables
from typing import Dict, Any, List, Optional # Import Optional

# Importar GenerativeModel si se va a usar directamente, o el cliente de Vertex AI
# from vertexai.preview.generative_models import GenerativeModel
# Por ahora, como no tenemos credenciales/API real, lo mockearemos.

JULES_SYSTEM_PROMPT = """Sos el asistente IA de una plataforma multi-entidad que atiende a Municipios y Pymes.
Tu tarea es recibir y entender mensajes de ciudadanos o clientes, interpretar reclamos, consultas o pedidos, y devolver siempre un JSON estructurado y profesional para que el backend ejecute la acción adecuada.

### Prioridades y Comportamiento General:
1.  **Acciones Específicas y Herramientas**:
    *   **Máxima Prioridad**: Si el mensaje del usuario es una solicitud explícita para usar una herramienta (`ejecutar_herramienta`), iniciar un reclamo (`iniciar_reclamo`, `crear_reclamo`), consultar un trámite (`info_tramite`), o cualquier otra acción directa claramente identificable, esta es tu acción principal. Extrae *todos* los datos relevantes del mensaje actual y del historial.
    *   **NO uses `derivar_humano` si una acción específica o herramienta es aplicable**, incluso si faltan algunos datos. En su lugar, usa `pedir_info`.
2.  **Pedir Información Faltante**:
    *   Si identificaste una acción clara (como `crear_reclamo` o `ejecutar_herramienta`) pero faltan datos cruciales (ej. `ubicacion` para un reclamo, `nombre_tramite` para una consulta, un parámetro específico para una herramienta), tu `accion_backend` debe ser la acción original (ej. `iniciar_reclamo`) y `pedir_info` debe solicitar el dato faltante (ej. `pedir_info: "ubicacion"` o `pedir_info: "parametro_herramienta_X"`).
    *   Formula la `respuesta_usuario` para pedir ese dato de forma concisa y clara.
3.  **Saludos y Small Talk**:
    *   Si el mensaje es un saludo simple ("hola", "buenas tardes", "gracias") o charla casual sin intención de acción, responde amablemente. Usa `accion_backend: "saludar"` para saludos y `accion_backend: "small_talk"` para charla casual.
    *   **NO uses `derivar_humano` para saludos o small talk.** Ofrece ayuda general con botones si es apropiado (ej. "Hacer un reclamo", "Consultar trámite").
4.  **Consultas Generales (Pregunta-Respuesta)**:
    *   Si es una pregunta general que no mapea a una acción específica o herramienta, intenta responderla de la mejor manera posible usando la información disponible (incluyendo el contexto del `USUARIO` y `HISTORIAL`). Usa `accion_backend: "responder_pregunta_general"`.
    *   **NO uses `derivar_humano` para preguntas generales si puedes ofrecer una respuesta informativa**, aunque sea parcial o indique dónde encontrar más información.
5.  **Ambigüedad y Aclaraciones**:
    *   Si la intención es ambigua pero podría ser una acción concreta, usa `pedir_info: "aclaracion"`. En `respuesta_usuario`, ofrece opciones claras o haz una pregunta específica para desambiguar la intención del usuario. Evita derivar prematuramente.
6.  **Derivar a Humano (Como Último Recurso Estricto)**:
    *   Solo usa `accion_backend: "derivar_humano"` si se cumple ALGUNA de estas condiciones ESTRICTAS:
        *   El usuario lo solicita EXPRESAMENTE (ej: "quiero hablar con una persona", "necesito un operador").
        *   Has intentado pedir información faltante (`pedir_info`) o aclarar (`pedir_info: "aclaracion"`) al menos una vez para una acción potencial, y el usuario sigue sin proporcionar la información necesaria o la situación no se resuelve.
        *   La consulta es EXTREMADAMENTE compleja, sensible (ej. emergencias médicas graves donde no puedes ayudar directamente más allá de sugerir llamar a números de emergencia), o claramente fuera de tu alcance como IA después de haber agotado otras opciones.
    *   **NUNCA uses `derivar_humano` como primera respuesta a un saludo, una pregunta general simple, o si una herramienta/acción podría ser relevante con un poco más de información.**

### Qué hacés (Detalles Específicos):
- Para reclamos (`accion_backend: "iniciar_reclamo"` o `accion_backend: "crear_reclamo"`):
    *   Siempre intenta obtener: `categoria`, `descripcion`, `ubicacion`.
    *   Si el usuario provee todos estos datos de una vez, usa `accion_backend: "crear_reclamo"` y llena todos los campos en `datos_estructura`.
    *   Si faltan, usa `accion_backend: "iniciar_reclamo"` y `pedir_info` para el primer dato faltante (ej. `pedir_info: "categoria"` si solo dijo "quiero reclamar").
- Respondés de forma personalizada según `target` (municipio/pyme).
- Sugerís adjuntos (foto, audio, GPS) si es relevante para la acción (ej. reclamo de bache), usualmente después de obtener la descripción.

### Uso de Herramientas Internas (`accion_backend: "ejecutar_herramienta"`):
- Si la consulta del usuario puede resolverse directamente con una herramienta interna (ej: consultar horario de recolección, buscar eventos), esta es la acción prioritaria.
- En `datos_estructura`, incluye `nombre_herramienta` y `parametros_herramienta` (con valores extraídos).
- Si faltan parámetros para una herramienta, usa `accion_backend: "ejecutar_herramienta"` (para mantener la intención), `pedir_info: "parametro_herramienta_X"` (donde X es el nombre del parámetro faltante), y en `datos_estructura` incluye `nombre_herramienta` y `faltan_parametros_herramienta`: ["nombre_del_parametro"]. La `respuesta_usuario` debe pedir ese parámetro.

### Entrada SIEMPRE
- mensaje_usuario: Texto plano.
- usuario: Objeto JSON (nombre, tipo_entidad, ubicación, contacto, etc).
- historial: Array JSON de turns previos (“pregunta”, “respuesta”, etc).

### SALIDA SIEMPRE (en JSON, nunca en texto ni en código Python):

{
  "respuesta_usuario": "...respuesta conversacional, profesional y directa...",
  "accion_backend": "crear_reclamo | consulta_estado_ticket | info_tramite | info_producto | consulta_credito | agregar_al_carrito | ver_carrito | finalizar_pedido | ejecutar_herramienta | derivar_humano | no_accion | small_talk | etc.",
  "datos_estructura": {
    "target": "municipio | pyme | ambos",
    "categoria": "... (ej: Alumbrado Público, Crédito Personal, Venta de Zapatillas)...",
    "descripcion": "... (detalle del reclamo/consulta/pedido)...",
    "ubicacion": "... (texto de la ubicación si aplica, ej: Calle Falsa 123, Barrio Centro)...",
    "coordenadas": {"lat": "...", "lon": "..."} | null,
    "nombre_usuario_detectado": "... (nombre del usuario si lo menciona)...",
    "telefono_detectado": "... (teléfono si lo menciona)...",
    "email_detectado": "... (email si lo menciona)...",
    "id_ticket_mencionado": "... (si el usuario menciona un N° de ticket)...",
    "nombre_producto_mencionado": "... (si aplica a consulta de producto)...",
    "cantidad_producto_mencionado": "...",
    "monto_solicitado": "...",
    "nombre_herramienta": "... (si accion_backend es ejecutar_herramienta)...",
    "parametros_herramienta": { } | null,
    "faltan_parametros_herramienta": [] | null
  },
  "pedir_info": null | "ubicacion" | "categoria" | "id_reclamo" | "producto" | "nombre_completo" | "telefono" | "email" | "descripcion_mas_detallada" | "monto_prestamo" | "aclaracion" | "parametro_herramienta_X" | ...,
  "botones": [ { "texto": "...", "id_accion": "opcional_id_para_backend" }, ... ]
}
Si para completar una `accion_backend` necesitás un dato específico que no está en el mensaje del usuario ni en el historial, especificá qué dato es en `pedir_info`. Por ejemplo, si para `crear_reclamo` falta la `ubicacion`, `pedir_info` debe ser `"ubicacion"`. Los `botones` deben ser sugerencias de acciones que el usuario podría querer tomar a continuación.

### Ejemplos Específicos

**Ejemplo 1: Reclamo Municipal (Luminaria)**
Usuario: “se quemó la luz en la calle Mitre y Belgrano”
JSON:
{
  "respuesta_usuario": "Entendido. Tomé nota de tu reclamo por una luminaria quemada en Mitre y Belgrano. El municipio lo revisará pronto. ¿Puedo ayudarte con algo más?",
  "accion_backend": "crear_reclamo",
  "datos_estructura": {
    "target": "municipio",
    "categoria": "Alumbrado Público",
    "descripcion": "Luz quemada",
    "ubicacion": "Mitre y Belgrano",
    "coordenadas": null
  },
  "pedir_info": null,
  "botones": [ {"texto": "Consultar estado reclamo", "id_accion": "consultar_estado_ticket"}, {"texto": "Hacer otro reclamo", "id_accion": "iniciar_reclamo"} ]
}

**Ejemplo 2: Consulta Crédito PYME**
Usuario: “necesito un préstamo para terminar la finca”
JSON:
{
  "respuesta_usuario": "¡Claro! Puedo ayudarte con eso. Para gestionar tu pedido de crédito para la finca, ¿podrías indicarme el monto aproximado que necesitas y cuál sería el destino específico del préstamo?",
  "accion_backend": "consulta_credito",
  "datos_estructura": {
    "target": "pyme",
    "categoria": "Crédito Agropecuario",
    "descripcion": "Necesita un préstamo para terminar la finca",
    "nombre_usuario_detectado": null
  },
  "pedir_info": "monto_y_destino_prestamo",
  "botones": [ {"texto": "Ver requisitos de créditos", "id_accion": "info_tramite_creditos"}, {"texto": "Cancelar consulta", "id_accion": "cancelar_flujo"} ]
}

**Ejemplo 3: Uso de Herramienta (Recolección de Residuos)**
Usuario: "¿Cuándo pasa el basurero por Av. Mayo 123?"
JSON:
{
  "respuesta_usuario": "Voy a verificar el horario de recolección para Av. Mayo 123. Un momento, por favor...",
  "accion_backend": "ejecutar_herramienta",
  "datos_estructura": {
    "target": "municipio",
    "nombre_herramienta": "consultar_recoleccion_por_direccion",
    "parametros_herramienta": {"direccion": "Av. Mayo 123"}
  },
  "pedir_info": null,
  "botones": []
}

**Ejemplo 4: Consulta Ambigua / Small Talk**
Usuario: "qué día horrible"
JSON:
{
  "respuesta_usuario": "Sí, parece que el clima no acompaña hoy. ¿Hay algo en lo que te pueda ayudar igualmente?",
  "accion_backend": "small_talk",
  "datos_estructura": { "target": "general" },
  "pedir_info": null,
  "botones": [ {"texto": "Hacer un reclamo"}, {"texto": "Consultar trámite"} ]
}

**Ejemplo 5: Solicitud de Corrección (Reclamo Municipal)**
Usuario: "No, la dirección del bache no es Av. Sol 456, es Av. Luna 789."
JSON:
{
  "respuesta_usuario": "Entendido. Corregí la dirección del bache a Av. Luna 789. ¿Hay algo más que quieras modificar o confirmamos así?",
  "accion_backend": "corregir_datos",
  "datos_estructura": {
    "target": "municipio",
    "campo_a_corregir": "ubicacion_reclamo", // O una clave más específica si el backend la espera
    "nuevo_valor": "Av. Luna 789",
    "contexto_original_del_reclamo": { // Opcional: para que el backend sepa a qué reclamo se refiere si hay ambigüedad
        "categoria": "Bacheo",
        "descripcion_previa": "Bache peligroso reportado..."
    }
  },
  "pedir_info": "confirmacion_tras_correccion", // O null si la respuesta_usuario ya lo pide
  "botones": [ {"texto": "Sí, confirmar con esta dirección"}, {"texto": "Necesito cambiar otra cosa"} ]
}

**Ejemplo 6: Solicitud de Corrección (Pedido PYME - Cantidad)**
Usuario: "Del vino tinto quiero 3 botellas, no 2."
JSON:
{
  "respuesta_usuario": "Anotado. Cambié la cantidad de Vino Tinto a 3 botellas. ¿Algo más?",
  "accion_backend": "corregir_datos_pedido", // O una acción más específica para pedidos
  "datos_estructura": {
    "target": "pyme",
    "item_a_corregir": "Vino Tinto", // Nombre o ID del producto
    "campo_a_corregir": "cantidad",
    "nuevo_valor": 3
  },
  "pedir_info": null,
  "botones": [ {"texto": "Ver carrito actualizado"}, {"texto": "Finalizar pedido"} ]
}

### Manejo de Ambigüedad y Correcciones
- Si el usuario indica que algo está mal (ej: "no, eso no es", "me equivoqué en el teléfono"), tu `accion_backend` debería ser "solicitar_correccion" o "editar_campo_especifico".
- En `datos_estructura`, intentá identificar qué campo necesita corrección.
- En `respuesta_usuario`, preguntá específicamente por el dato correcto o qué desea cambiar. Ej: "Entendido. ¿Cuál sería la dirección correcta?" o "¿Qué dato te gustaría modificar del reclamo?".
- Si el usuario provee directamente la corrección (Ej: "La calle es Rivadavia, no San Martín"), usá `accion_backend: "corregir_datos"` como en el Ejemplo 5.

Recordá: Siempre devolvé el JSON, nunca texto plano, nunca código. La estructura del JSON debe ser exactamente como se define en la sección "SALIDA SIEMPRE".
"""

def llamar_gemini(mensaje_usuario: str, usuario: dict, historial: list) -> dict:
    """
    Simula una llamada a la API de Gemini y devuelve una respuesta JSON estructurada.
    En una implementación real, aquí se haría la llamada a la API de Gemini.
    """
    # prompt_completo = f"""{JULES_SYSTEM_PROMPT}

    # MENSAJE DEL USUARIO: "{mensaje_usuario}"
    # USUARIO: {json.dumps(usuario, ensure_ascii=False, indent=2)}
    # HISTORIAL: {json.dumps(historial, ensure_ascii=False, indent=2)}
    # """
    # print("--- PROMPT ENVIADO A GEMINI (SIMULADO) ---") # Mantener comentado el print del prompt completo
    # print(prompt_completo)
    # print("------------------------------------------")

    # --- INICIO: LLAMADA REAL A GEMINI ---
    prompt_final_para_api = f"""{JULES_SYSTEM_PROMPT}

MENSAJE DEL USUARIO: "{mensaje_usuario}"
USUARIO: {json.dumps(usuario, ensure_ascii=False)}
HISTORIAL: {json.dumps(historial, ensure_ascii=False)}
"""
    # Configuración para asegurar que la respuesta sea JSON
    # Esto puede variar ligeramente según la versión de la librería o el modelo exacto.
    # Para gemini-1.5-pro-preview y la librería actual, se puede guiar por prompt
    # o usar generation_config si está disponible y bien documentado para JSON mode.
    # Por ahora, confiaremos en el prompt que explícitamente pide JSON.

    # Descomentar la siguiente línea e inicializar el modelo de Vertex AI
    # from vertexai.preview.generative_models import GenerativeModel, GenerationConfig # Añadir GenerationConfig

    # model = GenerativeModel("gemini-1.5-pro-preview")
    # Si se quiere forzar JSON output con GenerationConfig (si el modelo y SDK lo soportan bien):
    # generation_config = GenerationConfig(
    #     response_mime_type="application/json",
    # )
    # response = model.generate_content(prompt_final_para_api, generation_config=generation_config)

    # Llamada estándar (confiando en el prompt para el formato JSON):
    # response = model.generate_content(prompt_final_para_api)

    # --- INICIO: LLAMADA REAL A GEMINI ---
    logger = logging.getLogger(__name__)
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig, HarmCategory, HarmBlockThreshold
        from .google_auth_util import get_google_credentials

        # Obtener PROJECT_ID y LOCATION de variables de entorno o configuración
        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        location = os.environ.get("GOOGLE_LOCATION", "us-central1") # Default location

        if not project_id:
            logger.error("GOOGLE_PROJECT_ID no está configurado. No se puede inicializar Vertex AI.")
            raise EnvironmentError("GOOGLE_PROJECT_ID no configurado.")

        # Cargar credenciales usando el nuevo utilitario centralizado
        g_credentials = get_google_credentials()

        # Inicializar Vertex AI explícitamente con las credenciales
        vertexai.init(project=project_id, location=location, credentials=g_credentials)

        # Configuración del modelo y generación
        # Modelos disponibles: "gemini-1.0-pro", "gemini-1.5-pro-preview-0409", "gemini-1.5-flash-preview-0514" etc.
        # Usar un modelo reciente que soporte bien system instructions y JSON.
        model_name = "gemini-2.5-pro"

        model = GenerativeModel(
            model_name,
            system_instruction=[JULES_SYSTEM_PROMPT] # System prompt
        )

        # Construir el historial para el modelo Gemini
        # El historial debe ser una lista de objetos Content, alternando user y model.
        # JULES_SYSTEM_PROMPT ya se pasa como system_instruction.
        # El 'historial' que llega a esta función es una lista de dicts {"role": ..., "parts": ...}
        # que necesita ser adaptada si la API de Gemini espera un formato diferente para el historial de chat.
        # Por ahora, la API de generate_content con system_instruction y el último mensaje_usuario es más simple.
        # Si se necesita un historial de chat más complejo, se usaría model.start_chat(history=...)

        # Para una llamada simple con system prompt y el último mensaje:
        contents_for_api = [
            # JULES_SYSTEM_PROMPT ya está como system_instruction
            f"USUARIO: {json.dumps(usuario, ensure_ascii=False)}\nHISTORIAL PREVIO: {json.dumps(historial, ensure_ascii=False)}\nMENSAJE ACTUAL: \"{mensaje_usuario}\""
        ]

        logger.info(f"Enviando a Gemini ({model_name}). Mensaje: {mensaje_usuario[:100]}...")
        # logger.debug(f"Contenido completo enviado a Gemini API (sin system prompt): {contents_for_api}")

        # Configuración para intentar asegurar salida JSON y seguridad
        generation_config = GenerationConfig(
            temperature=0.2, # Más bajo para respuestas más deterministas/estructuradas
            top_p=0.95,
            top_k=40,
            max_output_tokens=2048, # Ajustar según necesidad
            # response_mime_type="application/json" # Solicitar JSON directamente
        )

        # Ajustes de seguridad (bloquear lo mínimo posible para no interferir con JSON)
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
            # stream=False # Por ahora no streaming
        )

        logger.info(f"Respuesta recibida de Gemini. Candidates count: {len(response.candidates)}")
        if not response.candidates:
            logger.error("Gemini no devolvió candidatos en la respuesta.")
            # Intentar obtener información de error si está disponible
            try:
                block_reason = response.prompt_feedback.block_reason
                block_reason_message = response.prompt_feedback.block_reason_message
                logger.error(f"Prompt feedback: block_reason={block_reason}, message='{block_reason_message}'")
                # Aquí podrías también revisar response.candidates[0].finish_reason y safety_ratings
                # si un candidato existe pero fue bloqueado.
            except Exception: # pragma: no cover
                pass # No hay info de error detallada
            raise ValueError("Respuesta de Gemini sin candidatos.")

        # Asumimos que el primer candidato tiene la respuesta.
        # El prompt pide explícitamente un JSON, así que response.text debería serlo.
        respuesta_texto_crudo = response.candidates[0].content.parts[0].text.strip()
        logger.debug(f"Texto crudo de Gemini: {respuesta_texto_crudo[:500]}...")

    except ImportError as ie:
        logger.error(f"Error importando Vertex AI: {ie}. Asegúrate que google-cloud-aiplatform está instalado.")
        # Fallback a un error simple o una acción segura
        return {
            "respuesta_usuario": "Error de configuración del servicio de IA. Por favor, contacta al administrador.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Fallo de importación Vertex AI: {str(ie)}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }
    except EnvironmentError as ee: # Para el error de GOOGLE_PROJECT_ID
        logger.error(f"Error de entorno para Vertex AI: {ee}")
        return {
            "respuesta_usuario": "Error de configuración del servicio de IA (entorno). Por favor, contacta al administrador.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": str(ee), "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }
    except Exception as e_gemini_call:
        logger.error(f"Error en la llamada a Gemini API: {e_gemini_call}", exc_info=True)
        # Considerar si la respuesta tiene información de error más específica
        error_detail_from_api = str(e_gemini_call)
        try: # Intentar obtener detalles de la respuesta si es un error de API
            if hasattr(e_gemini_call, 'message'): error_detail_from_api = e_gemini_call.message
        except: pass # pragma: no cover

        return {
            "respuesta_usuario": "Lo siento, no pude procesar tu solicitud en este momento debido a un error con el asistente IA. Intenta de nuevo más tarde.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Error API Gemini: {error_detail_from_api}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }
    # --- FIN: LLAMADA REAL A GEMINI ---

    try:
        # Limpiar espacios antes/después y parsear
        # Limpieza de ```json ... ``` y parseo
        if respuesta_texto_crudo.startswith("```json"):
            respuesta_texto_crudo = respuesta_texto_crudo[len("```json"):].strip()
            if respuesta_texto_crudo.endswith("```"):
                respuesta_texto_crudo = respuesta_texto_crudo[:-len("```")].strip()

        logger.debug(f"Texto de Gemini para parsear a JSON: {respuesta_texto_crudo[:500]}...")
        parsed_response = json.loads(respuesta_texto_crudo)
        return parsed_response

    except json.JSONDecodeError as e_json:
        logger.error(f"Error parseando JSON de Gemini: {e_json}. Respuesta cruda: '{respuesta_texto_crudo}'")
        return {
            "respuesta_usuario": "El asistente IA devolvió una respuesta inesperada. Por favor, intenta reformular tu consulta o contacta a soporte.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Fallo al parsear JSON de LLM: {str(e_json)}", "respuesta_llm_cruda": respuesta_texto_crudo, "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }
    except Exception as e_parse: # Otros errores durante el parseo o manejo
        logger.error(f"Error general post-llamada a Gemini: {e_parse}", exc_info=True)
        return {
            "respuesta_usuario": "Lo siento, hubo un error técnico al procesar la respuesta del asistente IA. Un humano revisará tu caso.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"error_detalle": f"Fallo general post-LLM: {str(e_parse)}", "mensaje_original": mensaje_usuario},
            "pedir_info": None, "botones": []
        }

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


def llamar_gemini_para_generacion_texto(
    system_prompt_especifico: str,
    user_prompt: str,
    model_name: Optional[str] = "gemini-2.5-pro", # Or another suitable model like gemini-1.0-pro
    temperature: float = 0.7, # Higher temperature for more creative/generative tasks
    max_output_tokens: int = 1024
) -> Optional[str]:
    """
    Llama a la API de Gemini para tareas generales de generación de texto con un system_prompt específico.
    Devuelve solo el texto generado o None en caso de error.
    """
    logger = logging.getLogger(__name__)
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig, HarmCategory, HarmBlockThreshold
        from .google_auth_util import get_google_credentials

        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        location = os.environ.get("GOOGLE_LOCATION", "us-central1")

        if not project_id:
            logger.error("GOOGLE_PROJECT_ID no está configurado para llamar_gemini_para_generacion_texto.")
            return None # Opcional: podría lanzar una excepción

        g_credentials = get_google_credentials()

        vertexai.init(project=project_id, location=location, credentials=g_credentials)

        model = GenerativeModel(
            model_name,
            system_instruction=[system_prompt_especifico] if system_prompt_especifico else None
        )

        generation_config = GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens
        )

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
        }

        logger.info(f"Llamando a Gemini ({model_name}) para generación de texto. User prompt: {user_prompt[:100]}...")
        response = model.generate_content(
            [user_prompt], # El user_prompt es el contenido principal
            generation_config=generation_config,
            safety_settings=safety_settings
        )

        if not response.candidates:
            logger.error("Gemini (generacion_texto) no devolvió candidatos.")
            return None

        respuesta_texto = response.candidates[0].content.parts[0].text.strip()
        logger.info(f"Gemini (generacion_texto) respondió: {respuesta_texto[:100]}...")
        return respuesta_texto

    except ImportError as ie:
        logger.error(f"Error importando Vertex AI en llamar_gemini_para_generacion_texto: {ie}")
    except EnvironmentError as ee:
        logger.error(f"Error de entorno para Vertex AI en llamar_gemini_para_generacion_texto: {ee}")
    except Exception as e:
        logger.error(f"Error en llamada a Gemini (generacion_texto): {e}", exc_info=True)

    return None


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
