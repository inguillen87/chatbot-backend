import logging
from types import SimpleNamespace
import os
import re
from typing import List, Optional, Dict, Any, Tuple
from collections import OrderedDict, Counter
from .qdrant_utils import get_qdrant_client, verificar_y_crear_coleccion_qdrant
from services.logic import es_rubro_publico
from .embedding_service import embed_textos_llm as embed_textos

# from collections import Counter # Ya está importado arriba
from qdrant_client.http import models as qdrant_models
from .common_utils import limpiar_texto_base, unir_codigos_alfa_numericos # Changed from .utils
from .herramientas_municipio import normalizar_texto

# Permite ajustar el número de resultados devueltos desde una variable de entorno.
DEFAULT_SEARCH_LIMIT = int(os.getenv("CATALOGO_RESULT_LIMIT", "5"))

# Columnas por defecto para la tabla de catálogo en formato Markdown
DEFAULT_TABLE_COLUMNS = [
    ("ID", "id"), ("Nombre", "nombre"), ("Desc", "descripcion"), ("Precio", "precio_str"),
    ("Cat", "categoria_qdrant"), ("SKU", "sku"), ("Marca", "marca")
]

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
    precio_min: Optional[float] = None,
    precio_max: Optional[float] = None,
) -> List[qdrant_models.ScoredPoint]:
    """
    Busca productos en el catálogo vectorial Qdrant de una PyME, maximizando relevancia comercial y minimizando falsos negativos.
    Implementa filtros avanzados por categoría, promoción, stock y rango de precios.
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
        must_conditions = []
        id_log = f"user_id {user_id}" if user_id is not None else "ANONIMO"

        # Filtro de usuario obligatorio si se provee
        if user_id is not None:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="user_id", match=qdrant_models.MatchValue(value=user_id)
                )
            )

        # Filtro por categoría
        if categoria:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="categoria_qdrant",
                    match=qdrant_models.MatchValue(value=categoria.lower()),
                )
            )

        # Filtros booleanos (promocion, stock)
        # Nota: Asumimos que el payload tiene campos 'en_promocion' y 'con_stock' o similar.
        # Si el payload usa otros nombres (ej. 'stock' > 0), ajustamos aquí.
        if en_promocion is True:
            # Opción A: campo booleano 'en_promocion'
            # must_conditions.append(qdrant_models.FieldCondition(key="en_promocion", match=qdrant_models.MatchValue(value=True)))

            # Opción B: campo 'promocion_info' o 'promocion_texto' no vacío.
            # Qdrant no tiene "IsNotEmpty" directo fácil en MatchValue, pero podemos filtrar si existe.
            # Para simplificar y dado que el prompt pide "en_promocion=True", asumimos un flag o lógica de negocio.
            # Vamos a usar un filtro de rango o match value si el campo existe como bool.
            # Si no existe, filtramos post-búsqueda o ajustamos el ingest.
            # Asumiremos que el ingest agrega 'en_promocion': True/False.
             must_conditions.append(
                qdrant_models.FieldCondition(
                    key="en_promocion", match=qdrant_models.MatchValue(value=True)
                )
            )

        if con_stock is True:
            # Filtrar items con stock > 0
            # Asumiendo campo 'cantidad' o 'stock' numérico
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="stock",
                    range=qdrant_models.Range(gt=0)
                )
            )

        # Filtro de Precio
        if precio_min is not None or precio_max is not None:
            rango_precio = qdrant_models.Range()
            if precio_min is not None:
                rango_precio.gte = float(precio_min)
            if precio_max is not None:
                rango_precio.lte = float(precio_max)

            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="precio_float",
                    range=rango_precio
                )
            )

        search_filter = None
        if must_conditions:
            search_filter = qdrant_models.Filter(must=must_conditions)

        logger.info(
            f"[QDRANT SEARCH] Buscando en ({coleccion}) para {id_log}, pregunta='{pregunta_limpia}', filtros={{cat:{categoria}, promo:{en_promocion}, stock:{con_stock}, p_min:{precio_min}, p_max:{precio_max}}}"
        )

        resultados = qdrant_cli.search(
            collection_name=coleccion,
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min,
        )

        # Fallback: Si no hay resultados con filtros estrictos, quizás relajar score?
        # Por ahora mantenemos la lógica original de reintentar sin threshold si falla la primera?
        # La lógica original reintentaba sin score_threshold pero CON filtros.
        if not resultados:
            resultados = qdrant_cli.search(
                collection_name=coleccion,
                query_vector=vector_q,
                query_filter=search_filter, # Mantenemos filtros, relajamos score
                limit=limite,
            )

        logger.info(
            f"[QDRANT SEARCH] Hits: {len(resultados)}, Scores: {[getattr(r, 'score', 0) for r in resultados[:3]]}"
        )

        # --- FILTRADO DE CALIDAD POST-SEARCH ---
        filtrados = []
        for hit in resultados:
            payload = getattr(hit, "payload", {}) or {}
            nombre = payload.get("nombre", "") or payload.get("title", "")
            precio = payload.get("precio_float") or payload.get("precio")

            # Filtro básico de integridad de datos
            if nombre and (precio is None or precio == "" or float(precio) >= 0):
                filtrados.append(hit)

        return filtrados

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
    """
    Formatea los resultados de Qdrant en una lista legible para el usuario,
    deduplicando y combinando información de productos idénticos.
    """
    if not resultados_qdrant:
        return "No se encontraron productos que coincidan con tu búsqueda."

    # Usar OrderedDict para mantener el orden de aparición del mejor hit
    productos_combinados = OrderedDict()

    for hit in resultados_qdrant:
        payload = getattr(hit, "payload", {}) or {}
        # Usar 'id' como clave principal para la deduplicación
        prod_id = payload.get("id") or payload.get("sku") or payload.get("nombre")
        if not prod_id:
            continue

        if prod_id not in productos_combinados:
            # Guardar el payload completo y el score del primer (mejor) hit
            productos_combinados[prod_id] = {
                "payload": payload,
                "score": getattr(hit, "score", 0.0),
            }
        else:
            # Combinar campos de hits duplicados
            # El payload existente se actualiza con campos faltantes del nuevo hit
            _combinar_payload(productos_combinados[prod_id]["payload"], payload)

    # Convertir de nuevo a una lista de objetos similares a ScoredPoint para ordenar
    lista_productos_final = [
        SimpleNamespace(
            id=prod_id, payload=data["payload"], score=data["score"]
        )
        for prod_id, data in productos_combinados.items()
    ]

    # Re-ordenar la lista final deduplicada si es necesario
    def _sort_key(p):
        payload = p.payload
        score = p.score
        destacado = 0 if payload.get("destacado") else 1
        precio = float(payload.get("precio_float", 0) or 0)
        nombre = (payload.get("nombre") or "").lower()
        return (-score, destacado, precio, nombre)

    productos_ordenados = sorted(lista_productos_final, key=_sort_key)


    lineas = []
    for prod in productos_ordenados[:max_items]:
        payload = prod.payload
        nombre = str(payload.get("nombre", "")).strip()
        precio_str = str(payload.get("precio_str", "")).strip()
        desc = str(payload.get("descripcion", "")).strip()

        linea = f"* **{nombre}**"
        if precio_str:
            linea += f" - ${precio_str}"
        if desc:
            linea += f": {desc}"
        lineas.append(linea)

    if len(productos_ordenados) > max_items:
        lineas.append(f"... y {len(productos_ordenados) - max_items} más.")

    return "\n".join(lineas)


def formatear_tabla_catalogo(
    resultados_qdrant: List[qdrant_models.ScoredPoint],
    columnas: Optional[List[Tuple[str, str]]] = None,
) -> str:
    """
    Devuelve una representación en tabla Markdown de los resultados,
    deduplicando y combinando información de productos idénticos.
    """
    if not resultados_qdrant:
        return "No se encontraron productos para mostrar en la tabla."

    # Deduplicar y combinar payloads
    productos_combinados = OrderedDict()
    for hit in resultados_qdrant:
        payload = getattr(hit, "payload", {}) or {}
        prod_id = payload.get("id") or payload.get("sku") or payload.get("nombre")
        if not prod_id:
            continue
        if prod_id not in productos_combinados:
            productos_combinados[prod_id] = payload
        else:
            _combinar_payload(productos_combinados[prod_id], payload)

    # Determinar columnas a mostrar
    # Si no se especifican columnas, se generan dinámicamente
    if not columnas:
        all_keys = set()
        for payload in productos_combinados.values():
            all_keys.update(payload.keys())

        # Ordenar columnas con algunas claves comunes primero
        key_order = ['id', 'sku', 'nombre', 'descripcion', 'marca', 'precio_str', 'categoria_qdrant']
        ordered_keys = [key for key in key_order if key in all_keys]
        remaining_keys = sorted([key for key in all_keys if key not in key_order])
        final_keys = ordered_keys + remaining_keys

        # Crear tuplas (Header, key) para las columnas
        columnas = [(key.replace('_', ' ').capitalize(), key) for key in final_keys]

    # Construir tabla
    encabezado = "| " + " | ".join(col for col, _ in columnas) + " |"
    separador = "|" + "|".join(["---"] * len(columnas)) + "|"
    filas = [encabezado, separador]

    for payload in productos_combinados.values():
        celdas = []
        for _, key in columnas:
            val = payload.get(key)
            # Normalizar valores vacíos para una mejor visualización
            if val is None or str(val).strip() == "":
                val = "-"
            celdas.append(str(val).replace("\n", " ").strip())
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
        from .llm_utils import robust_chat # Changed from .cohere_ai

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
    en_promocion: bool = False, # Added param
    con_stock: bool = False, # Added param
    precio_min: float = None, # Added param
    precio_max: float = None, # Added param
) -> tuple[list[qdrant_models.ScoredPoint], str | None]:
    """Búsqueda en Qdrant con inferencia de intención si hay pocos resultados."""

    resultados = buscar_catalogo_qdrant(
        user_id=user_id,
        pregunta=pregunta,
        limite=limite,
        score_min=score_min,
        coleccion=coleccion,
        en_promocion=en_promocion, # Pass through
        con_stock=con_stock, # Pass through
        precio_min=precio_min, # Pass through
        precio_max=precio_max, # Pass through
    )

    hay_score_suficiente = any(
        getattr(r, "score", 0) >= score_suficiente for r in resultados
    )
    if resultados and hay_score_suficiente:
        return resultados, None

    sugerencia = inferir_intencion_con_llm(pregunta)
    return resultados, sugerencia
