import logging
from flask import current_app # Para logging y config
from models import ArchivoAdjunto, AnalisisArchivo, db # db para la sesión
from services.interpretacion_service import interpretacion_service
from services.archivo_service import archivo_service
# servicio_tickets se importa/usa en los handlers específicos (municipios.py, pymes.py)

logger = logging.getLogger(__name__)

# Rubros que deben usar la lógica de municipio/ente público
RUBROS_PUBLICOS = {
    "municipio",
    "municipios",
    "ong",
    "gobierno",
    "hospital_publico",
    "entidad_publica",
    # Agregá acá los que consideres públicos
}

def normalizar_rubro(rubro) -> str:
    """Devuelve el nombre del rubro en minúsculas."""
    if not rubro:
        return ""
    if isinstance(rubro, str):
        return rubro.strip().lower()
    if hasattr(rubro, "nombre") and getattr(rubro, "nombre"):
        return str(rubro.nombre).strip().lower()
    if hasattr(rubro, "clave") and getattr(rubro, "clave"):
        return str(rubro.clave).strip().lower()
    return str(rubro).strip().lower()


def es_rubro_publico(rubro) -> bool:
    """Indica si un rubro pertenece a ``RUBROS_PUBLICOS``."""
    return normalizar_rubro(rubro) in RUBROS_PUBLICOS

try:
    from services.cohere_ai import get_cohere_response
except Exception:  # pragma: no cover - fallback for tests
    from services.cohere_ai import robust_chat as get_cohere_response

# --- Utilidades para small talk ---
PROMPT_DETECT_SMALL_TALK = """
Analiza la FRASE DEL USUARIO y responde únicamente "SI" o "NO".
Responde "SI" si la frase es simplemente una charla casual o un saludo sin una
solicitud específica. Responde "NO" en caso contrario.

FRASE DEL USUARIO: "{pregunta_usuario}"
"""

PROMPT_RESPUESTA_SMALL_TALK = """
Responde de manera cordial y breve en español a la FRASE DEL USUARIO y luego
ofrece tu ayuda.

FRASE DEL USUARIO: "{pregunta_usuario}"
"""


def detectar_small_talk_con_llm(pregunta: str) -> bool:
    """Devuelve ``True`` si la pregunta parece small talk según el LLM."""
    prompt = PROMPT_DETECT_SMALL_TALK.format(pregunta_usuario=pregunta)
    try:
        decision = get_cohere_response(
            message=prompt,
            preamble="Eres un clasificador de small talk. Responde solo SI o NO.",
        )
        return decision.strip().upper().startswith("SI")
    except Exception as e:
        logger.error(f"[SMALL_TALK] Error detectando small talk: {e}")
        return False


def generar_respuesta_small_talk(pregunta: str) -> str:
    """Genera una respuesta cordial para una frase de small talk."""
    prompt = PROMPT_RESPUESTA_SMALL_TALK.format(pregunta_usuario=pregunta)
    try:
        return get_cohere_response(
            message=prompt,
            preamble="Eres un asistente amigable que mantiene charlas casuales.",
        ).strip()
    except Exception as e:
        logger.error(f"[SMALL_TALK] Error generando respuesta: {e}")
        return "¡Hola! ¿En qué puedo ayudarte?"

# Puedes ajustar este prompt según las intenciones que quieras clasificar
PROMPT_CLASIFICACION_INTENCION = """
Analiza la siguiente PREGUNTA DEL USUARIO y clasifica su INTENCIÓN.
Responde ÚNICAMENTE con una de las INTENCIONES POSIBLES de la lista.
Si la pregunta no encaja claramente en ninguna de las categorías específicas, clasifícala como 'general'.

INTENCIONES POSIBLES:
- iniciar_reclamo: El usuario expresa deseo de presentar una queja, problema o denuncia.
    Ejemplos: "quiero reclamar por una luz quemada", "hay mucha basura en la esquina de mi casa", "el servicio de agua no funciona", "tengo un problema con el pavimento"
- consultar_estado_ticket: El usuario quiere saber el estado o progreso de un ticket, reclamo o trámite ya iniciado.
    Ejemplos: "cómo va mi reclamo 12345?", "quisiera saber el estado de mi gestión", "alguna novedad sobre el ticket M-5567?"
- consultar_impuestos: El usuario pregunta sobre impuestos municipales, tasas, facturas, boletas o formas de pago relacionadas.
    Ejemplos: "quiero pagar mis impuestos", "cómo pago la tasa municipal?", "dónde puedo ver mi boleta de ABL?", "cuánto debo de patentes?"
- consultar_tramite: El usuario pregunta sobre cómo realizar un trámite, requisitos, horarios o lugares para trámites municipales.
    Ejemplos: "requisitos para licencia de conducir", "cómo se hace la habilitación comercial?", "dónde saco el certificado de domicilio?", "horario para renovar DNI"
- hacer_sugerencia: El usuario quiere proponer una idea, mejora o dar una opinión constructiva.
    Ejemplos: "deberían poner más bancos en la plaza", "sugiero que mejoren la iluminación del parque", "tengo una idea para el tránsito"
- hablar_con_agente: El usuario solicita explícitamente hablar con una persona, empleado o representante.
    Ejemplos: "necesito hablar con alguien", "quiero hablar con una persona", "pasame con un humano", "me pasas con un operador?", "quiero un representante"
- general: Cualquier otra consulta que no encaje en las anteriores, o preguntas generales sobre el municipio.
    Ejemplos: "cuál es el teléfono del intendente?", "historia de la ciudad", "eventos culturales este fin de semana"

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

INTENCIÓN: """

def _clasificar_intencion_con_llm(pregunta: str) -> str:
    """
    Clasifica la intención de la pregunta del usuario utilizando un modelo de lenguaje.
    """
    logger.info(f"[CLASIFICADOR INTENCION] Clasificando intención para: '{pregunta}'")

    prompt = PROMPT_CLASIFICACION_INTENCION.format(pregunta_usuario=pregunta)

    try:
        # Asegúrate de que get_cohere_response esté correctamente configurado
        # para tu API de Cohere o el LLM que estés usando.
        # El preamble aquí es opcional, pero ayuda a guiar el LLM.
        intencion = get_cohere_response(
            message=prompt, 
            preamble="Eres un clasificador de intención de usuario. Responde solo con la intención clasificada."
        )
        # Limpia cualquier espacio en blanco o caracter especial
        intencion_limpia = intencion.strip().lower()
        logger.info(f"[CLASIFICADOR INTENCION] Intención detectada: '{intencion_limpia}'")
        return intencion_limpia
    except Exception as e:
        logger.error(f"[CLASIFICADOR INTENCION] Error al clasificar intención con LLM: {e}")
        return "general" # Retorna una intención por defecto en caso de error

# ... otras funciones que ya tengas en logic.py (como responder_chatboc)
def responder_chatboc(
    pregunta,
    owner_user=None,
    current_user=None,
    rubro_obj=None,
    session_obj=None,
    rubro_nombre_frontend=None,
    tipo_chat=None,
    anon_id=None,
    chat_session_uuid=None, # Nuevo parámetro
    **kwargs,
):
    """Envía la consulta al handler correcto según el rubro y tipo de chat."""

    # 1. Detectar nombre de rubro (universal)
    rubro_nombre = ""
    fuente = ""
    if rubro_obj:
        if getattr(rubro_obj, "nombre", None):
            rubro_nombre = str(rubro_obj.nombre).strip().lower()
            fuente = "rubro_obj.nombre"
        elif getattr(rubro_obj, "clave", None):
            rubro_nombre = str(rubro_obj.clave).strip().lower()
            fuente = "rubro_obj.clave"
    elif owner_user and getattr(owner_user, "rubro", None):
        rubro_value = owner_user.rubro
        if hasattr(rubro_value, "nombre") and rubro_value.nombre:
            rubro_nombre = str(rubro_value.nombre).strip().lower()
            fuente = "owner_user.rubro.nombre"
        elif hasattr(rubro_value, "clave") and rubro_value.clave:
            rubro_nombre = str(rubro_value.clave).strip().lower()
            fuente = "owner_user.rubro.clave"
        else:
            rubro_nombre = str(rubro_value).strip().lower()
            fuente = "owner_user.rubro (str)"
    elif rubro_nombre_frontend:
        rubro_nombre = str(rubro_nombre_frontend).strip().lower()
        fuente = "rubro_nombre_frontend"
    else:
        rubro_nombre = ""
        fuente = "no_encontrado"

    logger.info(
        f"[LOGIC] Usando rubro: '{rubro_nombre}' (fuente: {fuente}, user: {getattr(owner_user, 'id', None)})"
    )

    # Si el rubro indica un tipo específico de lógica, lo usamos siempre
    if rubro_nombre:
        esperado = "municipio" if es_rubro_publico(rubro_nombre) else "pyme"
        if tipo_chat and tipo_chat != esperado:
            logger.info(
                "Ajustando tipo_chat de '%s' a '%s' por rubro público '%s'",
                tipo_chat,
                esperado,
                rubro_nombre,
            )
        tipo_chat = esperado
    elif tipo_chat not in ("municipio", "pyme"):
        raise ValueError(f"Tipo de chat inválido: {tipo_chat}")

    if not tipo_chat:
        raise ValueError("tipo_chat requerido")

    # ---------- ACÁ DEFINÍS EL anon_id ----------
    # anon_id ya es un parámetro de la función responder_chatboc, no necesita extraerse de kwargs si se pasa directamente.
    # anon_id = kwargs.get("anon_id", None)

    # --- Inicio: Lógica de manejo de archivo adjunto y su análisis ---
    uploaded_file_info = kwargs.get("uploaded_file_info")
    datos_interpretados_de_archivo = None
    archivo_id_para_asociar_al_ticket = None

    if uploaded_file_info and isinstance(uploaded_file_info, dict) and uploaded_file_info.get("id"):
        archivo_id = uploaded_file_info.get("id")
        # uploaded_file_info["id"] es el ID del ArchivoAdjunto.
        current_app.logger.info(f"[LOGIC] Procesando uploaded_file_info para ArchivoAdjunto ID: {archivo_id}")

        archivo_obj = db.session.query(ArchivoAdjunto).get(archivo_id)

        if archivo_obj:
            archivo_id_para_asociar_al_ticket = archivo_id

            if archivo_obj.analisis and archivo_obj.analisis.estado_analisis == "completado":
                current_app.logger.info(f"[LOGIC] Análisis encontrado y completado para ArchivoAdjunto ID: {archivo_id}")

                user_id_actual = owner_user.id if owner_user else None

                datos_interpretados_de_archivo = interpretacion_service.interpretar_analisis_para_datos_ticket(
                    analisis_archivo=archivo_obj.analisis,
                    tipo_contexto=tipo_chat,
                    user_id=user_id_actual
                )
                if datos_interpretados_de_archivo:
                    current_app.logger.info(f"[LOGIC] Datos interpretados del archivo: {datos_interpretados_de_archivo}")
                    # Se pasarán a los handlers via kwargs
            elif archivo_obj.analisis and archivo_obj.analisis.estado_analisis in ["pendiente", "procesando"]:
                current_app.logger.info(f"[LOGIC] Análisis para ArchivoAdjunto ID: {archivo_id} aún está '{archivo_obj.analisis.estado_analisis}'. El archivo se asociará si se crea un ticket.")
            else:
                current_app.logger.info(f"[LOGIC] No hay análisis completado para ArchivoAdjunto ID: {archivo_id} (estado: {archivo_obj.analisis.estado_analisis if archivo_obj.analisis else 'sin análisis'}). El archivo se asociará si se crea un ticket.")
        else:
            current_app.logger.warning(f"[LOGIC] No se encontró ArchivoAdjunto con ID: {archivo_id} desde uploaded_file_info.")
            archivo_id_para_asociar_al_ticket = None # No podemos asociar si no encontramos el archivo

    # Actualizar kwargs para pasar la información a los handlers específicos
    kwargs["datos_interpretados_archivo"] = datos_interpretados_de_archivo
    kwargs["archivo_id_para_asociar"] = archivo_id_para_asociar_al_ticket
    # Limpiar uploaded_file_info de kwargs para no pasarlo más allá si ya se procesó.
    # Opcional, pero puede ser más limpio.
    if "uploaded_file_info" in kwargs:
        del kwargs["uploaded_file_info"]
    # --- Fin: Lógica de manejo de archivo adjunto ---

    # ---------- Y ACÁ LO PASÁS SIEMPRE ----------
    if tipo_chat == "municipio":
        from services.municipios import responder_municipio
        return responder_municipio(
            pregunta,
            owner_user,
            rubro_obj,
            viewer_user=current_user,
            session_obj=session_obj, # El session_obj de flask
            anon_id=anon_id,
            chat_session_uuid=chat_session_uuid, # Pasar aquí
            **kwargs, # kwargs ahora contiene datos_interpretados_archivo y archivo_id_para_asociar
        )
    elif tipo_chat == "pyme":
        from services.pymes import responder_pyme
        return responder_pyme(
            pregunta,
            owner_user,
            rubro_obj,
            viewer_user=current_user,
            session_obj=session_obj, # El session_obj de flask
            anon_id=anon_id,
            chat_session_uuid=chat_session_uuid, # Pasar aquí
            **kwargs, # kwargs ahora contiene datos_interpretados_archivo y archivo_id_para_asociar
        )
    else:
        raise ValueError(f"Tipo de chat no soportado: {tipo_chat}")
