import json
import logging
import random
import difflib
from datetime import datetime
from models import CatalogoItem

logger = logging.getLogger(__name__)


def consultar_horario_actual(user):
    if not user or not getattr(user, "horario", None):
        return json.dumps({"respuesta": "No hay horario configurado."})
    horario = user.horario
    now = datetime.now()
    mensaje = f"Nuestro horario de atención es {horario}."
    return json.dumps({"respuesta": mensaje})


def verificar_stock_producto(nombre, user_id):
    """Busca stock del producto con coincidencia difusa y sugiere alternativas."""
    if not nombre or not user_id:
        return json.dumps({"respuesta": "Falta especificar el producto."})

    items = CatalogoItem.query.filter(CatalogoItem.user_id == user_id).all()
    if not items:
        return json.dumps({"respuesta": f"No se encontró stock de '{nombre}'."})

    nombres = [it.nombre for it in items if getattr(it, "nombre", None)]
    coincidencias = difflib.get_close_matches(nombre, nombres, n=3, cutoff=0.6)

    if coincidencias:
        item = next((i for i in items if i.nombre == coincidencias[0]), None)
        if item:
            cant = getattr(item, "cantidad", None)
            if cant and str(cant) not in {"0", "0.0", "0"}:
                return json.dumps({"respuesta": f"Contamos con {cant} unidades de {item.nombre}."})
            alternativas = [c for c in coincidencias[1:] if c != item.nombre]
            if alternativas:
                return json.dumps({"respuesta": f"No tenemos stock de {item.nombre}. Quizás te interesen: {', '.join(alternativas)}."})
            return json.dumps({"respuesta": f"No tenemos stock de {item.nombre}."})

    if nombres:
        suger = ", ".join(nombres[:3])
        return json.dumps({"respuesta": f"No se encontró stock de '{nombre}'. Productos disponibles: {suger}."})
    return json.dumps({"respuesta": f"No se encontró stock de '{nombre}'."})


def calcular_costo_envio(ciudad):
    if not ciudad:
        return json.dumps({"respuesta": "Falta la ciudad de destino."})
    costo = random.randint(500, 1500)
    return json.dumps({"respuesta": f"El costo estimado de envío a {ciudad} es ${costo}."})


TOOL_REGISTRY_PYME = {
    "consultar_horario": {
        "funcion": consultar_horario_actual,
        "descripcion": "Informa el horario de atención de la empresa.",
        "parametros": {"user": {"type": "object", "description": "Instancia del usuario pyme"}},
    },
    "verificar_stock": {
        "funcion": verificar_stock_producto,
        "descripcion": "Verifica la disponibilidad de un producto en el catálogo.",
        "parametros": {
            "nombre": {"type": "string", "description": "Nombre del producto"},
            "user_id": {"type": "integer", "description": "ID de la pyme"}
        },
    },
    "calcular_envio": {
        "funcion": calcular_costo_envio,
        "descripcion": "Calcula un costo estimado de envío a una ciudad dada.",
        "parametros": {"ciudad": {"type": "string", "description": "Ciudad de destino"}},
    },
}
