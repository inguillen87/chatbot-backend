import logging
import os
from typing import List, Optional
from .qdrant_utils import get_qdrant_client
from .cohere_ai import embed_textos
from qdrant_client.http import models as qdrant_models
from .utils import limpiar_texto_base

# Permite ajustar el número de resultados devueltos desde una variable de entorno.
DEFAULT_SEARCH_LIMIT = int(os.getenv("CATALOGO_RESULT_LIMIT", "5"))

logger = logging.getLogger(__name__)

def buscar_catalogo_qdrant(
    user_id: Optional[int],
    pregunta: str,
    limite: int = DEFAULT_SEARCH_LIMIT,
    score_min: float = 0.30
) -> List[qdrant_models.ScoredPoint]:
    qdrant_cli = get_qdrant_client()
    if not qdrant_cli:
        logger.error("[QDRANT SEARCH] No se pudo obtener cliente Qdrant.")
        return []

    try:
        pregunta_limpia = limpiar_texto_base(pregunta.strip())
        if not pregunta_limpia:
            logger.warning("[QDRANT SEARCH] Pregunta para búsqueda vacía después de limpiar.")
            return []
        vector_pregunta_lista = embed_textos([pregunta_limpia], input_type="search_query")
        if not vector_pregunta_lista or not isinstance(vector_pregunta_lista[0], list):
            logger.error(f"[QDRANT SEARCH] No se pudo generar vector para pregunta: '{pregunta_limpia}'")
            return []
        vector_q = vector_pregunta_lista[0]
    except Exception as e_embed:
        logger.error(f"[QDRANT SEARCH] Error generando embedding para pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
        return []

    try:
        search_filter = None
        id_log = f"user_id {user_id}" if user_id is not None else "ANONIMO"

        if user_id is not None:
            search_filter = qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="user_id",
                        match=qdrant_models.MatchValue(value=user_id)
                    )
                ]
            )
            logger.info(f"[QDRANT SEARCH] Buscando en catálogo PRIVADO para {id_log}, pregunta '{pregunta_limpia}'")
        else:
            logger.info(f"[QDRANT SEARCH] Buscando en catálogo GENERAL para {id_log}, pregunta '{pregunta_limpia}'")
            # Si tuvieras un campo catálogo_público, acá le podés meter ese filtro

        resultados = qdrant_cli.search(
            collection_name="catalogos",
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min
        )
        logger.info(f"[QDRANT SEARCH] Pregunta: '{pregunta}', Hits: {len(resultados)}, Scores: {[getattr(r, 'score', 0) for r in resultados[:3]]}")
        return resultados

    except Exception as e_qdrant:
        id_log = f"user_id {user_id}" if user_id is not None else "ANONIMO"
        logger.error(f"[QDRANT SEARCH] Error buscando en Qdrant para {id_log}, pregunta '{pregunta_limpia}': {e_qdrant}", exc_info=True)
        return []

def _ordenar_por_precio(resultados: List[qdrant_models.ScoredPoint]) -> List[qdrant_models.ScoredPoint]:
    """Ordena la lista de resultados por el campo ``precio_float`` ascendente."""
    def _precio(hit: qdrant_models.ScoredPoint) -> float:
        p = getattr(hit, "payload", None) or {}
        try:
            return float(p.get("precio_float"))
        except (TypeError, ValueError):
            return float("inf")

    return sorted(resultados, key=_precio)


def armar_respuesta_legible(
    resultados_qdrant: List[qdrant_models.ScoredPoint],
    max_items: int = DEFAULT_SEARCH_LIMIT,
    order_by: str | None = None,
) -> str:
    """Convierte una lista de resultados Qdrant en un texto legible."""

    if not resultados_qdrant:
        logger.info("[QDRANT FORMAT] No hay resultados Qdrant para formatear.")
        return "No encontré productos para mostrar en este momento."

    if order_by == "price":
        resultados_qdrant = _ordenar_por_precio(resultados_qdrant)
    else:
        resultados_qdrant = sorted(
            resultados_qdrant, key=lambda r: getattr(r, "score", 0), reverse=True
        )

    lineas: List[str] = []
    for idx, hit in enumerate(resultados_qdrant[:max_items], 1):
        p = getattr(hit, "payload", {}) or {}

        nombre = str(p.get("nombre") or p.get("title") or "Producto sin nombre").strip()
        sku = str(p.get("sku") or "").strip()
        categoria = str(p.get("categoria_qdrant") or p.get("categoria") or "").strip()
        moneda = str(p.get("moneda", "")).strip()
        precio = str(
            p.get("precio_str") or p.get("precio") or p.get("precio_unitario") or ""
        ).strip()
        if not precio and p.get("precio_float") is not None:
            precio = f"{p.get('precio_float'):,.2f}"
        unidad = str(p.get("unidad") or p.get("presentacion") or "").strip()
        descripcion = str(p.get("descripcion") or p.get("descripcion_corta") or "").strip()

        datos_linea: List[str] = [f"{idx}. **{nombre}**"]
        if sku and sku.lower() not in nombre.lower() and sku.lower() != "n/a":
            datos_linea.append(f"  - SKU: {sku}")
        if unidad:
            datos_linea.append(f"  - Presentación: {unidad}")
        if precio and precio not in {"0", "0.0", "$0", "$0.0"}:
            if moneda and moneda.lower() not in precio.lower():
                datos_linea.append(f"  - Precio: {moneda} {precio}")
            else:
                datos_linea.append(f"  - Precio: {precio}")
        if descripcion and descripcion.lower() not in nombre.lower():
            desc_limpia = descripcion.replace("\n", " ").strip()
            if len(desc_limpia) > 100:
                desc_limpia = desc_limpia[:100] + "..."
            datos_linea.append(f"  - Descripción: {desc_limpia}")
        if categoria:
            datos_linea.append(f"  - Categoría: {categoria}")

        lineas.append("\n".join(datos_linea))

    return "Según nuestro catálogo, esto podría interesarte:\n\n" + "\n\n".join(lineas)
