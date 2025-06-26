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
    score_min: float = 0.20,
    categoria: str | None = None,
    coleccion: str = CATALOGO_PYME,
) -> List[qdrant_models.ScoredPoint]:
    """
    Busca productos en el catálogo vectorial Qdrant de una PyME, maximizando relevancia comercial y minimizando falsos negativos.
    Retorna una lista de resultados ordenados por score y enriquecidos para experiencia de usuario.
    """
    qdrant_cli = get_qdrant_client()
    if not qdrant_cli:
        logger.error("[QDRANT SEARCH] No se pudo obtener cliente Qdrant.")
        return []

    # Verifica que la colección e índices existen.
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
            logger.warning(
                "[QDRANT SEARCH] Pregunta para búsqueda vacía después de limpiar."
            )
            return []

        vector_pregunta_lista = embed_textos(
            [pregunta_limpia], input_type="search_query"
        )
        if not vector_pregunta_lista or not isinstance(vector_pregunta_lista[0], list):
            logger.error(
                f"[QDRANT SEARCH] No se pudo generar vector para pregunta: '{pregunta_limpia}'"
            )
            return []
        vector_q = vector_pregunta_lista[0]
    except Exception as e_embed:
        logger.error(
            f"[QDRANT SEARCH] Error generando embedding para pregunta '{pregunta_limpia}': {e_embed}",
            exc_info=True,
        )
        return []

    try:
        search_filter = None
        id_log = f"user_id {user_id}" if user_id is not None else "ANONIMO"
        must_conditions = []

        if user_id is not None:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="user_id", match=qdrant_models.MatchValue(value=user_id)
                )
            )
        if categoria:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="categoria_qdrant",
                    match=qdrant_models.MatchValue(value=categoria.lower()),
                )
            )
        if must_conditions:
            search_filter = qdrant_models.Filter(must=must_conditions)

        logger.info(
            f"[QDRANT SEARCH] Buscando en catálogo ({coleccion}) para {id_log}, pregunta '{pregunta_limpia}', categoria='{categoria}'"
        )

        resultados = qdrant_cli.search(
            collection_name=coleccion,
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min,
        )

        if not resultados:
            resultados = qdrant_cli.search(
                collection_name=coleccion,
                query_vector=vector_q,
                query_filter=search_filter,
                limit=limite,
            )

        logger.info(
            f"[QDRANT SEARCH] Pregunta: '{pregunta}', Hits: {len(resultados)}, Scores: {[getattr(r, 'score', 0) for r in resultados[:3]]}"
        )

        # --- FILTRADO INTELIGENTE Y FLEXIBLE (opcional, solo si querés más control) ---
        # Si querés filtrar resultados basura, lo mejor es filtrar solo productos sin nombre/código o con precio 0:
        filtrados = []
        for hit in resultados:
            payload = getattr(hit, "payload", {}) or {}
            nombre = payload.get("nombre", "") or payload.get("title", "")
            precio = payload.get("precio_float") or payload.get("precio")
            if nombre and (precio is None or precio == "" or float(precio) > 0):
                filtrados.append(hit)
        if filtrados:
            resultados = filtrados

        return resultados

    except Exception as e_qdrant:
        id_log = f"user_id {user_id}" if user_id is not None else "ANONIMO"
        logger.error(
            f"[QDRANT SEARCH] Error buscando en Qdrant para {id_log}, pregunta '{pregunta_limpia}': {e_qdrant}",
            exc_info=True,
        )
        return []


def _ordenar_por_precio(
    resultados: List[qdrant_models.ScoredPoint],
) -> List[qdrant_models.ScoredPoint]:
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
    consulta_usuario: str = "",
) -> str:
    if not resultados_qdrant:
        return (
            "No hay productos en el catálogo que coincidan con tu búsqueda. "
            "¿Querés ver el catálogo completo?"
        )

    # Ordená por score Qdrant, luego por destacado, luego por precio.
    def _key(p):
        payload = getattr(p, "payload", p) or {}
        score = getattr(p, "score", 0)
        destacado = 0 if payload.get("destacado") else 1
        precio = float(payload.get("precio_float") or 0)
        nombre = (payload.get("nombre") or "").lower()
        return (-score, destacado, precio, nombre)

    productos_ordenados = sorted(resultados_qdrant, key=_key)
    lineas = []
    vistos = set()

    for prod in productos_ordenados:
        payload = getattr(prod, "payload", prod) or {}
        nombre = str(payload.get("nombre", "")).strip()
        if not nombre:
            continue
        clave = nombre.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        precio = payload.get("precio_str") or payload.get("precio") or ""
        if not precio and payload.get("precio_float") is not None:
            precio = f"${payload['precio_float']:,.2f}"
        if not precio:
            precio = "Consultar"
        unidad = payload.get("unidad", "")
        desc = payload.get("descripcion", "")
        res = f"- <b>{nombre}</b>"
        if unidad:
            res += f" ({unidad})"
        res += f" — <b>{precio}</b>"
        if desc:
            res += f" | {desc[:60]}{'...' if len(desc)>60 else ''}"
        lineas.append(res)
        if len(lineas) >= max_items:
            break
    if len(productos_ordenados) > max_items:
        lineas.append(f"…y {len(productos_ordenados)-max_items} productos más.")
    return "\n".join(lineas)


def armar_respuesta_legible_multi_rubro(
    resultados: List[qdrant_models.ScoredPoint],
    max_items: int = DEFAULT_SEARCH_LIMIT,
):
    """Formatea los productos de forma amigable para cualquier rubro."""
    if not resultados:
        return (
            "No encontramos productos exactos para tu búsqueda. ¿Querés ver otras opciones? "
            "Podés consultar el catálogo completo o pedir ayuda a un agente."
        )

    lineas = []
    vistos = set()
    for prod in resultados:
        pl = getattr(prod, "payload", {}) or {}
        nombre = str(pl.get("nombre", "")).strip().capitalize()
        if not nombre:
            continue
        clave = nombre.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        precio = str(pl.get("precio_str", "") or pl.get("precio", "")).strip()
        unidad = str(pl.get("unidad", "")).strip()
        marca = str(pl.get("marca", "")).strip()
        desc = str(pl.get("descripcion", "")).strip()
        talles = str(pl.get("talles", "")).strip()
        colores = str(pl.get("colores", "")).strip()

        extras = []
        if marca:
            extras.append(f"Marca: {marca}")
        if talles:
            extras.append(f"Talles: {talles}")
        if colores:
            extras.append(f"Colores: {colores}")
        if desc:
            extras.append(desc)

        linea = f"- **{nombre}**"
        if unidad:
            linea += f" ({unidad})"
        if precio:
            linea += f" - {precio}"
        if extras:
            linea += " | " + " | ".join(extras)
        lineas.append(linea)
        if len(lineas) >= max_items:
            break

    return "\n".join(lineas)
