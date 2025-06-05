# services/logic.py
import logging

logger = logging.getLogger(__name__)

def responder_chatboc(
    pregunta,
    user_obj=None,
    rubro_obj=None,
    session_obj=None,
    rubro_nombre_frontend=None,
    **kwargs
):
    """
    Lógica universal de ruteo por rubro. Nunca revienta si falta rubro.
    Toma el rubro de rubro_obj, user_obj.rubro (objeto Rubro o string), o rubro_nombre_frontend.
    """
    rubro_nombre = ""
    fuente = ""
    # 1. Buscamos el rubro en orden de prioridad
    if rubro_obj and getattr(rubro_obj, "nombre", None):
        rubro_nombre = str(rubro_obj.nombre).strip().lower()
        fuente = "rubro_obj.nombre"
    elif user_obj and getattr(user_obj, "rubro", None):
        # Puede ser Rubro o str (soportamos ambos)
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

    # 2. Ruteo real por tipo de rubro
    if rubro_nombre in ("municipios", "municipio"):
        from services.municipios import responder_municipio
        return responder_municipio(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)
    elif rubro_nombre in ("pymes", "pyme"):
        from services.pymes import responder_pyme
        return responder_pyme(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)
    # Ejemplo para agregar otros rubros:
    # elif rubro_nombre in ("escuelas", "escuela"):
    #     from services.escuelas import responder_escuela
    #     return responder_escuela(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)

    # 3. Si no hay match, loguea y responde con genérico
    logger.warning(
        f"[LOGIC] Rubro no soportado o faltante: '{rubro_nombre}' (fuente: {fuente}, user: {getattr(user_obj, 'id', None)})"
    )
    return {
        "respuesta": "Aún no está disponible la atención automática para este tipo de rubro. Contactanos por WhatsApp.",
        "fuente": "no_configurado",
    }
