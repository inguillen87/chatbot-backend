# En tu archivo services/catalogo_matcher.py

import re
import logging
import numpy as np
from models import CatalogoEmbedding 
from services.cohere_ai import embed_textos
from sklearn.metrics.pairwise import cosine_similarity
from .utils import limpiar_texto_base, parse_precio_flexible # <--- IMPORTAR DE UTILS

def detectar_cantidad_generica(texto: str) -> int:
    # ... (tu función se mantiene, podrías moverla a utils.py también si la usas en más lugares) ...
    match_con_palabra = re.search(r"(?:quiero|necesito|dame|comprar|cantidad|para|por)\s*(\d+)\b|\b(\d+)\s*(?:cajas|unidades|unids|u\.|pares|sets|kits|packs)\b", texto.lower())
    if match_con_palabra:
        for i in range(1, len(match_con_palabra.groups()) + 1):
            if match_con_palabra.group(i): return int(match_con_palabra.group(i))
    match_simple = re.search(r"(\d+)", texto.lower())
    if match_simple: return int(match_simple.group(1))
    return 1

def buscar_en_catalogo_semantico(pregunta_usuario: str, user_id: int, threshold: float = 0.75, top_n: int = 1) -> str | None:
    try:
        pregunta_limpia = limpiar_texto_base(pregunta_usuario)
        logging.info(f"[CM] Buscando en catálogo semántico para user_id={user_id}, pregunta='{pregunta_limpia}'")

        productos_db = CatalogoEmbedding.query.filter_by(user_id=user_id).all()
        if not productos_db: # ... (sin cambios) ...
             logging.info(f"[CM] 📭 Sin productos en CatalogoEmbedding para user_id={user_id}")
             return None

        productos_validos = [p for p in productos_db if hasattr(p, 'embedding_vector') and p.embedding_vector is not None and len(p.embedding_vector) > 0]
        if not productos_validos: # ... (sin cambios) ...
             logging.warning(f"[CM] 📭 No hay productos con vectores válidos en CatalogoEmbedding para user_id={user_id}")
             return None
        
        vectores_db = np.array([p.embedding_vector for p in productos_validos])
        # ... (lógica de embedding de pregunta y similitud - sin cambios) ...
        try:
            pregunta_vector_list = embed_textos([pregunta_limpia])
            if not pregunta_vector_list or not pregunta_vector_list[0]:
                logging.error(f"[CM] Error: embed_textos no devolvió vector para: '{pregunta_limpia}'")
                return None
            pregunta_vector = pregunta_vector_list[0]
        except Exception as e_embed:
            logging.error(f"[CM] Error embedding pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
            return None

        similitudes = cosine_similarity([pregunta_vector], vectores_db)[0]
        candidatos_con_score = []
        for i, score in enumerate(similitudes):
            if score >= threshold:
                candidatos_con_score.append((i, score))
        
        if not candidatos_con_score: # ... (sin cambios) ...
            max_sim = np.max(similitudes) if similitudes.size > 0 else -1.0 
            logging.info(f"[CM] 📉 Similaridad baja (máx: {max_sim:.3f}, umbral: {threshold}) para '{pregunta_limpia}'")
            return None

        candidatos_ordenados = sorted(candidatos_con_score, key=lambda item: item[1], reverse=True)
        
        resultados_formateados = []
        for i_prod_valido, score_similitud in candidatos_ordenados[:top_n]:
            prod = productos_validos[i_prod_valido]
            cantidad_detectada = detectar_cantidad_generica(pregunta_limpia)

            nombre_prod = limpiar_texto_base(getattr(prod, 'nombre', 'Producto Desconocido'))
            desc_prod = limpiar_texto_base(getattr(prod, 'descripcion', ''))
            
            # Obtener precio del modelo CatalogoEmbedding
            # Asumimos que el modelo puede tener 'precio_str_display' y 'precio_float'
            # o un campo 'precio' genérico que intentamos parsear.
            precio_str_prod = getattr(prod, 'precio_str_display', None)
            precio_float_prod = getattr(prod, 'precio_float', None)
            moneda_prod = getattr(prod, 'moneda', "ARS") # Asumir ARS si no está
            
            if precio_float_prod is None and hasattr(prod, 'precio') and prod.precio:
                # Si no hay precio_float, intentar parsear el campo 'precio'
                precio_str_parseado_temp, precio_flt_parseado_temp, moneda_parseada_temp = parse_precio_flexible(str(prod.precio))
                if precio_flt_parseado_temp is not None:
                    precio_float_prod = precio_flt_parseado_temp
                    # Si no teníamos un precio_str_display, usamos el parseado (que es el número sin símbolos)
                    # OJO: parse_precio_flexible devuelve el NÚMERO como string, no el string original completo.
                    # Necesitarías ajustar parse_precio_flexible o tener un campo de display en el modelo.
                    precio_str_prod = precio_str_parseado_temp if precio_str_parseado_temp else str(prod.precio) # fallback
                    if moneda_parseada_temp: moneda_prod = moneda_parseada_temp

            precio_mostrar = precio_str_prod if precio_str_prod else (f"${precio_float_prod:.2f}" if precio_float_prod is not None else "Consultar precio")
            if precio_float_prod is not None and moneda_prod and "$" not in precio_mostrar:
                precio_mostrar = f"{moneda_prod} {precio_mostrar}" # Añadir moneda si no tiene $

            total_mostrar = "No calculable"
            if precio_float_prod is not None and cantidad_detectada > 0:
                total_float = cantidad_detectada * precio_float_prod
                total_mostrar = f"{moneda_prod} {total_float:.2f}" # Ajustar formato si es necesario

            unidad_prod = limpiar_texto_base(getattr(prod, 'unidad', 'unidad(es)'))
            
            info_item = f"📦 Producto: {nombre_prod}"
            if desc_prod and desc_prod != nombre_prod:
                info_item += f"\n📝 Descripción: {desc_prod[:150]}"
            info_item += f"\n💰 Precio ({unidad_prod}): {precio_mostrar}"
            if cantidad_detectada > 1 and precio_float_prod is not None:
                info_item += f"\n🧮 Total por {cantidad_detectada} {unidad_prod}: {total_mostrar}"
            
            resultados_formateados.append(info_item)

        return "\n\n---\n\n".join(resultados_formateados) if resultados_formateados else None

    except Exception as e:
        logging.error(f"❌ Error general en buscar_en_catalogo_semantico para '{pregunta_usuario}': {e}", exc_info=True)
        return None