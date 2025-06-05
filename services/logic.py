# services/logic.py
import logging

logger = logging.getLogger(__name__)

def responder_chatboc(pregunta, user_obj=None, rubro_obj=None, session_obj=None, rubro_nombre_frontend=None, **kwargs):
    """
    Lógica universal de ruteo por rubro. Nunca revienta si falta rubro. 
    Toma el rubro de rubro_obj, de user_obj o de rubro_nombre_frontend (el que primero encuentre).
    """
    # 1. Normaliza el nombre de rubro
    rubro_nombre = ""
    if rubro_obj and getattr(rubro_obj, "nombre", None):
        rubro_nombre = rubro_obj.nombre.strip().lower()
    elif user_obj and getattr(user_obj, "rubro", None):
        rubro_nombre = user_obj.rubro.strip().lower()
    elif rubro_nombre_frontend:
        rubro_nombre = rubro_nombre_frontend.strip().lower()

    # 2. Ruteo por tipo de rubro
    if rubro_nombre in ("municipios", "municipio"):
        from services.municipios import responder_municipio
        return responder_municipio(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)
    elif rubro_nombre in ("pymes", "pyme"):
        from services.pymes import responder_pyme
        return responder_pyme(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)
    # Ejemplo de rubros nuevos:
    # elif rubro_nombre in ("escuelas", "escuela"):
    #     from services.escuelas import responder_escuela
    #     return responder_escuela(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)

    # Si no hay match, loguea y responde con genérico
    logger.warning(f"[LOGIC] Rubro no soportado o faltante: '{rubro_nombre}' (user: {getattr(user_obj, 'id', None)})")
    return {
        "respuesta": "Aún no está disponible la atención automática para este tipo de rubro. Contactanos por WhatsApp.",
        "fuente": "no_configurado"
    }
