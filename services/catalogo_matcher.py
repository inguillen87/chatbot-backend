# En tu archivo catalogo_matcher.py o donde corresponda

import re
import logging
import numpy as np
# Asumo que CatalogoEmbedding es tu modelo SQLAlchemy para la tabla que tiene los embeddings
# y los metadatos de los productos (nombre, precio, etc.)
from models import CatalogoEmbedding 
from services.cohere_ai import embed_textos # Para generar el embedding de la pregunta del usuario
from sklearn.metrics.pairwise import cosine_similarity

# Funciones de limpieza y normalización de precios (similares a las de google_docai.py)
# Idealmente, estas estarían en un archivo de utilidades (utils.py) e importadas donde se necesiten.
def limpiar_texto_simple_cm(texto: str) -> str: # cm para catalog_matcher, evitar colisión
    if not texto: return ""
    return re.sub(r'\s+', ' ', texto).strip()

def normalizar_precio_cm(precio_val) -> str:
    if pd.isna(precio_val) if isinstance(precio_val, float) else not precio_val: # Manejar NaN de pandas y vacíos
        return ""
    precio_str = str(precio_val)
    precio = precio_str.replace("$", "").strip()
    if ',' in precio and '.' in precio:
        if precio.rfind(',') > precio.rfind('.'):
            precio = precio.replace('.', '').replace(',', '.')
        else:
            precio = precio.replace(',', '')
    elif ',' in precio:
        precio = precio.replace(',', '.')
    if re.fullmatch(r"\d+(\.\d{1,2})?", precio):
        return precio
    logging.warning(f"[CM] Precio no pudo normalizarse: '{precio_str}' -> '{precio}'")
    return ""

def extraer_precio_float_cm(precio_normalizado_str: str) -> float | None:
    if not precio_normalizado_str: return None
    try:
        return float(precio_normalizado_str)
    except ValueError:
        return None
# --- Fin funciones de utilidad ---

def detectar_cantidad_generica(texto: str) -> int:
    """
    Detecta un número en el texto del usuario (por ejemplo "quiero 6 cajas")
    y lo devuelve como cantidad. Si no encuentra, devuelve 1.
    Más robusto si buscamos números asociados a palabras de cantidad o al final.
    """
    # Intenta encontrar patrones como "6 cajas", "cantidad 3", "quiero 2"
    match_con_palabra = re.search(r"(?:quiero|necesito|dame|comprar|cantidad|para|por)\s*(\d+)\b|\b(\d+)\s*(?:cajas|unidades|unids|u\.|pares|sets|kits|packs)\b", texto.lower())
    if match_con_palabra:
        # El número puede estar en el grupo 1 o 2 o 3, dependiendo de qué parte de la regex coincidió.
        # Necesitamos encontrar cuál de los grupos no es None.
        for i in range(1, len(match_con_palabra.groups()) + 1):
            if match_con_palabra.group(i):
                return int(match_con_palabra.group(i))
    
    # Si no, busca cualquier número, preferiblemente al final o aislado.
    # Esta regex es más simple y toma el primer número que encuentre.
    match_simple = re.search(r"(\d+)", texto.lower())
    if match_simple:
        return int(match_simple.group(1))
        
    return 1


def buscar_en_catalogo_semantico(pregunta_usuario: str, user_id: int, threshold: float = 0.75, top_n: int = 1) -> str | None:
    """
    Busca en la tabla CatalogoEmbedding usando similitud semántica.
    Devuelve una cadena formateada con los top_n productos más similares si superan el umbral.
    La precisión depende CRÍTICAMENTE de la calidad de los datos en la tabla CatalogoEmbedding.
    """
    try:
        pregunta_limpia = limpiar_texto_simple_cm(pregunta_usuario)
        logging.info(f"[CM] Buscando en catálogo semántico para user_id={user_id}, pregunta='{pregunta_limpia}'")

        # 1. Obtener productos y sus vectores de la BD (o tu fuente de embeddings)
        # Asumimos que CatalogoEmbedding tiene: user_id, nombre, descripcion, 
        # precio_str_display (ej "$1,200.50"), precio_float (ej 1200.50), 
        # unidad (ej "botella", "caja x6"), embedding_vector
        productos_db = CatalogoEmbedding.query.filter_by(user_id=user_id).all()

        if not productos_db:
            logging.info(f"[CM] 📭 Sin productos en CatalogoEmbedding para user_id={user_id}")
            return None

        productos_validos = [p for p in productos_db if hasattr(p, 'embedding_vector') and p.embedding_vector is not None and len(p.embedding_vector) > 0]
        if not productos_validos:
            logging.warning(f"[CM] 📭 No hay productos con vectores válidos en CatalogoEmbedding para user_id={user_id}")
            return None

        vectores_db = np.array([p.embedding_vector for p in productos_validos])
        
        # 2. Generar embedding para la pregunta del usuario
        try:
            pregunta_vector_list = embed_textos([pregunta_limpia]) # Usa la pregunta limpia
            if not pregunta_vector_list or not pregunta_vector_list[0]:
                logging.error(f"[CM] Error: embed_textos no devolvió un vector para la pregunta: '{pregunta_limpia}'")
                return None
            pregunta_vector = pregunta_vector_list[0]
        except Exception as e_embed:
            logging.error(f"[CM] Error al generar embedding para la pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
            return None

        # 3. Calcular similitudes
        similitudes = cosine_similarity([pregunta_vector], vectores_db)[0]

        # 4. Obtener los mejores N resultados que superen el umbral
        # Crear una lista de tuplas (índice, similitud) para los que superan el umbral
        candidatos_con_score = []
        for i, score in enumerate(similitudes):
            if score >= threshold:
                candidatos_con_score.append((i, score))
        
        if not candidatos_con_score:
            max_sim = np.max(similitudes) if similitudes.size > 0 else -1.0 # Manejar array vacío
            logging.info(f"[CM] 📉 Similaridad demasiado baja para todos los productos (máx: {max_sim:.3f}, umbral: {threshold}) para '{pregunta_limpia}'")
            return None

        # Ordenar los candidatos por similitud descendente
        candidatos_ordenados = sorted(candidatos_con_score, key=lambda item: item[1], reverse=True)
        
        # 5. Formatear los N mejores resultados
        resultados_formateados = []
        for i_prod_valido, score_similitud in candidatos_ordenados[:top_n]:
            prod = productos_validos[i_prod_valido]
            cantidad_detectada = detectar_cantidad_generica(pregunta_limpia)

            nombre_prod = limpiar_texto_simple_cm(getattr(prod, 'nombre', 'Producto Desconocido'))
            desc_prod = limpiar_texto_simple_cm(getattr(prod, 'descripcion', ''))
            
            precio_str_display_prod = getattr(prod, 'precio_str_display', None) # Campo ideal
            precio_float_prod = getattr(prod, 'precio_float', None) # Campo ideal
            unidad_prod = getattr(prod, 'unidad', 'unidad(es)')

            if precio_float_prod is None and hasattr(prod, 'precio'): # Fallback si solo hay 'precio'
                precio_crudo_str = str(getattr(prod, 'precio', ''))
                precio_normalizado = normalizar_precio_cm(precio_crudo_str)
                if precio_normalizado:
                    precio_float_prod = extraer_precio_float_cm(precio_normalizado)
                    # Si no hay precio_str_display, lo creamos a partir del float
                    if precio_float_prod is not None and precio_str_display_prod is None :
                         # Formato ARS (ej. 1.234,50)
                        try:
                            precio_str_display_prod = f"${precio_float_prod:_.2f}".replace('.', '#').replace(',', '.').replace('#', ',')
                        except: # Si el formateo falla por alguna razón
                            precio_str_display_prod = f"${precio_float_prod}"
            
            precio_mostrar = precio_str_display_prod if precio_str_display_prod else "Consultar precio"
            total_mostrar = "No calculable"

            if precio_float_prod is not None and cantidad_detectada > 0:
                total_float = cantidad_detectada * precio_float_prod
                try:
                    total_mostrar = f"${total_float:_.2f}".replace('.', '#').replace(',', '.').replace('#', ',')
                except:
                    total_mostrar = f"${total_float}"


            # Formato del item individual
            info_item = f"📦 Producto: {nombre_prod}"
            if desc_prod and desc_prod != nombre_prod: # No repetir si son iguales
                info_item += f"\n📝 Descripción: {desc_prod[:150]}" # Acortar descripción si es muy larga
            info_item += f"\n💰 Precio ({unidad_prod}): {precio_mostrar}"
            if cantidad_detectada > 1 and precio_float_prod is not None:
                info_item += f"\n🧮 Total por {cantidad_detectada} {unidad_prod}: {total_mostrar}"
            # info_item += f"\n(Ref. score: {score_similitud:.2f})" # Opcional para depuración

            resultados_formateados.append(info_item)

        if not resultados_formateados:
            return None # No debería pasar si candidatos_ordenados no estaba vacío

        # Unir los resultados con un separador claro
        return "\n\n---\n\n".join(resultados_formateados)

    except Exception as e:
        logging.error(f"❌ Error general en buscar_en_catalogo_semantico para '{pregunta_usuario}': {e}", exc_info=True)
        return None