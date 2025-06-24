import logging
import os
import re
from typing import List, Optional, Dict, Any, Tuple
from collections import OrderedDict
from .qdrant_utils import get_qdrant_client, verificar_y_crear_coleccion_qdrant
from services.logic import es_rubro_publico
from .cohere_ai import embed_textos
from qdrant_client.http import models as qdrant_models
from .utils import limpiar_texto_base, unir_codigos_alfa_numericos
from .herramientas_municipio import normalizar_texto

# Permite ajustar el número de resultados devueltos desde una variable de entorno.
DEFAULT_SEARCH_LIMIT = int(os.getenv("CATALOGO_RESULT_LIMIT", "5"))

# Colecciones separadas para cada tipo de usuario
CATALOGO_PYME = "catalogo_pyme"
CATALOGO_MUNICIPIO = "catalogo_municipio"

def coleccion_catalogo_para_rubro(rubro) -> str:
    """Devuelve el nombre de colección Qdrant según el rubro."""
    return CATALOGO_MUNICIPIO if es_rubro_publico(rubro) else CATALOGO_PYME

logger = logging.getLogger(__name__)

def buscar_catalogo_qdrant(
    user_id: Optional[int],
    pregunta: str,
    limite: int = DEFAULT_SEARCH_LIMIT,
    score_min: float = 0.30,
    categoria: str | None = None,
    coleccion: str = CATALOGO_PYME,
) -> List[qdrant_models.ScoredPoint]:
    qdrant_cli = get_qdrant_client()
    if not qdrant_cli:
        logger.error("[QDRANT SEARCH] No se pudo obtener cliente Qdrant.")
        return []

    # Aseguramos que la colección exista y tenga los índices necesarios.
    # Esto previene fallos 403 cuando no existe el índice 'categoria_qdrant'.
    if not verificar_y_crear_coleccion_qdrant(
        coleccion,
        vector_size=1024,
        create_indexes=True,
    ):
        logger.error(
            "[QDRANT SEARCH] No se pudo inicializar colección/indexes en Qdrant."
        )
        return []

    try:
        pregunta_pre = unir_codigos_alfa_numericos(pregunta.strip())
        pregunta_limpia = limpiar_texto_base(pregunta_pre)
        if not pregunta_limpia:
            logger.warning("[QDRANT SEARCH] Pregunta para búsqueda vacía después de limpiar.")
            return []

        # Ya no aplicamos un diccionario de sinónimos fijo. Se procesa el texto tal cual.
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

        if user_id is not None or categoria:
            must_conditions = []
            if user_id is not None:
                must_conditions.append(
                    qdrant_models.FieldCondition(
                        key="user_id",
                        match=qdrant_models.MatchValue(value=user_id),
                    )
                )
            if categoria:
                must_conditions.append(
                    qdrant_models.FieldCondition(
                        key="categoria_qdrant",
                        match=qdrant_models.MatchValue(value=categoria.lower()),
                    )
                )
            search_filter = qdrant_models.Filter(must=must_conditions)

        if user_id is not None:
            logger.info(
                f"[QDRANT SEARCH] Buscando en catálogo PRIVADO para {id_log}, pregunta '{pregunta_limpia}', categoria='{categoria}'"
            )
        else:
            logger.info(
                f"[QDRANT SEARCH] Buscando en catálogo GENERAL para {id_log}, pregunta '{pregunta_limpia}', categoria='{categoria}'"
            )
            # Si tuvieras un campo catálogo_público, acá le podés meter ese filtro

        resultados = qdrant_cli.search(
            collection_name=coleccion,
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min
        )
        logger.info(
            f"[QDRANT SEARCH] Pregunta: '{pregunta}', Hits: {len(resultados)}, Scores: {[getattr(r, 'score', 0) for r in resultados[:3]]}"
        )

        tokens_raw = normalizar_texto(pregunta_limpia).split()
        tokens = []
        tamanios = {"xs", "s", "m", "l", "xl", "xxl", "xxxl"}
        for t in tokens_raw:
            if len(t) > 2 or t in tamanios or t.isdigit():
                tokens.append(t)
        if tokens:
            filtrados: List[qdrant_models.ScoredPoint] = []
            for hit in resultados:
                payload = getattr(hit, "payload", {}) or {}
                texto = " ".join(
                    str(payload.get(k) or "")
                    for k in [
                        "nombre",
                        "descripcion",
                        "talles",
                        "categoria",
                        "categoria_qdrant",
                        "colores",
                    ]
                )
                texto_norm = normalizar_texto(texto)
                if all(tok in texto_norm for tok in tokens):
                    filtrados.append(hit)
            resultados = filtrados

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


def _combinar_payload(destino: Dict[str, Any], fuente: Dict[str, Any]) -> None:
    """Completa en ``destino`` los campos faltantes usando valores de ``fuente``."""
    for k, v in fuente.items():
        if v and not destino.get(k):
            destino[k] = v


def armar_respuesta_legible(
    resultados_qdrant: List[qdrant_models.ScoredPoint],
    max_items: int = DEFAULT_SEARCH_LIMIT,
    order_by: str | None = None,
) -> str:
    """Convierte una lista de resultados Qdrant en un texto legible."""

    if not resultados_qdrant:
        logger.info("[QDRANT FORMAT] No hay resultados Qdrant para formatear.")
        return (
            "No hay productos cargados en el catálogo. "
            "Contactá a la empresa para más info."
        )

    if order_by == "price":
        resultados_qdrant = _ordenar_por_precio(resultados_qdrant)
    else:
        resultados_qdrant = sorted(
            resultados_qdrant, key=lambda r: getattr(r, "score", 0), reverse=True
        )

    # -- Combinar productos repetidos por SKU o nombre --
    combinados: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
    for hit in resultados_qdrant:
        payload = getattr(hit, "payload", hit) or {}
        key = (
            str(payload.get("sku") or "").lower(),
            str(payload.get("nombre") or payload.get("title") or "").lower(),
        )
        if key not in combinados:
            combinados[key] = payload.copy()
        else:
            _combinar_payload(combinados[key], payload)

    productos_agrupados = list(combinados.values())

    lineas: List[str] = []
    for p in productos_agrupados[:max_items]:
        nombre = str(p.get("nombre") or p.get("title") or "Producto").strip()
        precio = str(p.get("precio_str") or p.get("precio") or "").strip()
        if not precio and p.get("precio_float") is not None:
            precio = f"{p.get('precio_float'):,.2f}"
        unidad = str(p.get("unidad") or p.get("presentacion") or "").strip()

        campos: List[str] = []
        if unidad:
            campos.append(unidad)
        if precio and precio not in {"0", "0.0", "$0", "$0.0"}:
            moneda = str(p.get("moneda", "")).strip()
            if moneda and moneda.lower() not in precio.lower():
                campos.append(f"{moneda} {precio}")
            else:
                campos.append(precio)

        linea = f"- **{nombre}**"
        if campos:
            linea += " | " + " | ".join(campos)

        lineas.append(linea)

    return "\n".join(lineas)
