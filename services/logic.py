# services/logic.py
import logging

logger = logging.getLogger(__name__)

def responder_chatboc(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    rubro_nombre = (rubro_obj.nombre if rubro_obj else "").strip().lower()

    if rubro_nombre == "municipios":
        from services.municipios import responder_municipio
        return responder_municipio(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)
    elif rubro_nombre == "pymes" or rubro_nombre == "pyme":
        from services.pymes import responder_pyme
        return responder_pyme(pregunta, user_obj, rubro_obj, session_obj=session_obj, **kwargs)
    # Para más rubros, seguí este patrón:
    # elif rubro_nombre == "escuelas":
    #     from services.escuelas import responder_escuela
    #     return responder_escuela(...)
    else:
        logger.warning(f"[LOGIC] Rubro no soportado: '{rubro_nombre}'")
        return {"respuesta": "Aún no está disponible la atención automática para este tipo de rubro. Contactanos por WhatsApp.", "fuente": "no_configurado"}
