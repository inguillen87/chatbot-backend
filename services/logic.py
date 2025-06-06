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

def responder_chatboc(
    pregunta,
    user_obj=None,
    rubro_obj=None,
    session_obj=None,
    rubro_nombre_frontend=None,
    **kwargs
):
    # 1. Detectar nombre de rubro (universal)
    rubro_nombre = ""
    fuente = ""
    if rubro_obj and getattr(rubro_obj, "nombre", None):
        rubro_nombre = str(rubro_obj.nombre).strip().lower()
        fuente = "rubro_obj.nombre"
    elif user_obj and getattr(user_obj, "rubro", None):
        rubro_value = user_obj.rubro
        if hasattr(rubro_value, "nombre"):
            rubro_nombre = str(rubro_value.nombre).strip().lower()
            fuente = "user_obj.rubro.nombre"
        else:
            rubro_nombre = str(rubro_value).strip().lower()
            fuente = "user_obj.rubro (str)"
    elif rubro_nombre_frontend:
        rubro_nombre = str(rubro_nombre_frontend).strip().lower()
        fuente = "rubro_nombre_frontend"
    else:
        rubro_nombre = ""
        fuente = "no_encontrado"

    logger.info(f"[LOGIC] Usando rubro: '{rubro_nombre}' (fuente: {fuente}, user: {getattr(user_obj, 'id', None)})")

    # 2. Ruteo según tipo de rubro
    if rubro_nombre in RUBROS_PUBLICOS:
        from services.municipios import responder_municipio
        return responder_municipio(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)
    else:
        from services.pymes import responder_pyme
        return responder_pyme(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)
