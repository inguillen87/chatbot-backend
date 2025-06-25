import logging

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

from services.cohere_ai import get_cohere_response  # Asegúrate de que esta importación exista y sea correcta

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
Si la pregunta no encaja en ninguna de las categorías, clasifícala como 'general'.

INTENCIONES POSIBLES:
- iniciar_reclamo: El usuario quiere iniciar un reclamo o queja (ej. "quiero reclamar por una luz", "hay basura en la calle")
- consultar_estado_ticket: El usuario quiere saber el estado de un ticket o reclamo existente (ej. "estado de mi reclamo", "cómo va mi ticket 12345")
- consultar_impuestos: El usuario pregunta sobre impuestos municipales, tasas o pagos (ej. "quiero pagar mis impuestos", "deuda de tasas")
- consultar_tramite: El usuario pregunta sobre cómo realizar un trámite (ej. "requisitos para licencia de conducir", "cómo se hace habilitacion comercial")
- hablar_con_agente: El usuario quiere hablar con una persona (ej. "necesito hablar con alguien", "quiero hablar con una persona", "pasame con un humano", "me pasas con un operador", "quiero un representante")
- general: Cualquier otra consulta que no encaje en las anteriores.

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

Tu respuesta debe ser SÓLO una de las INTENCIONES POSIBLES.
"""

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
        # Limpia espacios y normaliza
        intencion_limpia = intencion.strip().lower()

        intents_validos = {
            "iniciar_reclamo",
            "consultar_estado_ticket",
            "consultar_impuestos",
            "consultar_tramite",
            "hablar_con_agente",
            "general",
        }

        if intencion_limpia not in intents_validos:
            logger.warning(
                f"[CLASIFICADOR INTENCION] Respuesta fuera de catálogo: '{intencion_limpia}'."
            )
            return "general"

        logger.info(
            f"[CLASIFICADOR INTENCION] Intención detectada: '{intencion_limpia}'"
        )
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
    anon_id = kwargs.get("anon_id", None)

    # ---------- Y ACÁ LO PASÁS SIEMPRE ----------
    if tipo_chat == "municipio":
        from services.municipios import responder_municipio
        return responder_municipio(
            pregunta,
            owner_user,
            rubro_obj,
            viewer_user=current_user,
            session_obj=session_obj,
            anon_id=anon_id,
            **kwargs,
        )
    elif tipo_chat == "pyme":
        from services.pymes import responder_pyme
        return responder_pyme(
            pregunta,
            owner_user,
            rubro_obj,
            viewer_user=current_user,
            session_obj=session_obj,
            anon_id=anon_id,
            **kwargs,
        )
    else:
        raise ValueError(f"Tipo de chat no soportado: {tipo_chat}")
