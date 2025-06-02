# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re 
from typing import List, Dict, Any, Optional
from .utils import limpiar_texto_base, parse_precio_flexible # Asegúrate que extraer_unidades_y_tipos_precio esté en utils si lo usas aquí

logger = logging.getLogger(__name__)

# --- PALABRAS CLAVE PARA DETECTAR LA FILA DE ENCABEZADOS ---
# Ajustadas para tu archivo "FCA MZA..."
PALABRAS_CLAVE_ENCABEZADOS_PRIORITARIAS = [
    "codigo", "código", "cod.", "sku", "art.", "articulo", "ref", 
    "producto", "nombre", "descripción", "descripcion", "detalle", "varietal", "designacion", 
    "precio", "lista", "pvp", "valor", "importe", "$", "contado", "tarifa", "unitario",
    "marca", "linea", "línea", "categoria", "categoría", "rubro", "tipo"
]
PALABRAS_CLAVE_ENCABEZADOS_SECUNDARIAS = [
    "unidad", "presentacion", "presentación", "empaque", "formato", "unidades", "unid", "u/m", "un.caja", "un/caja", "caja x", "cajas",
    "stock", "cantidad", "disponible", "existencias", "cant.", 
    "moneda", "currency", "divisa", "iva", "observaciones", "notas", "ean", "caja/pallet", "empaquetado", "talle",
    "envase", "cc", "ml", "lts", "kg", "grs", "color", "origen", "bodega" # Añadidas algunas más genéricas
]

def encontrar_fila_encabezados_excel(
    df_check: pd.DataFrame, 
    keywords_prioritarias: List[str], 
    keywords_secundarias: List[str],
    min_prioritarias_req: int = 2, 
    min_totales_req: int = 4, # Aumentado un poco para mayor seguridad
    max_filas_a_revisar: int = 20 
) -> Optional[int]:
    logger.info(f"[EXCEL_PROC_HEADER] Buscando encabezados en primeras {min(len(df_check), max_filas_a_revisar)} filas (min_prioritarias={min_prioritarias_req}, min_totales={min_totales_req}).")
    best_header_row_index: Optional[int] = None
    highest_score: float = -1.0

    for i, row_series in df_check.head(max_filas_a_revisar).iterrows():
        row_values_str = [str(cell).strip() for cell in row_series.tolist() if pd.notna(cell) and str(cell).strip()]
        if not row_values_str or len(row_values_str) < min_totales_req: continue

        num_coincidencias_prioritarias = 0; num_coincidencias_secundarias = 0
        celdas_limpias_lower = [limpiar_texto_base(cell) for cell in row_values_str]
        found_prioritarias = set(); found_secundarias = set()

        for cell_text_lower in celdas_limpias_lower:
            for kw in keywords_prioritarias:
                if re.search(r'\b' + re.escape(kw) + r'\b', cell_text_lower): found_prioritarias.add(kw); break 
            for kw in keywords_secundarias:
                if re.search(r'\b' + re.escape(kw) + r'\b', cell_text_lower): found_secundarias.add(kw); break
        
        num_coincidencias_prioritarias = len(found_prioritarias)
        num_coincidencias_secundarias = len(found_secundarias)
        total_coincidencias_distintas = num_coincidencias_prioritarias + num_coincidencias_secundarias
        
        score_actual = (num_coincidencias_prioritarias * 2.5) + (num_coincidencias_secundarias * 1.0)
        if len(row_values_str) > 0 : score_actual *= (total_coincidencias_distintas / len(row_values_str)) 
        
        logger.debug(f"[EXCEL_PROC_HEADER] Fila Excel {i}: {row_values_str} -> Prioritarias: {num_coincidencias_prioritarias}, Secundarias: {num_coincidencias_secundarias}, Score: {score_actual:.2f}")

        if num_coincidencias_prioritarias >= min_prioritarias_req and total_coincidencias_distintas >= min_totales_req:
            if score_actual > highest_score:
                highest_score = score_actual; best_header_row_index = i
                logger.info(f"[EXCEL_PROC_HEADER] Mejor candidato encabezado (fila índice {i}) con score {score_actual:.2f}. Contenido: {row_values_str}")
    
    if best_header_row_index is not None:
        logger.info(f"[EXCEL_PROC_HEADER] Fila encabezados final seleccionada en índice Excel {best_header_row_index} (score: {highest_score:.2f}).")
        return best_header_row_index
        
    logger.warning("[EXCEL_PROC_HEADER] No se encontró fila encabezados clara con keywords y heurísticas.")
    return None

def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    productos_extraidos: List[Dict[str, Any]] = []
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Iniciando procesamiento de: {base_filename} para user_id: {pyme_user_id}, rubro: {pyme_rubro_nombre}")

    try:
        ext = os.path.splitext(path)[1].lower()
        df_preliminar: Optional[pd.DataFrame] = None
        encodings_to_try = ['utf-8', 'latin1', 'iso-8859-1', 'cp1252']
        if ext == ".csv":
            for enc in encodings_to_try:
                try:
                    df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='skip', encoding=enc, skip_blank_lines=False, low_memory=False, keep_default_na=False, na_filter=False, dtype=str)
                    df_preliminar.attrs['encoding'] = enc; logger.info(f"[EXCEL_PROC] CSV '{base_filename}' leído preliminarmente con encoding '{enc}'."); break 
                except UnicodeDecodeError: logger.debug(f"[EXCEL_PROC] Falló CSV '{base_filename}' con encoding '{enc}'.")
                except Exception as e_csv_prelim: logger.warning(f"[EXCEL_PROC] Error inesperado leyendo CSV '{base_filename}' con '{enc}': {e_csv_prelim}"); df_preliminar = None; break 
            if df_preliminar is None: logger.error(f"[EXCEL_PROC] No se pudo leer CSV '{base_filename}'."); return []
        elif ext in [".xlsx", ".xls"]:
            try: 
                df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str)
                df_preliminar.attrs['encoding'] = 'excel'
                logger.info(f"[EXCEL_PROC] Excel '{base_filename}' leído preliminarmente. Filas: {len(df_preliminar)}")
            except Exception as e_excel_read: logger.error(f"[EXCEL_PROC] Error leyendo archivo Excel '{base_filename}': {e_excel_read}", exc_info=True); return []
        else: logger.error(f"[EXCEL_PROC] Extensión no soportada '{ext}' para '{base_filename}'."); return []
        
        if df_preliminar is None or df_preliminar.empty: logger.warning(f"⚠️ [EXCEL_PROC] Archivo '{base_filename}' vacío o no leído."); return []
        
        idx_fila_encabezados = encontrar_fila_encabezados_excel(
            df_preliminar, PALABRAS_CLAVE_ENCABEZADOS_PRIORITARIAS, PALABRAS_CLAVE_ENCABEZADOS_SECUNDARIAS
        )
        
        df: Optional[pd.DataFrame] = None
        if idx_fila_encabezados is not None:
            logger.info(f"[EXCEL_PROC] Usando fila en índice {idx_fila_encabezados} como encabezados para leer DataFrame principal.")
            try:
                current_encoding = df_preliminar.attrs.get('encoding', 'utf-8')
                # El argumento 'header' en read_csv/read_excel es el índice de la fila a usar como nombres de columna.
                # Las filas ANTERIORES a esta se saltan.
                if ext == ".csv": 
                    df = pd.read_csv(path, sep=None, engine='python', header=idx_fila_encabezados, on_bad_lines='skip', encoding=current_encoding, skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False, dtype=str)
                else: 
                    df = pd.read_excel(path, engine=None, header=idx_fila_encabezados, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str)
                
                if df is not None and not df.empty:
                    df.columns = [limpiar_texto_base(str(col)) for col in df.columns]
                    df.dropna(how='all', inplace=True) 
                    df = df.loc[:, [col for col in df.columns if col and col != 'nan' and col.strip() != '']]
                    if df.empty: logger.warning(f"[EXCEL_PROC] DataFrame vacío después de leer con encabezados en fila {idx_fila_encabezados} y limpiar.")
                    else: logger.info(f"[EXCEL_PROC] DataFrame principal cargado con {len(df)} filas y columnas: {df.columns.tolist()}.")
                else: logger.warning(f"[EXCEL_PROC] DataFrame None o vacío después de intentar leer con encabezados en fila {idx_fila_encabezados}.")
            except Exception as e_read_main: 
                logger.error(f"[EXCEL_PROC] Error leyendo '{base_filename}' con encabezados en fila {idx_fila_encabezados}: {e_read_main}", exc_info=True)
                df = None
        
        if df is None or df.empty: # Si la detección de encabezados falló o resultó en un df vacío
            logger.warning(f"[EXCEL_PROC] No se pudo usar fila de encabezados detectada. Intentando leer sin encabezados específicos (Pandas infiere o usa índices).")
            try:
                if ext == ".csv": df = pd.read_csv(path, sep=None, engine='python', header=0, on_bad_lines='skip', encoding=df_preliminar.attrs.get('encoding', 'utf-8'), skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False, dtype=str)
                else: df = pd.read_excel(path, engine=None, header=0, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str)
                df.columns = [limpiar_texto_base(str(col)) for col in df.columns] # Limpiar los headers inferidos por Pandas
                df.dropna(how='all', inplace=True)
                df = df.loc[:, [col for col in df.columns if col and col != 'nan' and col.strip() != '']]
                if df.empty: logger.error(f"[EXCEL_PROC] DataFrame vacío incluso leyendo con header=0."); return []
                logger.info(f"[EXCEL_PROC] DataFrame cargado usando primera fila como encabezados (o inferidos por Pandas). Columnas: {df.columns.tolist()}. {len(df)} filas.")
            except Exception as e_fallback_read:
                logger.error(f"[EXCEL_PROC] Error fatal en lectura de fallback para '{base_filename}': {e_fallback_read}", exc_info=True); return []

        if df.empty: logger.warning(f"[EXCEL_PROC] DataFrame final vacío para '{base_filename}'."); return []
            
        df_column_list = df.columns.astype(str).tolist()

        # --- MAPEO DE COLUMNAS (Mantenemos tus listas map_nombre, etc.) ---
        map_nombre = ["nombre", "producto", "articulo", "descripción producto", "designacion", "denominacion", "name", "item", "descripción", "varietal", "título"]
        map_descripcion = ["descripcion", "descripción adicional", "info adicional", "caracteristicas", "observaciones", "notas", "description"]
        map_precio = [ "precio", "valor", "importe", "price", "venta", "final", "lista", "sugerido", "pvp", "$ botella", "$ caja", "precio unitario", "contado", "tarifa", "precio distribuidor", "precio publico"]
        map_unidad = ["unidad", "presentacion", "presentación", "empaque", "formato", "u/m", "un/caja", "unidades por caja", "caja x", "pack x", "unid.", "contenido neto"]
        map_categoria = ["categoria", "categoría", "línea", "linea", "rubro", "familia", "tipo", "subrubro", "clase", "department"]
        map_sku = ["sku", "código", "codigo", "cód.", "cod", "art", "artículo nro", "referencia", "ref", "id producto", "item no", "product id", "item code", "ean"]
        map_marca = ["marca", "brand", "fabricante", "bodega", "elaborado por", "productor", "manufacturer"]
        map_stock = ["stock", "cantidad", "disponible", "disponibilidad", "existencias", "cant.", "inventory", "quantity", "qty", "available"]
        map_moneda = ["moneda", "currency", "divisa"]
        
        def encontrar_col_flexible_excel(cols_disponibles, posibles_nombres_map, field_debug_name=""):
            for nombre_map_exacto in posibles_nombres_map: # Primero búsqueda exacta (case-insensitive ya hecho)
                if nombre_map_exacto in cols_disponibles: 
                    logger.info(f"[EXCEL_PROC_MAP] Columna EXACTA para '{field_debug_name}': '{nombre_map_exacto}'")
                    return nombre_map_exacto
            for nombre_map_parcial in posibles_nombres_map: # Luego búsqueda parcial
                for col_df_actual in cols_disponibles:
                    if nombre_map_parcial in col_df_actual: 
                        logger.info(f"[EXCEL_PROC_MAP] Columna PARCIAL para '{field_debug_name}': '{col_df_actual}' (buscando '{nombre_map_parcial}')")
                        return col_df_actual
            logger.info(f"[EXCEL_PROC_MAP] No se encontró columna para '{field_debug_name}' con términos: {posibles_nombres_map}")
            return None
        
        col_nombre = encontrar_col_flexible_excel(df_column_list, map_nombre, "Nombre")
        col_descripcion = encontrar_col_flexible_excel(df_column_list, map_descripcion, "Descripción")
        col_precio = encontrar_col_flexible_excel(df_column_list, map_precio, "Precio")
        col_unidad = encontrar_col_flexible_excel(df_column_list, map_unidad, "Unidad")
        col_categoria = encontrar_col_flexible_excel(df_column_list, map_categoria, "Categoría")
        col_sku = encontrar_col_flexible_excel(df_column_list, map_sku, "SKU")
        col_marca = encontrar_col_flexible_excel(df_column_list, map_marca, "Marca")
        col_stock = encontrar_col_flexible_excel(df_column_list, map_stock, "Stock")
        col_moneda_explicita = encontrar_col_flexible_excel(df_column_list, map_moneda, "Moneda")

        logger.info(f"[EXCEL_PROC] Mapeo final: Nombre='{col_nombre}', Desc='{col_descripcion}', Precio='{col_precio}', Unidad='{col_unidad}', Categ='{col_categoria}', SKU='{col_sku}', Marca='{col_marca}', Stock='{col_stock}', MonedaCol='{col_moneda_explicita}'")

        if not col_nombre and not col_sku: 
            # Si después de todo el mapeo, no tenemos ni nombre ni SKU, usar la primera columna disponible como nombre.
            # Esto es un último recurso si los encabezados son completamente irreconocibles o están ausentes.
            if df.columns.tolist(): # Chequear si df.columns no está vacío
                col_nombre = df.columns[0] # df.columns ya son strings limpios (o índices como strings)
                logger.warning(f"[EXCEL_PROC] No se mapeó Nombre ni SKU. Usando primera columna disponible '{col_nombre}' como Nombre por defecto.")
            else:
                 logger.error(f"[EXCEL_PROC] No hay columnas disponibles en el DataFrame después de la limpieza. No se pueden extraer productos de '{base_filename}'.")
                 return []
        
        for index, row_data_series in df.iterrows():
            try:
                row = row_data_series.to_dict() # Clave para evitar "ValueError: The truth value of a Series is ambiguous"
                
                nombre_prod_construido = ""; marca_val_str = str(row.get(col_marca, "")).strip() if col_marca else ""
                if marca_val_str: nombre_prod_construido += marca_val_str
                
                nombre_generico_val_str = str(row.get(col_nombre, "")).strip() if col_nombre else ""
                if nombre_generico_val_str: nombre_prod_construido = (nombre_prod_construido + " " + nombre_generico_val_str).strip()
                
                if not nombre_prod_construido.strip(): continue

                nombre_final = limpiar_texto_base(nombre_prod_construido); sku_val_str = str(row.get(col_sku, "")).strip() if col_sku else ""; sku_final = limpiar_texto_base(sku_val_str)
                if not nombre_final and not sku_final: continue
                if not nombre_final and sku_final: nombre_final = sku_final
                
                desc_val_str = str(row.get(col_descripcion, "")).strip() if col_descripcion else ""; descripcion_final = limpiar_texto_base(desc_val_str)
                if descripcion_final == nombre_final: descripcion_final = ""
                
                precio_crudo_val_str = str(row.get(col_precio, "")).strip() if col_precio else ""
                precio_s, precio_f, mon_p = parse_precio_flexible(precio_crudo_val_str)
                moneda_final = mon_p if mon_p else "ARS"
                if col_moneda_explicita and pd.notna(row.get(col_moneda_explicita)):
                    moneda_de_col = str(row.get(col_moneda_explicita)).strip().upper()
                    if moneda_de_col in ["USD", "ARS", "EUR"]: moneda_final = moneda_de_col
                
                unidad_val_str = str(row.get(col_unidad, "")).strip() if col_unidad else ""; unidad_final = limpiar_texto_base(unidad_val_str) if unidad_val_str else "unidad"
                categoria_val_str = str(row.get(col_categoria, "")).strip() if col_categoria else ""; categoria_final = limpiar_texto_base(categoria_val_str) if categoria_val_str else pyme_rubro_nombre 
                stock_val_str = str(row.get(col_stock, "1")).strip() if col_stock else "1"; cantidad_final = limpiar_texto_base(stock_val_str) if stock_val_str else "1"

                if len(nombre_final) < 2: # Nombre muy corto, probablemente no es un producto
                    logger.debug(f"[EXCEL_PROC] Fila {index} omitida: nombre '{nombre_final}' demasiado corto."); continue
                
                # Considerar un producto válido si tiene nombre Y (precio o descripción o sku)
                if not ( (precio_f is not None or re.search(r'(?i)consultar', precio_s or "")) or descripcion_final or sku_final ):
                     logger.debug(f"[EXCEL_PROC] Fila {index} omitida: '{nombre_final}' no tiene precio, ni descripción, ni SKU significativos.")
                     continue

                producto_dict = {
                    "user_id": pyme_user_id, "nombre": nombre_final[:250], "descripcion": descripcion_final[:1000], 
                    "precio_str": precio_s if precio_s else "", "precio_float": precio_f, "moneda": moneda_final, 
                    "unidad": unidad_final[:50], "cantidad_disponible": cantidad_final[:50], 
                    "categoria_qdrant": categoria_final[:100], "sku": sku_final[:100], "marca": marca_val_str[:100]
                }
                productos_extraidos.append(producto_dict)
            except Exception as e_row:
                fila_real_excel = index + (idx_fila_encabezados + 1 if idx_fila_encabezados is not None else 0) +1 
                logger.error(f"[EXCEL_PROC] Error procesando fila Excel {fila_real_excel} de '{base_filename}': {e_row}", exc_info=True)
        
        logger.info(f"[EXCEL_PROC] Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
        if not productos_extraidos and len(df) > 0 :
             logger.warning(f"[EXCEL_PROC] Se leyeron {len(df)} filas de '{base_filename}' pero no se extrajeron productos válidos.")
        return productos_extraidos

    except FileNotFoundError: logger.error(f"❌[EXCEL_PROC] Archivo no encontrado: {path}"); return []
    except pd.errors.EmptyDataError: logger.warning(f"⚠️[EXCEL_PROC] Archivo '{os.path.basename(path)}' vacío."); return []
    except Exception as e_general:
        logger.error(f"❌[EXCEL_PROC] Error fatal procesando archivo '{os.path.basename(path)}': {e_general}", exc_info=True)
        return []