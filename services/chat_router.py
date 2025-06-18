from importlib import import_module

HANDLER_MAP = {
    "pyme": "services.pymes.responder_pyme",
    "municipio": "services.municipios.responder_municipio",
}


def get_chat_handler(tipo_chat: str):
    """Devuelve la función manejadora correspondiente al tipo de chat."""
    if not tipo_chat:
        raise ValueError("tipo_chat requerido")
    path = HANDLER_MAP.get(tipo_chat)
    if not path:
        raise ValueError(f"Tipo de chat no soportado: {tipo_chat}")
    module_name, func_name = path.rsplit(".", 1)
    module = import_module(module_name)
    return getattr(module, func_name)
