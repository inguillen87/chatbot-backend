import difflib
import logging
from typing import List, Dict
from models import CatalogoItem
from .utils import limpiar_texto_base

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_LIMIT = 5


def buscar_catalogo_local(user_id: int, consulta: str, limite: int = DEFAULT_LOCAL_LIMIT) -> List[Dict]:
    """Busca productos en CatalogoItem usando coincidencia aproximada."""
    if not user_id or not consulta:
        return []
    if not hasattr(CatalogoItem, "query"):
        return []
    items = CatalogoItem.query.filter_by(user_id=user_id).all()
    if not items:
        return []
    consulta_norm = limpiar_texto_base(consulta)
    matches = []
    for it in items:
        nombre_norm = limpiar_texto_base(getattr(it, "nombre", ""))
        score = difflib.SequenceMatcher(None, consulta_norm, nombre_norm).ratio()
        matches.append((score, it))
    matches.sort(key=lambda x: x[0], reverse=True)
    seleccionados = [it for _, it in matches[:limite]]
    resultados = []
    for it in seleccionados:
        resultados.append({
            "nombre": it.nombre,
            "categoria": it.categoria,
            "descripcion": it.descripcion,
            "sku": it.sku,
            "unidad": it.unidad,
            "precio_str": it.precio,
            "cantidad": it.cantidad,
            "marca": it.marca,
        })
    logger.info(f"[CATALOGO_LOCAL] Consulta '{consulta}' -> {len(resultados)} resultados")
    return resultados
