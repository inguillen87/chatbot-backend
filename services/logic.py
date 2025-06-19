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
    user_obj=None,
    rubro_obj=None,
    session_obj=None,
    rubro_nombre_frontend=None,
    tipo_chat=None,
    anon_id=None,
    **kwargs,
):
    """Envía la consulta al handler correcto según el rubro y tipo de chat.

    Nunca se mezclan lógica ni estética de pymes y municipios. Si la
    información recibida no coincide (por ejemplo, rubro público pero
    ``tipo_chat`` de pyme), se ajustará y se registrará un error para que el
    frontend corrija su comportamiento.
    """

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
    elif user_obj and getattr(user_obj, "rubro", None):
        rubro_value = user_obj.rubro
        if hasattr(rubro_value, "nombre") and rubro_value.nombre:
            rubro_nombre = str(rubro_value.nombre).strip().lower()
            fuente = "user_obj.rubro.nombre"
        elif hasattr(rubro_value, "clave") and rubro_value.clave:
            rubro_nombre = str(rubro_value.clave).strip().lower()
            fuente = "user_obj.rubro.clave"
        else:
            rubro_nombre = str(rubro_value).strip().lower()
            fuente = "user_obj.rubro (str)"
    elif rubro_nombre_frontend:
        rubro_nombre = str(rubro_nombre_frontend).strip().lower()
        fuente = "rubro_nombre_frontend"
    else:
        rubro_nombre = ""
        fuente = "no_encontrado"

    logger.info(
        f"[LOGIC] Usando rubro: '{rubro_nombre}' (fuente: {fuente}, user: {getattr(user_obj, 'id', None)})"
    )

    # Si el rubro sugiere un tipo de chat distinto al provisto, ajustamos
    if rubro_nombre:
        esperado = "municipio" if rubro_nombre in RUBROS_PUBLICOS else "pyme"
        if tipo_chat and tipo_chat != esperado:
            logger.error(
                "ERROR: Se está intentando procesar pyme como municipio o viceversa"
            )
            logger.warning(
                f"Tipo de chat '{tipo_chat}' no coincide con el rubro '{rubro_nombre}'. AJUSTANDO a '{esperado}'."
            )
            tipo_chat = esperado
    elif tipo_chat not in ("municipio", "pyme"):
        raise ValueError(f"Tipo de chat inválido: {tipo_chat}")

    if not tipo_chat:
        raise ValueError("tipo_chat requerido")

    if tipo_chat == "municipio":
        from services.municipios import responder_municipio
        return responder_municipio(
            pregunta,
            user_obj,
            rubro_obj,
            session_obj=session_obj,
            anon_id=anon_id,
            **kwargs,
        )
    elif tipo_chat == "pyme":
        from services.pymes import responder_pyme
        return responder_pyme(
            pregunta,
            user_obj,
            rubro_obj,
            session_obj=session_obj,
            anon_id=anon_id,
            **kwargs,
        )
    else:
        raise ValueError(f"Tipo de chat no soportado: {tipo_chat}")
