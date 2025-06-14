import json
import logging
import random
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
    if not nombre or not user_id:
        return json.dumps({"respuesta": "Falta especificar el producto."})
    item = CatalogoItem.query.filter(
        CatalogoItem.user_id == user_id,
        CatalogoItem.nombre.ilike(f"%{nombre}%")
    ).first()
    if item and item.cantidad:
        return json.dumps({"respuesta": f"Contamos con {item.cantidad} unidades de {item.nombre}."})
    return json.dumps({"respuesta": f"No se encontró stock de '{nombre}'."})


def calcular_costo_envio(ciudad):
    if not ciudad:
        return json.dumps({"respuesta": "Falta la ciudad de destino."})
    costo = random.randint(500, 1500)
    return json.dumps({"respuesta": f"El costo estimado de envío a {ciudad} es ${costo}."})


TOOL_REGISTRY_PYME = {
    "consultar_horario": {"funcion": consultar_horario_actual, "parametros": ["user"]},
    "verificar_stock": {"funcion": verificar_stock_producto, "parametros": ["nombre", "user_id"]},
    "calcular_envio": {"funcion": calcular_costo_envio, "parametros": ["ciudad"]},
}
