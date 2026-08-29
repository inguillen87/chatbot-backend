from __future__ import annotations

import logging
import re # Para limpieza y parseo
from typing import Dict, Any, List, Optional
from sqlalchemy import func # Para func.lower()

# Dependencias de la aplicación
from models import CatalogoItem, User # Asumiendo que User es el modelo de la PYME
from extensions import db # Para la sesión de base deatos, si es necesario aquí o se pasa
from services.common_utils import crear_mapa_de_columnas_inteligente, limpiar_texto_base, parse_precio_flexible, KEYWORD_MAP
from utils.lazy_module import LazyModule

pd = LazyModule("pandas")
# Necesitaremos una función de similitud si buscamos por nombre de forma flexible
# from services.common_utils import calcular_similitud_levenshtein # Si la movemos/copiamos a common_utils
# O la definimos aquí o importamos Levenshtein directamente
try:
    import Levenshtein
    def calcular_similitud_levenshtein(s1: str, s2: str) -> float:
        if not s1 and not s2: return 1.0
        if not s1 or not s2: return 0.0
        distancia = Levenshtein.distance(s1, s2)
        longitud_max = max(len(s1), len(s2))
        if longitud_max == 0: return 1.0
        return 1 - (distancia / longitud_max)
except ImportError:
    def calcular_similitud_levenshtein(s1: str, s2: str) -> float:
        # Fallback muy simple si Levenshtein no está
        logger.warning("python-Levenshtein no instalado, usando fallback de similitud simple.")
        if s1 == s2: return 1.0
        return 0.0 # O una métrica más simple como Jaccard si es necesario


logger = logging.getLogger(__name__)

# Umbral de similitud para aceptar un producto encontrado por nombre
UMBRAL_SIMILITUD_PRODUCTO_PEDIDO = 0.80 # Ajustable
UMBRAL_SIMILITUD_OCR_PEDIDO = 0.75 # Ligeramente más bajo para OCR


def buscar_item_en_catalogo(
    pyme_user_id: int,
    nombre_busqueda: str,
    sku_busqueda: Optional[str] = None,
    umbral_similitud: float = UMBRAL_SIMILITUD_PRODUCTO_PEDIDO,
    es_ocr: bool = False # Para aplicar lógicas ligeramente diferentes si la búsqueda viene de OCR
) -> Optional[CatalogoItem]:
    """
    Busca un ítem en el catálogo de una PYME por SKU (si se provee o si el nombre parece SKU) y por nombre.

    Args:
        pyme_user_id: ID del usuario PYME dueño del catálogo.
        nombre_busqueda: El nombre del producto a buscar (puede ser un SKU si sku_busqueda es None).
        sku_busqueda: El SKU explícito a buscar (opcional).
        umbral_similitud: Umbral para la comparación de nombres por Levenshtein.
        es_ocr: Si es True, indica que la búsqueda proviene de un texto OCR, pudiendo aplicar heurísticas
                o umbrales ligeramente diferentes.

    Returns:
        El objeto CatalogoItem encontrado, o None.
    """
    item_encontrado = None
    nombre_limpio = limpiar_texto_base(nombre_busqueda)
    sku_limpio = limpiar_texto_base(sku_busqueda) if sku_busqueda else None

    # 1. Búsqueda por SKU explícito (si se proporciona)
    if sku_limpio:
        item_encontrado = CatalogoItem.query.filter_by(user_id=pyme_user_id, sku=sku_limpio).first()
        if item_encontrado:
            logger.info(f"[CATALOG_SEARCH] Encontrado por SKU explícito '{sku_limpio}' -> Cat.ID {item_encontrado.id} ('{item_encontrado.nombre}')")
            return item_encontrado

    # 2. Si no hay SKU explícito Y el nombre_busqueda parece un SKU (especialmente si es OCR)
    #    O si simplemente queremos probar el nombre_busqueda como SKU.
    #    Esta heurística de "parece SKU" es simple.
    if not item_encontrado and (es_ocr and re.match(r"^[A-Za-z0-9-]{3,20}$", nombre_limpio)) or \
       (not es_ocr and not sku_limpio): # Si no es OCR, probar nombre_limpio como SKU si no se dio sku_busqueda
        # No loguear aquí como "búsqueda por SKU" si es solo una prueba del nombre_limpio como SKU.
        # Se logueará si se encuentra de esta forma.
        item_potencial_por_sku_en_nombre = CatalogoItem.query.filter_by(user_id=pyme_user_id, sku=nombre_limpio).first()
        if item_potencial_por_sku_en_nombre:
            logger.info(f"[CATALOG_SEARCH] Encontrado por nombre_busqueda '{nombre_limpio}' actuando como SKU -> Cat.ID {item_potencial_por_sku_en_nombre.id} ('{item_potencial_por_sku_en_nombre.nombre}')")
            return item_potencial_por_sku_en_nombre


    # 3. Búsqueda por similitud de nombre
    if not item_encontrado and nombre_limpio: # Solo buscar por nombre si hay un nombre_limpio
        nombre_norm = nombre_limpio.lower()

        # Búsqueda inicial con ILIKE (más eficiente en DB que Levenshtein en toda la tabla)
        # Usamos un query más permisivo si es OCR, o más restrictivo si es de Excel/directo
        filtro_nombre = CatalogoItem.nombre.ilike(f"%{nombre_norm}%")
        if es_ocr and len(nombre_norm.split()) > 1:
             # Para OCR, si hay varias palabras, también buscar por la primera palabra si es significativa
            primera_palabra_ocr = nombre_norm.split()[0]
            if len(primera_palabra_ocr) > 2:
                 filtro_nombre = func.or_(
                     CatalogoItem.nombre.ilike(f"%{nombre_norm}%"),
                     CatalogoItem.nombre.ilike(f"%{primera_palabra_ocr}%")
                 )
        elif not es_ocr and len(nombre_norm.split()) > 1: # Para Excel, si hay varias palabras, buscar la frase completa
            # Podríamos mantener el contains simple o hacerlo más estricto si es necesario.
            # Por ahora, el ilike(f"%{nombre_norm}%") es un buen punto de partida.
            pass


        candidatos = CatalogoItem.query.filter(
            CatalogoItem.user_id == pyme_user_id,
            filtro_nombre
        ).limit(10).all() # Aumentar un poco el límite de candidatos para dar más margen a Levenshtein

        if candidatos:
            mejor_candidato = None
            max_sim = -1.0

            for candidato in candidatos:
                sim = calcular_similitud_levenshtein(nombre_norm, candidato.nombre.lower())
                if sim > max_sim:
                    max_sim = sim
                    mejor_candidato = candidato

            if mejor_candidato and max_sim >= umbral_similitud:
                item_encontrado = mejor_candidato
                logger.info(f"[CATALOG_SEARCH] Encontrado por similitud de nombre '{nombre_limpio}' (Score: {max_sim:.2f}) -> Cat.ID {item_encontrado.id} ('{item_encontrado.nombre}')")
                return item_encontrado
            else:
                logger.info(f"[CATALOG_SEARCH] Nombre '{nombre_limpio}' no alcanzó umbral de similitud (Mejor: {max_sim:.2f} vs Umbral: {umbral_similitud}). Mejor candidato: {getattr(mejor_candidato, 'nombre', 'N/A')}")

    if not item_encontrado:
        logger.info(f"[CATALOG_SEARCH] Producto '{nombre_busqueda}' (SKU: '{sku_busqueda}') no encontrado en catálogo de PYME {pyme_user_id} con los criterios actuales.")

    return item_encontrado


def procesar_pedido_excel(path_archivo: str, pyme_user_id: int) -> Dict[str, Any]:
    """
    Procesa un archivo Excel subido por un cliente para extraer ítems de un pedido.
    Intenta hacer matching con el catálogo de la PYME.
    """
    pyme_user = db.session.get(User, pyme_user_id)
    if not pyme_user:
        logger.error(f"PYME User con ID {pyme_user_id} no encontrado.")
        return {"error": f"PYME no encontrada.", "items_procesados": [], "items_no_encontrados": []}

    logger.info(f"Procesando archivo Excel de pedido: {path_archivo} para PYME: {pyme_user.id} - {getattr(pyme_user, 'nombre_empresa', 'N/A')}")

    try:
        # Leer todas las filas como string para evitar problemas de tipo con pandas al inicio
        df_raw = pd.read_excel(path_archivo, sheet_name=0, keep_default_na=False, dtype=str, header=None)
    except Exception as e:
        logger.error(f"Error al leer el archivo Excel '{path_archivo}': {e}", exc_info=True)
        return {"error": f"No se pudo leer el archivo Excel: {str(e)}", "items_procesados": [], "items_no_encontrados": []}

    if df_raw.empty:
        logger.warning(f"El archivo Excel '{path_archivo}' está vacío.")
        return {"error": "El archivo Excel está vacío.", "items_procesados": [], "items_no_encontrados": []}

    # Usar crear_mapa_de_columnas_inteligente.
    # Necesitamos asegurarnos que KEYWORD_MAP tenga claves/sinónimos para pedido:
    # ej. "nombre_producto_pedido", "cantidad_pedido", "sku_pedido"
    # O adaptar el uso de KEYWORD_MAP aquí para buscar los campos relevantes de un pedido.
    mapa_info = crear_mapa_de_columnas_inteligente(df_raw.copy()) # Pasar copia

    if not mapa_info:
        logger.error(f"No se pudieron identificar encabezados/columnas en el Excel '{path_archivo}'.")
        return {"error": "No se pudieron identificar las columnas de producto y cantidad en el Excel.", "items_procesados": [], "items_no_encontrados": []}

    mapa_columnas, fila_inicio_datos = mapa_info
    logger.debug(f"Mapa de columnas para pedido Excel: {mapa_columnas}, inicio datos en fila: {fila_inicio_datos}")

    # Identificar qué columnas del DataFrame original (índices 0, 1, ...) corresponden a nuestros campos estándar
    col_idx_nombre = mapa_columnas.get("nombre") # KEYWORD_MAP usa "nombre"
    col_idx_cantidad = mapa_columnas.get("stock") # KEYWORD_MAP usa "stock" para cantidad
    col_idx_sku = mapa_columnas.get("sku")
    # col_idx_precio_excel = mapa_columnas.get("precio") # Si el usuario puede incluir precios

    if col_idx_nombre is None or col_idx_cantidad is None:
        faltantes = []
        if col_idx_nombre is None: faltantes.append("'Nombre/Producto'")
        if col_idx_cantidad is None: faltantes.append("'Cantidad'")
        logger.error(f"Columnas clave {', '.join(faltantes)} no encontradas/mapeadas en el Excel '{path_archivo}'. Mapa obtenido: {mapa_columnas}")
        return {"error": f"Columnas clave {', '.join(faltantes)} no encontradas en el Excel. Por favor, asegúrate de que tu archivo tenga columnas para producto y cantidad.", "items_procesados": [], "items_no_encontrados": []}

    df_datos = df_raw.iloc[fila_inicio_datos:].reset_index(drop=True)
    if df_datos.empty:
        logger.warning(f"No hay filas de datos en el Excel '{path_archivo}' después de la fila de encabezado {fila_inicio_datos}.")
        return {"error": "No hay filas de datos en el Excel después de los encabezados.", "items_procesados": [], "items_no_encontrados": []}

    items_procesados = []
    items_no_encontrados = []

    for index, row in df_datos.iterrows():
        # Acceder a los datos usando los identificadores de columna originales del DataFrame (que son índices si se leyó con header=None)
        nombre_producto_excel = limpiar_texto_base(str(row.get(col_idx_nombre, "")))
        cantidad_excel_str = str(row.get(col_idx_cantidad, ""))
        sku_excel = limpiar_texto_base(str(row.get(col_idx_sku, ""))) if col_idx_sku is not None else None

        fila_excel_num = index + fila_inicio_datos + 1 # +1 para número de fila 1-based para el usuario

        if not nombre_producto_excel or not cantidad_excel_str:
            logger.debug(f"Fila {fila_excel_num}: Nombre o cantidad vacíos. Nombre: '{nombre_producto_excel}', Cant: '{cantidad_excel_str}'.")
            continue # Saltar fila si no hay nombre o cantidad

        try:
            # Intentar convertir cantidad a float primero para manejar decimales, luego a int.
            cantidad_pedido_float = float(cantidad_excel_str.replace(',', '.'))
            cantidad_pedido = int(round(cantidad_pedido_float)) # Usar round para evitar truncamiento simple si es 2.9 -> 3
            if cantidad_pedido <= 0:
                items_no_encontrados.append({"fila_excel": fila_excel_num, "producto_excel": nombre_producto_excel, "sku_excel": sku_excel, "razon": "Cantidad no es positiva."})
                continue
            if abs(cantidad_pedido_float - cantidad_pedido) > 0.001: # Si había decimales significativos
                 logger.warning(f"Fila {fila_excel_num}: Cantidad '{cantidad_excel_str}' tenía decimales, se redondeó a {cantidad_pedido}.")

        except ValueError:
            items_no_encontrados.append({"fila_excel": fila_excel_num, "producto_excel": nombre_producto_excel, "sku_excel": sku_excel, "razon": f"Cantidad '{cantidad_excel_str}' no es un número válido."})
            continue

        # Usar la nueva función de búsqueda
        item_catalogo_encontrado = buscar_item_en_catalogo(
            pyme_user_id=pyme_user_id,
            nombre_busqueda=nombre_producto_excel,
            sku_busqueda=sku_excel, # Pasar el SKU del Excel si existe
            umbral_similitud=UMBRAL_SIMILITUD_PRODUCTO_PEDIDO, # Usar el umbral estándar para Excel
            es_ocr=False # No es OCR
        )

        if item_catalogo_encontrado:
            # Log específico para Excel si se desea, o confiar en los logs de buscar_item_en_catalogo
            logger.info(f"Fila Excel {fila_excel_num}: Producto '{nombre_producto_excel}' (SKU: '{sku_excel}') -> Match con Cat.ID {item_catalogo_encontrado.id} ('{item_catalogo_encontrado.nombre}')")

            precio_str, precio_float, moneda = parse_precio_flexible(item_catalogo_encontrado.precio)
            if precio_float is None:
                items_no_encontrados.append({"fila_excel": fila_excel_num, "producto_excel": nombre_producto_excel, "sku_excel": sku_excel, "razon": f"Producto '{item_catalogo_encontrado.nombre}' (ID: {item_catalogo_encontrado.id}) encontrado pero sin precio válido en catálogo ('{item_catalogo_encontrado.precio}')."})
                continue

            items_procesados.append({
                "catalogo_item_id": item_catalogo_encontrado.id,
                "nombre_producto_excel": nombre_producto_excel, # Nombre original del Excel
                "nombre_producto_catalogo": item_catalogo_encontrado.nombre,
                "sku_catalogo": item_catalogo_encontrado.sku,
                "cantidad_pedido": cantidad_pedido,
                "precio_unitario_catalogo": precio_float,
                "moneda_catalogo": moneda,
                "subtotal_calculado": round(cantidad_pedido * precio_float, 2)
            })
        else:
            logger.info(f"Fila {fila_excel_num}: Producto '{nombre_producto_excel}' (SKU: '{sku_excel}') no encontrado en catálogo de PYME {pyme_user.id}.")
            items_no_encontrados.append({"fila_excel": fila_excel_num, "producto_excel": nombre_producto_excel, "sku_excel": sku_excel, "cantidad_pedido": cantidad_pedido, "razon": "Producto no encontrado en el catálogo."})

    num_procesados = len(items_procesados)
    num_no_encontrados = len(items_no_encontrados)
    mensaje_resumen = f"Procesamiento de Excel de pedido completado. {num_procesados} ítems identificados y listos para confirmar. {num_no_encontrados} ítems no pudieron ser procesados o encontrados en el catálogo."

    logger.info(mensaje_resumen)
    if items_no_encontrados:
        logger.info(f"Detalle de ítems no encontrados/procesados: {items_no_encontrados[:5]}") # Loguear primeros 5

    return {
        "mensaje": mensaje_resumen,
        "items_procesados": items_procesados,
        "items_no_encontrados": items_no_encontrados,
        "pyme_id": pyme_user.id
    }

# Ejemplo de uso (requiere contexto de app Flask para db.session y modelos)
# if __name__ == '__main__':
#     # Crear una app Flask dummy y configurar la DB para probar
#     # ...
#     # mock_pyme_user = User.query.first() # Obtener una Pyme de prueba
#     # resultado = procesar_pedido_excel("ruta/a/un/excel_de_pedido.xlsx", mock_pyme_user.id)
#     # print(resultado)
#     pass
