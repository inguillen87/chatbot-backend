# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re 
from typing import List, Dict, Any, Optional
from .utils import limpiar_texto_base, parse_precio_flexible

logger = logging.getLogger(__name__)

# --- PALABRAS CLAVE PARA DETECTAR LA FILA DE ENCABEZADOS ---
# Ajusta estas keywords según los encabezados más comunes y distintivos en tus archivos
PALABRAS_CLAVE_ENCABEZADOS_PRIORITARIAS = [
    "codigo", "código", "cod.", "sku", "art.", "articulo", 
    "producto", "nombre", "descripción", "descripcion", "detalle", "varietal",
    "precio", "lista", "pvp", "valor", "importe", "$",
    "marca", "linea", "línea", "categoria", "categoría"
]
PALABRAS_CLAVE_ENCABEZADOS_SECUNDARIAS = [
    "unidad", "presentacion", "presentación", "empaque", "formato", "unidades", "unid",
    "stock", "cantidad", "disponible", "existencias", "cant.", 
    "moneda", "currency", "divisa", "iva", "observaciones", "notas"
]

def encontrar_fila_encabezados_excel(df_preliminar: pd.DataFrame, min_coincidencias_prioritarias: int = 2, min_coincidencias_totales: int = 3, max_filas_a_revisar: int = 20) -> Optional[int]:
    """
    Intenta encontrar el índice de la fila que contiene los encabezados.
    Busca una combinación de palabras clave prioritarias y secundarias.
    """
    logger.info(f"[EXCEL_PROC_HEADER] Buscando encabezados en las primeras {min(len(df_preliminar), max_filas_a_revisar)} filas.")
    
    best_header_row_index = None
    highest_score = -1

    for i, row_series in df_preliminar.head(max_filas_a_revisar).iterrows():
        row_values = [str(cell).strip() for cell in row_series.tolist() if pd.notna(cell) and str(cell).strip()]
        if not row_values or len(row_values) < min_coincidencias_totales: # Si la fila tiene muy pocas celdas con texto
            continue

        num_coincidencias_prioritarias = 0
        num_coincidencias_secundarias = 0
        
        celdas_limpias_lower = [limpiar_texto_base(cell) for cell in row_values]

        for cell_text_lower in celdas_limpias_lower:
            if any(re.search(r'\b' + re.escape(kw) + r'\b', cell_text_lower) for kw in PALABRAS_CLAVE_ENCABEZADOS_PRIORITARIAS):
                num_coincidencias_prioritarias += 1
            elif any(re.search(r'\b' + re.escape(kw) + r'\b', cell_text_lower) for kw in PALABRAS_CLAVE_ENCABEZADOS_SECUNDARIAS):
                num_coincidencias_secundarias += 1
        
        total_coincidencias = num_coincidencias_prioritarias + num_coincidencias_secundarias
        
        # Ponderar más las keywords prioritarias
        score_actual = (num_coincidencias_prioritarias * 2) + num_coincidencias_secundarias

        logger.debug(f"[EXCEL_PROC_HEADER] Fila {i}: {row_values} -> Prioritarias: {num_coincidencias_prioritarias}, Secundarias: {num_coincidencias_secundarias}, Score: {score_actual}")

        if num_coincidencias_prioritarias >= min_coincidencias_prioritarias and total_coincidencias >= min_coincidencias_totales:
            if score_actual > highest_score:
                highest_score = score_actual
                best_header_row_index = i
                logger.info(f"[EXCEL_PROC_HEADER] Mejor candidato a encabezado (hasta ahora) en fila índice {i} con score {score_actual}. Contenido: {row_values}")
    
    if best_header_row_index is not None:
        logger.info(f"[EXCEL_PROC_HEADER] Fila de encabezados final seleccionada en índice Excel {best_header_row_index} (score: {highest_score}).")
        return best_header_row_index
        
    logger.warning("[EXCEL_PROC_HEADER] No se encontró una fila de encabezados clara con las palabras clave y heurísticas.")
    return None

def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    productos_extraidos: List[Dict[str, Any]] = []
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Iniciando procesamiento de: {base_filename} para user_id: {pyme_user_id}, rubro: {pyme_rubro_nombre}")

    try:
        ext = os.path.splitext(path)[1].lower()
        df_preliminar = None
        encodings_to_try = ['utf-8', 'latin1', 'iso-8859-1', 'cp1252']

        if ext == ".csv":
            for enc in encodings_to_try:
                try:
                    df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='skip', encoding=enc, skip_blank_lines=False, low_memory=False, keep_default_na=False, na_filter=False)
                    df_preliminar.attrs['encoding'] = enc; logger.info(f"[EXCEL_PROC] CSV '{base_filename}' leído preliminarmente con encoding '{enc}'."); break 
                except UnicodeDecodeError: logger.debug(f"[EXCEL_PROC] Falló CSV '{base_filename}' con encoding '{enc}'.")
                except Exception as e_csv_prelim: logger.warning(f"[EXCEL_PROC] Error inesperado leyendo CSV '{base_filename}' con '{enc}': {e_csv_prelim}"); df_preliminar = None; break 
            if df_preliminar is None: logger.error(f"[EXCEL_PROC] No se pudo leer CSV '{base_filename}'."); return []
        elif ext in [".xlsx", ".xls"]:
            try: 
                df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0, keep_default_na=False, na_filter=False)
                df_preliminar.attrs['encoding'] = 'excel'
                logger.info(f"[EXCEL_PROC] Excel '{base_filename}' leído preliminarmente. Filas: {len(df_preliminar)}")
            except Exception as e_excel_read: logger.error(f"[EXCEL_PROC] Error leyendo archivo Excel '{base_filename}': {e_excel_read}", exc_info=True); return []
        else: logger.error(f"[EXCEL_PROC] Extensión no soportada '{ext}' para '{base_filename}'."); return []
        
        if df_preliminar is None or df_preliminar.empty: logger.warning(f"⚠️ [EXCEL_PROC] Archivo '{base_filename}' está vacío o no se pudo leer preliminarmente."); return []

        idx_fila_encabezados = encontrar_fila_encabezados_excel(df_preliminar, PALABRAS_CLAVE_ENCABEZADOS_PRIORITARIAS + PALABRAS_CLAVE_ENCABEZADOS_SECUNDARIAS)
        
        df: Optional[pd.DataFrame] = None # Tipado explícito
        if idx_fila_encabezados is not None:
            logger.info(f"[EXCEL_PROC] Usando fila en índice {idx_fila_encabezados} como encabezados para leer el DataFrame principal.")
            try:
                # Leer el DataFrame principal usando la fila de encabezados detectada
                # skiprows necesita el número de filas a saltar, no el índice. Si idx_fila_encabezados es el índice (0-based),
                # y queremos que ESA fila sea el encabezado, entonces header=idx_fila_encabezados
                # Si idx_fila_encabezados es el índice de la fila DE LOS DATOS, y el header está una antes,
                # entonces header=idx_fila_encabezados-1 (si >0)
                # La función encontrar_fila_encabezados_excel devuelve el índice de la fila de encabezados.
                # Entonces, pd.read_excel/csv debe usar header=idx_fila_encabezados
                
                current_encoding = df_preliminar.attrs.get('encoding', 'utf-8')
                if ext == ".csv": 
                    df = pd.read_csv(path, sep=None, engine='python', header=idx_fila_encabezados, on_bad_lines='warn', encoding=current_encoding, skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False)
                else: # xlsx, xls
                    df = pd.read_excel(path, engine=None, header=idx_fila_encabezados, sheet_name=0, keep_default_na=False, na_filter=False)
                
                df.dropna(how='all', inplace=True) # Eliminar filas completamente vacías
                if df.empty: logger.warning(f"[EXCEL_PROC] DataFrame vacío después de leer con encabezados en fila {idx_fila_encabezados}.")
                else: logger.info(f"[EXCEL_PROC] DataFrame principal cargado con {len(df)} filas y {len(df.columns)} columnas usando encabezados de fila {idx_fila_encabezados}.")
            except Exception as e_read_main: 
                logger.error(f"[EXCEL_PROC] Error leyendo '{base_filename}' con encabezados en fila {idx_fila_encabezados}: {e_read_main}", exc_info=True)
                df = None # Asegurar que df sea None si falla la lectura
        
        if df is None or df.empty:
            logger.warning(f"[EXCEL_PROC] No se pudo determinar/usar fila de encabezados. Intentando leer desde la primera fila sin detección explícita.")
            # Fallback: intentar leer asumiendo que la primera fila es el encabezado
            try:
                if ext == ".csv": df = pd.read_csv(path, sep=None, engine='python', header=0, on_bad_lines='warn', encoding=df_preliminar.attrs.get('encoding', 'utf-8'), skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False)
                else: df = pd.read_excel(path, engine=None, header=0, sheet_name=0, keep_default_na=False, na_filter=False)
                df.dropna(how='all', inplace=True)
                if df.empty: logger.error(f"[EXCEL_PROC] DataFrame vacío incluso leyendo desde la primera fila."); return []
                logger.info(f"[EXCEL_PROC] DataFrame cargado usando la primera fila como encabezados. {len(df)} filas.")
            except Exception as e_fallback_read:
                logger.error(f"[EXCEL_PROC] Error fatal en lectura de fallback para '{base_filename}': {e_fallback_read}", exc_info=True); return []

        if df.empty: logger.warning(f"[EXCEL_PROC] DataFrame final vacío para '{base_filename}'."); return []
            
        original_columns = df.columns.astype(str).tolist()
        # Limpiar nombres de columnas y quitar las que queden vacías o 'nan'
        df.columns = [limpiar_texto_base(str(col)) for col in df.columns]
        df = df.loc[:, [col for col in df.columns if col and col != 'nan']] 
        
        if df.empty or not df.columns.tolist(): # Chequear si no quedaron columnas válidas
            logger.error(f"[EXCEL_PROC] No quedaron columnas válidas después de limpiar encabezados para '{base_filename}'. Columnas originales: {original_columns}")
            return []
        logger.info(f"[EXCEL_PROC] Columnas originales: {original_columns} -> Columnas finales limpias: {df.columns.tolist()}")

        # --- MAPEO DE COLUMNAS (Misma lista expandida que antes) ---
        map_nombre = ["nombre", "producto", "articulo", "descripción producto", "designacion", "denominacion", "name", "item", "descripción", "varietal"]
        map_descripcion = ["descripcion", "descripción adicional", "info adicional", "caracteristicas", "observaciones", "notas", "description", "product description"]
        map_precio = [ "precio", "valor", "importe", "price", "precio venta", "precio final", "contado", "efectivo", "precio lista", "lista", "sugerido", "pvp", "p.v.p.", "p.v.p", "oferta", "precio oferta", "costo", "tarifa", "$ box", "$ botella", "precio distribuidor", "precio sugerido publico", "unit price", "sale price" ]
        map_unidad = ["unidad", "presentacion", "presentación", "empaque", "formato", "medida", "u/m", "unidades x bulto", "fraccion", "unit/box", "unit/caja", "package", "size", "un/caja"]
        map_categoria = ["categoria", "categoría", "línea", "linea", "rubro", "familia", "tipo", "subrubro", "clase", "department", "category"]
        map_sku = ["sku", "código", "codigo", "cód.", "cod", "art", "articulo nro", "referencia", "ref", "id producto", "item no", "product id", "item code", "código ean", "ean"]
        map_marca = ["marca", "brand", "fabricante", "bodega", "elaborado por", "productor", "manufacturer"]
        map_stock = ["stock", "cantidad", "disponible", "disponibilidad", "existencias", "cant.", "inventory", "quantity", "qty", "available"]
        map_moneda = ["moneda", "currency", "divisa"]

        # --- Tu función encontrar_col_flexible_excel (como estaba) ---
        def encontrar_col_flexible_excel(df_cols_list, posibles_nombres_map_list, field_debug_name=""):
            for nombre_map in posibles_nombres_map_list:
                if nombre_map in df_cols_list: 
                    logger.info(f"[EXCEL_PROC_MAP] Columna EXACTA para '{field_debug_name}': '{nombre_map}'")
                    return nombre_map
            for nombre_map in posibles_nombres_map_list:
                for col_df in df_cols_list:
                    if nombre_map in col_df: 
                        logger.info(f"[EXCEL_PROC_MAP] Columna PARCIAL para '{field_debug_name}': '{col_df}' (buscando '{nombre_map}')")
                        return col_df
            logger.info(f"[EXCEL_PROC_MAP] No se encontró columna para {field_debug_name} con términos: {posibles_nombres_map_list}")
            return None
        
        col_nombre = encontrar_col_flexible_excel(df.columns, map_nombre, "Nombre")
        col_descripcion = encontrar_col_flexible_excel(df.columns, map_descripcion, "Descripción")
        col_precio = encontrar_col_flexible_excel(df.columns, map_precio, "Precio")
        col_unidad = encontrar_col_flexible_excel(df.columns, map_unidad, "Unidad")
        col_categoria = encontrar_col_flexible_excel(df.columns, map_categoria, "Categoría")
        col_sku = encontrar_col_flexible_excel(df.columns, map_sku, "SKU")
        col_marca = encontrar_col_flexible_excel(df.columns, map_marca, "Marca")
        col_stock = encontrar_col_flexible_excel(df.columns, map_stock, "Stock")
        col_moneda_explicita = encontrar_col_flexible_excel(df.columns, map_moneda, "Moneda")

        logger.info(f"[EXCEL_PROC] Mapeo final: Nombre='{col_nombre}', Desc='{col_descripcion}', Precio='{col_precio}', Unidad='{col_unidad}', Categ='{col_categoria}', SKU='{col_sku}', Marca='{col_marca}', Stock='{col_stock}', MonedaCol='{col_moneda_explicita}'")

        if not col_nombre and not col_sku: 
            if not df.columns.empty:
                logger.warning(f"[EXCEL_PROC] No se mapeó Nombre ni SKU. Intentando usar la primera columna '{df.columns[0]}' como Nombre por defecto.")
                col_nombre = df.columns[0] 
            else:
                 logger.error(f"[EXCEL_PROC] No hay columnas para usar como Nombre o SKU. Archivo: '{base_filename}'.")
                 return []
        
        for index, row_data_series in df.iterrows():
            try:
                row = row_data_series.to_dict() # Convertir la fila a diccionario para acceso seguro con .get()
                nombre_prod_construido = ""; marca_val_str = str(row.get(col_marca, "")).strip() if col_marca else ""
                if marca_val_str: nombre_prod_construido += marca_val_str
                
                # Usar col_nombre mapeado (que puede ser el fallback a la primera columna)
                nombre_generico_val_str = str(row.get(col_nombre, "")).strip() if col_nombre else "" 
                if nombre_generico_val_str: nombre_prod_construido = (nombre_prod_construido + " " + nombre_generico_val_str).strip()
                
                if not nombre_prod_construido.strip(): 
                    logger.debug(f"[EXCEL_PROC] Fila {index}: Nombre construido vacío, se omite.")
                    continue

                nombre_final = limpiar_texto_base(nombre_prod_construido); sku_val_str = str(row.get(col_sku, "")).strip() if col_sku else ""; sku_final = limpiar_texto_base(sku_val_str)
                if not nombre_final and not sku_final: logger.info(f"[EXCEL_PROC] Fila {index} omitida: nombre y SKU vacíos."); continue
                if not nombre_final and sku_final: nombre_final = sku_final; logger.info(f"[EXCEL_PROC] Fila {index}: Usando SKU '{sku_final}' como nombre.")
                
                desc_val_str = str(row.get(col_descripcion, "")).strip() if col_descripcion else ""; descripcion_final = limpiar_texto_base(desc_val_str)
                if descripcion_final == nombre_final: descripcion_final = "" # Evitar descripción idéntica al nombre
                
                precio_crudo_val_str = str(row.get(col_precio, "")).strip() if col_precio else ""
                precio_s, precio_f, mon_p = parse_precio_flexible(precio_crudo_val_str)
                moneda_final = mon_p if mon_p else "ARS" # Default a ARS
                if col_moneda_explicita and pd.notna(row.get(col_moneda_explicita)): # Si hay columna de moneda, usarla
                    moneda_de_col = str(row.get(col_moneda_explicita)).strip().upper()
                    if moneda_de_col in ["USD", "ARS", "EUR", "GBP", "CLP"]: moneda_final = moneda_de_col
                
                unidad_val_str = str(row.get(col_unidad, "")).strip() if col_unidad else ""; unidad_final = limpiar_texto_base(unidad_val_str) if unidad_val_str else "unidad"
                categoria_val_str = str(row.get(col_categoria, "")).strip() if col_categoria else ""; categoria_final = limpiar_texto_base(categoria_val_str) if categoria_val_str else pyme_rubro_nombre 
                stock_val_str = str(row.get(col_stock, "1")).strip() if col_stock else "1"; cantidad_final = limpiar_texto_base(stock_val_str) if stock_val_str else "1"

                if len(nombre_final) < 3: logger.info(f"[EXCEL_PROC] Fila {index} omitida: nombre '{nombre_final}' muy corto."); continue
                # Permitir productos sin precio si el nombre es válido
                # if precio_f is None and not re.search(r'(?i)consultar', precio_s or ""): 
                #     logger.info(f"[EXCEL_PROC] Fila {index} omitida: precio no parseable y no es 'consultar' ('{nombre_final}', precio_crudo: '{precio_crudo_val_str}').")
                #     continue

                producto_dict = {
                    "user_id": pyme_user_id, "nombre": nombre_final[:250], "descripcion": descripcion_final[:1000], 
                    "precio_str": precio_s if precio_s else "", "precio_float": precio_f, "moneda": moneda_final, 
                    "unidad": unidad_final[:50], "cantidad_disponible": cantidad_final[:50], 
                    "categoria_qdrant": categoria_final[:100], # Usar un campo dedicado para Qdrant
                    "sku": sku_final[:100], "marca": marca_val_str[:100]
                }
                productos_extraidos.append(producto_dict)
            except Exception as e_row:
                fila_real_excel = index + (idx_fila_encabezados + 1 if idx_fila_encabezados is not None else 1) # Ajustar índice de fila para logs
                logger.error(f"[EXCEL_PROC] Error procesando fila Excel {fila_real_excel} de '{base_filename}': {e_row}", exc_info=True)
        
        logger.info(f"[EXCEL_PROC] Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
        if not productos_extraidos and len(df) > 0 :
             logger.warning(f"[EXCEL_PROC] Se leyeron {len(df)} filas de '{base_filename}' pero no se extrajeron productos válidos.")
        return productos_extraidos

    except FileNotFoundError: logger.error(f"❌[EXCEL_PROC] Archivo no encontrado: {path}"); return []
    except pd.errors.EmptyDataError: logger.warning(f"⚠️[EXCEL_PROC] Archivo '{os.path.basename(path)}' vacío."); return []
    except Exception as e_general:
        logger.error(f"❌[EXCEL_PROC] Error fatal procesando archivo '{os.path.basename(path)}': {e_general}", exc_info=True)
        # No relanzar ValueError genérico, devolver lista vacía para que el flujo continúe
        return [] # Devolver lista vacía para evitar que el endpoint entero falle