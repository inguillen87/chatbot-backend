# services/catalogo_matcher.py
import re
import logging
import numpy as np
from models import CatalogoEmbedding 
from services.cohere_ai import embed_textos # Asumiendo que es la misma función de embedding
from sklearn.metrics.pairwise import cosine_similarity
from .utils import limpiar_texto_base, parse_precio_flexible # Importar desde utils

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

def buscar_en_catalogo_semantico(pregunta_usuario: str, user_id: int, threshold: float = 0.70, top_n: int = 3) -> str | None:
    """
    Busca en el catálogo semántico (modelo CatalogoEmbedding) para un user_id dado.
    Devuelve una cadena formateada con los mejores N resultados o None.
    NOTA: Este buscador es diferente de Qdrant. Considera si necesitas ambos.
    """
    logger.info(f"[CATALOG_MATCH] Iniciando búsqueda semántica en CatalogoEmbedding para user_id={user_id}, pregunta='{pregunta_usuario[:50]}...'")
    
    try:
        pregunta_limpia = limpiar_texto_base(pregunta_usuario)
        if not pregunta_limpia:
            logger.warning("[CATALOG_MATCH] Pregunta limpia está vacía.")
            return None

        # 1. Obtener todos los productos con embeddings para el user_id
        # Asumimos que CatalogoEmbedding.embedding_vector es una lista de floats (o JSON que se puede convertir)
        productos_db_con_vector = CatalogoEmbedding.query.filter(
            CatalogoEmbedding.user_id == user_id,
            CatalogoEmbedding.embedding_vector.isnot(None) # Asegurar que el vector no sea SQL NULL
        ).all()

        if not productos_db_con_vector:
            logger.info(f"[CATALOG_MATCH] No hay productos con embeddings en CatalogoEmbedding para user_id={user_id}")
            return None

        # Filtrar en Python por si el JSON es "null" o una lista vacía
        productos_validos = [
            p for p in productos_db_con_vector 
            if p.embedding_vector and isinstance(p.embedding_vector, list) and len(p.embedding_vector) > 0
        ]

        if not productos_validos:
            logger.warning(f"[CATALOG_MATCH] No hay productos con vectores numéricos válidos en CatalogoEmbedding para user_id={user_id} después del filtro Python.")
            return None
        
        # Extraer los vectores y convertirlos a un array de NumPy
        try:
            # p.embedding_vector se asume que es una lista de números (float o int)
            vectores_db = np.array([p.embedding_vector for p in productos_validos], dtype=np.float32)
            if vectores_db.ndim == 1: # Si por alguna razón todos los vectores son 1D y de la misma longitud (improbable para embeddings)
                # O si solo hay un producto y embedding_vector es una lista simple de números
                # Esto es una heurística, lo ideal es que embedding_vector siempre sea list[float]
                if len(productos_validos) == 1:
                    vectores_db = np.array([productos_validos[0].embedding_vector], dtype=np.float32)
                else: # Esto sería un error de datos
                    logger.error(f"[CATALOG_MATCH] Formato de vectores_db inesperado (1D) para múltiples productos. User ID: {user_id}")
                    return None

        except Exception as e_np:
            logger.error(f"[CATALOG_MATCH] Error convirtiendo vectores de DB a NumPy array para user_id={user_id}: {e_np}", exc_info=True)
            return None
            
        if vectores_db.shape[0] == 0: # No hay vectores después de la conversión
            logger.warning(f"[CATALOG_MATCH] No quedaron vectores válidos después de la conversión a NumPy. User ID: {user_id}")
            return None

        # 2. Generar embedding para la pregunta del usuario
        try:
            pregunta_vector_list = embed_textos([pregunta_limpia])
            if not pregunta_vector_list or not pregunta_vector_list[0] or not isinstance(pregunta_vector_list[0], list):
                logger.error(f"[CATALOG_MATCH] Error: embed_textos no devolvió un vector válido para: '{pregunta_limpia}'")
                return None
            pregunta_vector_np = np.array([pregunta_vector_list[0]], dtype=np.float32) # Debe ser 2D para cosine_similarity
        except Exception as e_embed:
            logger.error(f"[CATALOG_MATCH] Error al generar embedding para la pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
            return None

        # 3. Calcular similitud del coseno
        try:
            similitudes = cosine_similarity(pregunta_vector_np, vectores_db)[0] # [0] para obtener el array de scores
        except Exception as e_sim:
            logger.error(f"[CATALOG_MATCH] Error calculando similitud del coseno: {e_sim}", exc_info=True)
            return None
            
        # 4. Filtrar y ordenar candidatos
        candidatos_con_score = []
        for i, score in enumerate(similitudes):
            if score >= threshold:
                candidatos_con_score.append((productos_validos[i], float(score))) # Guardar el objeto producto y su score
        
        if not candidatos_con_score:
            max_sim = np.max(similitudes) if similitudes.size > 0 else -1.0 
            logger.info(f"[CATALOG_MATCH] Similaridad demasiado baja (máx: {max_sim:.3f}, umbral: {threshold}) para '{pregunta_limpia}' en CatalogoEmbedding. User ID: {user_id}")
            return None

        # Ordenar por score descendente
        candidatos_ordenados = sorted(candidatos_con_score, key=lambda item: item[1], reverse=True)
        
        # 5. Formatear los N mejores resultados
        resultados_formateados = []
        for prod_obj, score_similitud in candidatos_ordenados[:top_n]:
            cantidad_detectada = detectar_cantidad_generica(pregunta_limpia)

            nombre_prod = limpiar_texto_base(getattr(prod_obj, 'nombre', 'Producto Desconocido'))
            desc_prod = limpiar_texto_base(getattr(prod_obj, 'descripcion', ''))
            
            # Obtener precio del modelo CatalogoEmbedding (este modelo no tiene precio_str, precio_float, moneda)
            # El modelo CatalogoEmbedding SÍ tiene 'precio' (String) y 'cantidad' (Float)
            # PERO el payload que se arma en upload_processor para QDRANT tiene 'precio_str', 'precio_float', 'moneda'.
            # ESTO INDICA UNA DISCREPANCIA. CatalogoEmbedding debería tener los mismos campos de precio
            # que el payload de Qdrant si se quiere consistencia, o el formateo aquí debe adaptarse.
            # Asumiré que CatalogoEmbedding.precio es un string que parse_precio_flexible puede manejar.
            
            precio_crudo_db = getattr(prod_obj, 'precio', None)
            precio_str_display, precio_float_calc, moneda_calc = parse_precio_flexible(str(precio_crudo_db) if precio_crudo_db else None)

            precio_mostrar = precio_str_display if precio_str_display else "Consultar precio"
            if precio_float_calc is not None and moneda_calc and (moneda_calc.upper() not in precio_mostrar.upper() and "$" not in precio_mostrar):
                 precio_mostrar = f"{moneda_calc} {precio_mostrar}"
            
            total_mostrar = "No calculable"
            if precio_float_calc is not None and cantidad_detectada > 0:
                total_float = cantidad_detectada * precio_float_calc
                total_mostrar = f"{moneda_calc if moneda_calc else ''} {total_float:.2f}".strip()

            # 'unidad' no está en el modelo CatalogoEmbedding. Se podría añadir o inferir.
            # Por ahora, lo omitimos o ponemos un default.
            unidad_prod = "unidad(es)" # Default si no hay campo 'unidad' en CatalogoEmbedding

            info_item = f"📦 Producto: {nombre_prod} (Score: {score_similitud:.2f})" # Añadir score para debug/info
            if desc_prod and desc_prod != nombre_prod:
                info_item += f"\n📝 Descripción: {desc_prod[:150]}" # Acortar descripción
            info_item += f"\n💰 Precio ({unidad_prod}): {precio_mostrar}"
            if cantidad_detectada > 1 and precio_float_calc is not None:
                info_item += f"\n🧮 Total por {cantidad_detectada} {unidad_prod}: {total_mostrar}"
            
            resultados_formateados.append(info_item)

        if resultados_formateados:
            respuesta_final = "\n\n---\n\n".join(resultados_formateados)
            logger.info(f"[CATALOG_MATCH] Respuesta formateada (parcial): {respuesta_final[:200]}...")
            return respuesta_final
        
        return None # No se encontraron o formatearon resultados

    except Exception as e:
        logger.error(f"❌ Error general en buscar_en_catalogo_semantico (CatalogoEmbedding) para '{pregunta_usuario}', user_id={user_id}: {e}", exc_info=True)
        return None