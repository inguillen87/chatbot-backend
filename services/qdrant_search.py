import logging
import os
import re
from typing import List, Optional, Dict, Any, Tuple
from collections import OrderedDict, Counter
from .qdrant_utils import get_qdrant_client, verificar_y_crear_coleccion_qdrant
from services.logic import es_rubro_publico

# from collections import Counter # Ya está importado arriba
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
    en_promocion: Optional[bool] = None, 
    con_stock: Optional[bool] = None,
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

        precio_val = payload.get("precio_float")
        precio_str_display = payload.get("precio_str", "")

        if precio_val is not None:
            try:
                precio_formateado = f"${float(precio_val):,.2f}"
            except (ValueError, TypeError):
                precio_formateado = precio_str_display or "Consultar"
        elif precio_str_display:
            # Ensure that price_str_display doesn't accidentally contain non-price numbers if parse_precio_flexible was too aggressive
            # For now, we trust precio_val if it exists, otherwise precio_str_display
            precio_formateado = precio_str_display
        else:
            precio_formateado = "Consultar" # Default if no price info

        # New unit fields from Qdrant payload
        unidad_original_str = payload.get("unidad_original", "")     # e.g., "Caja x 6 botellas"
        unidad_desc_parsed = payload.get("unidad_descripcion", "")    # e.g., "Caja botellas" or "Caja"
        cantidad_empaque_val = payload.get("cantidad_empaque")      # e.g., 6 (int) or None

        # Usar descripcion_corta si existe, sino la descripcion normal
        desc_corta = payload.get("descripcion_corta", "")
        desc_completa = payload.get("descripcion", "")
        desc_display = desc_corta if desc_corta else desc_completa

        promocion = payload.get("promocion_info", "")

        res = f"- <b>{nombre}</b>"

        # Construct unit display string
        display_unidad_info = ""
        if unidad_desc_parsed and cantidad_empaque_val and cantidad_empaque_val > 1:
            # e.g., "Caja (empaque de 6)" or "Caja botellas (empaque de 6)"
            display_unidad_info = f"{unidad_desc_parsed} (empaque de {cantidad_empaque_val})"
        elif unidad_desc_parsed: # e.g., "Botella", "Unidad" (cantidad_empaque_val is 1 or None)
            display_unidad_info = unidad_desc_parsed
        elif unidad_original_str: # Fallback to original string if parsing was incomplete
            display_unidad_info = unidad_original_str

        if display_unidad_info:
            res += f" ({display_unidad_info})"

        res += f" — <b>{precio_formateado}</b>"

        # Display per-item price if the main price is for a pack
        # This assumes `precio_val` is the price for the pack of `cantidad_empaque_val` items.
        if precio_val is not None and isinstance(cantidad_empaque_val, int) and cantidad_empaque_val > 1:
            try:
                precio_por_item_individual = float(precio_val) / cantidad_empaque_val
                # Only show if significantly different from pack price and makes sense
                if abs(precio_por_item_individual - float(precio_val)) > 0.01 :
                     res += f" <i style='font-size:smaller;'>(equivale a ${precio_por_item_individual:,.2f} c/u individual)</i>"
            except (ValueError, TypeError, ZeroDivisionError):
                pass # Couldn't calculate individual price

        if promocion:
            res += f" <b style='color:green;'>({promocion})</b>"

        if desc_display:
            res += f" | {desc_display[:70]}{'...' if len(desc_display) > 70 else ''}"

        lineas.append(res)
        if len(lineas) >= max_items:
            break
    if len(productos_ordenados) > max_items:
        lineas.append(f"…y {len(productos_ordenados) - max_items} productos más.")
    return "\n".join(lineas)


def formatear_tabla_catalogo(
    resultados_qdrant: List[qdrant_models.ScoredPoint],
    columnas: list[tuple[str, str]] | None = None,
) -> str:
    """Devuelve una representación en tabla Markdown de los resultados."""
    if not resultados_qdrant:
        return (
            "No hay productos en el catálogo que coincidan con tu búsqueda. "
            "¿Querés ver el catálogo completo?"
        )

    columnas = columnas or DEFAULT_TABLE_COLUMNS
    encabezado = "| " + " | ".join(col for col, _ in columnas) + " |"
    separador = "|" + "|".join("---" for _ in columnas) + "|"
    filas: list[str] = [encabezado, separador]

    for hit in resultados_qdrant:
        payload = getattr(hit, "payload", {}) or {}
        celdas = []
        for _, key in columnas:
            val = payload.get(key)
            if val is None or str(val).strip() == "":
                val = "-"
            celdas.append(str(val))
        filas.append("| " + " | ".join(celdas) + " |")

    return "\n".join(filas)


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


# --- Funciones avanzadas para inferir intención y ofrecer sugerencias ---

PROMPT_INTENCION_BUSQUEDA = """
Sos un asistente de ventas. Analizá la consulta del usuario y respondé solo con
una de las siguientes palabras: ofertas, combos, destacados o consulta_exacta.

CONSULTA: "{consulta}"
"""


def inferir_intencion_con_llm(consulta: str) -> str | None:
    """Intenta deducir la intención comercial del usuario con el LLM."""
    try:
        from .cohere_ai import robust_chat

        resp = robust_chat(message=PROMPT_INTENCION_BUSQUEDA.format(consulta=consulta))
        if resp:
            return resp.strip().lower()
    except Exception:
        logger.exception("[QDRANT SEARCH] Error al inferir intención con el LLM")
    return None


def buscar_catalogo_avanzado(
    user_id: Optional[int],
    pregunta: str,
    limite: int = DEFAULT_SEARCH_LIMIT,
    score_min: float = 0.20,
    coleccion: str = CATALOGO_PYME,
    score_suficiente: float = 0.25,
) -> tuple[list[qdrant_models.ScoredPoint], str | None]:
    """Búsqueda en Qdrant con inferencia de intención si hay pocos resultados."""

    resultados = buscar_catalogo_qdrant(
        user_id=user_id,
        pregunta=pregunta,
        limite=limite,
        score_min=score_min,
        coleccion=coleccion,
    )

    hay_score_suficiente = any(
        getattr(r, "score", 0) >= score_suficiente for r in resultados
    )
    if resultados and hay_score_suficiente:
        return resultados, None

    sugerencia = inferir_intencion_con_llm(pregunta)
    return resultados, sugerencia
