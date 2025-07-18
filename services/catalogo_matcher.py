# services/catalogo_matcher.py
import re
import logging
import numpy as np
from models import CatalogoEmbedding 
from services.cohere_ai import embed_textos # Asumiendo que es la misma función de embedding
from sklearn.metrics.pairwise import cosine_similarity
from .common_utils import limpiar_texto_base, parse_precio_flexible, unir_codigos_alfa_numericos # Importar desde common_utils

logger = logging.getLogger(__name__)

def detectar_cantidad_generica(texto: str) -> int:
    """Detecta una cantidad numérica en el texto."""
    # (Tu función se mantiene, pero podría ser más robusta o usar NLP si es necesario)
    # Ejemplo simplificado:
    texto_lower = limpiar_texto_base(texto)
    match_con_palabra = re.search(r"(?:quiero|necesito|dame|comprar|cantidad|para|por)\s*(\d{1,3})\b|\b(\d{1,3})\s*(?:cajas|unidades|unids|u\.?|pares|sets|kits|packs)\b", texto_lower)
    if match_con_palabra:
        for group_num in range(1, len(match_con_palabra.groups()) + 1): # Iterar por grupos de captura
            if match_con_palabra.group(group_num):
                try:
                    return int(match_con_palabra.group(group_num))
                except ValueError:
                    continue # Si un grupo no es int, probar el siguiente
    
    # Fallback a buscar cualquier número si no hay palabras clave específicas
    match_simple = re.search(r"\b(\d{1,3})\b", texto_lower) # Buscar números como palabras completas
    if match_simple:
        try:
            return int(match_simple.group(1))
        except ValueError:
            pass
    return 1 # Default a 1 si no se detecta cantidad

import json

def _cargar_sinonimos():
    try:
        with open("data/product_synonyms.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

PRODUCT_SYNONYMS = _cargar_sinonimos()

def buscar_en_catalogo_semantico(pregunta_usuario: str, user_id: int, threshold: float = 0.60, top_n: int = 3) -> str | None:
    """
    Busca en el catálogo semántico y por palabras clave, usando sinónimos.
    Devuelve una cadena formateada con los mejores N resultados o None.
    """
    logger.info(f"[CATALOG_MATCH] Iniciando búsqueda para user_id={user_id}, pregunta='{pregunta_usuario[:50]}...'")
    
    try:
        pregunta_pre = unir_codigos_alfa_numericos(pregunta_usuario)
        pregunta_limpia = limpiar_texto_base(pregunta_pre)
        if not pregunta_limpia:
            logger.warning("[CATALOG_MATCH] Pregunta limpia está vacía.")
            return None

        # Expandir la búsqueda con sinónimos
        terminos_busqueda = [pregunta_limpia]
        for key, synonyms in PRODUCT_SYNONYMS.items():
            if key in pregunta_limpia:
                terminos_busqueda.extend(synonyms)

        candidatos_keyword = []
        for termino in terminos_busqueda:
            candidatos_keyword.extend(CatalogoEmbedding.query.filter(
                CatalogoEmbedding.user_id == user_id,
                CatalogoEmbedding.nombre.ilike(f"%{termino}%")
            ).all())

        # Búsqueda semántica
        productos_db_con_vector = CatalogoEmbedding.query.filter(
            CatalogoEmbedding.user_id == user_id,
            CatalogoEmbedding.embedding_vector.isnot(None)
        ).all()

        if not productos_db_con_vector:
            logger.info(f"[CATALOG_MATCH] No hay productos con embeddings en CatalogoEmbedding para user_id={user_id}")
            # Si no hay embeddings, devolvemos solo los resultados de keyword
            if candidatos_keyword:
                return _formatear_resultados(candidatos_keyword, pregunta_limpia, top_n)
            return None

        productos_validos = [p for p in productos_db_con_vector if p.embedding_vector and isinstance(p.embedding_vector, list) and len(p.embedding_vector) > 0]

        if not productos_validos:
            logger.warning(f"[CATALOG_MATCH] No hay productos con vectores numéricos válidos en CatalogoEmbedding para user_id={user_id} después del filtro Python.")
            if candidatos_keyword:
                return _formatear_resultados(candidatos_keyword, pregunta_limpia, top_n)
            return None
        
        vectores_db = np.array([p.embedding_vector for p in productos_validos], dtype=np.float32)

        pregunta_vector_list = embed_textos([pregunta_limpia])
        pregunta_vector_np = np.array([pregunta_vector_list[0]], dtype=np.float32)

        similitudes = cosine_similarity(pregunta_vector_np, vectores_db)[0]

        candidatos_semanticos = []
        for i, score in enumerate(similitudes):
            if score >= threshold:
                candidatos_semanticos.append((productos_validos[i], float(score)))

        # Combinar y eliminar duplicados
        candidatos_combinados = {p.id: (p, 1.0) for p in candidatos_keyword} # Mayor score a keywords
        for p, score in candidatos_semanticos:
            if p.id not in candidatos_combinados:
                candidatos_combinados[p.id] = (p, score)

        if not candidatos_combinados:
            return None

        candidatos_ordenados = sorted(candidatos_combinados.values(), key=lambda item: item[1], reverse=True)
        
        return _formatear_resultados([p for p, s in candidatos_ordenados], pregunta_limpia, top_n)

    except Exception as e:
        logger.error(f"❌ Error general en buscar_en_catalogo_semantico para '{pregunta_usuario}', user_id={user_id}: {e}", exc_info=True)
        return None

def _formatear_resultados(productos: list, pregunta_limpia: str, top_n: int) -> str | None:
    """Formatea la lista de productos para mostrar al usuario."""
    resultados_formateados = []
    for prod_obj in productos[:top_n]:
        cantidad_detectada = detectar_cantidad_generica(pregunta_limpia)
        nombre_prod = limpiar_texto_base(getattr(prod_obj, 'nombre', 'Producto Desconocido'))
        desc_prod = limpiar_texto_base(getattr(prod_obj, 'descripcion', ''))

        precio_crudo_db = getattr(prod_obj, 'precio', None)
        precio_str_display, precio_float_calc, moneda_calc = parse_precio_flexible(str(precio_crudo_db) if precio_crudo_db else None)

        precio_mostrar = precio_str_display if precio_str_display else "Consultar precio"
        if precio_float_calc is not None and moneda_calc and (moneda_calc.upper() not in precio_mostrar.upper() and "$" not in precio_mostrar):
             precio_mostrar = f"{moneda_calc} {precio_mostrar}"

        total_mostrar = "No calculable"
        if precio_float_calc is not None and cantidad_detectada > 0:
            total_float = cantidad_detectada * precio_float_calc
            total_mostrar = f"{moneda_calc if moneda_calc else ''} {total_float:.2f}".strip()

        unidad_prod = "unidad(es)"

        info_item = f"📦 Producto: {nombre_prod}"
        if desc_prod and desc_prod != nombre_prod:
            info_item += f"\n📝 Descripción: {desc_prod[:150]}"
        info_item += f"\n💰 Precio ({unidad_prod}): {precio_mostrar}"
        if cantidad_detectada > 1 and precio_float_calc is not None:
            info_item += f"\n🧮 Total por {cantidad_detectada} {unidad_prod}: {total_mostrar}"
        
        resultados_formateados.append(info_item)

    if resultados_formateados:
        return "\n\n---\n\n".join(resultados_formateados)

    return None
