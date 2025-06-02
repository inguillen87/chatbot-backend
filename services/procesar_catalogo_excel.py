# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re 
from typing import List, Dict, Any, Optional # Añadido Optional
from .utils import limpiar_texto_base, parse_precio_flexible

logger = logging.getLogger(__name__)

def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> list[dict]: # Añadido pyme_rubro_nombre
    productos_extraidos = []
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Iniciando procesamiento de: {base_filename} para user_id: {pyme_user_id}, rubro: {pyme_rubro_nombre}")

    try:
        ext = os.path.splitext(path)[1].lower()
        df_preliminar = None
        encodings_to_try = ['utf-8', 'latin1', 'iso-8859-1', 'cp1252']
        if ext == ".csv":
            for enc in encodings_to_try:
                try:
                    df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='warn', encoding=enc, skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False)
                    df_preliminar.attrs['encoding'] = enc; logger.info(f"[EXCEL_PROC] CSV '{base_filename}' leído preliminarmente con encoding '{enc}'."); break 
                except UnicodeDecodeError: logger.warning(f"[EXCEL_PROC] Error leyendo CSV '{base_filename}' con encoding '{enc}'. Intentando siguiente...")
                except Exception as e_csv_prelim: logger.warning(f"[EXCEL_PROC] Error inesperado leyendo CSV '{base_filename}' con '{enc}': {e_csv_prelim}"); df_preliminar = None; break 
            if df_preliminar is None: logger.error(f"[EXCEL_PROC] No se pudo leer CSV '{base_filename}'."); return []
        elif ext in [".xlsx", ".xls"]:
            try: df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0, keep_default_na=False, na_filter=False); df_preliminar.attrs['encoding'] = 'excel'
            except Exception as e_excel_read: logger.error(f"[EXCEL_PROC] Error leyendo Excel '{base_filename}': {e_excel_read}", exc_info=True); return []
        else: logger.error(f"[EXCEL_PROC] Extensión no soportada '{ext}' para '{base_filename}'."); return []
        if df_preliminar is None or df_preliminar.empty: logger.warning(f"⚠️ [EXCEL_PROC] Archivo '{base_filename}' vacío o no leído."); return []
        
        keywords_header = ["nombre", "producto", "precio", "descripción", "sku", "código", "categoria", "marca", "unidad", "stock", "valor", "articulo", "item", "title", "detalle", "designación", "denominacion", "pvp", "referencia"]
        
        def encontrar_fila_encabezados_excel(df_check, posibles_palabras, min_coincidencias=2):
            logger.info(f"[EXCEL_PROC] Buscando encabezados en primeras {min(len(df_check), 20)} filas (min_coincidencias={min_coincidencias}).")
            best_i = None; max_coincidencias_found = 0
            for i, row_series in df_check.head(20).iterrows():
                row_list = row_series.astype(str).tolist() # Convertir a lista de strings
                coincidencias = 0; celdas_con_keywords = set(); num_celdas_con_texto = 0
                for cell_idx, cell_value in enumerate(row_list):
                    if cell_value and cell_value.strip():
                        num_celdas_con_texto += 1; cell_lower = cell_value.lower()
                        for palabra_clave in posibles_palabras:
                            if re.search(r'\b' + re.escape(palabra_clave.lower()) + r'\b', cell_lower): celdas_con_keywords.add(cell_idx); break
                coincidencias = len(celdas_con_keywords)
                if coincidencias >= min_coincidencias and num_celdas_con_texto > coincidencias:
                    if coincidencias > max_coincidencias_found: max_coincidencias_found = coincidencias; best_i = i; logger.info(f"[EXCEL_PROC] Mejor candidato encabezado (fila {i}) con {coincidencias} keywords: {row_list}")
            if best_i is not None: logger.info(f"[EXCEL_PROC] Fila encabezados seleccionada en índice {best_i} con {max_coincidencias_found} coincidencias."); return best_i
            logger.warning("[EXCEL_PROC] No se encontró fila encabezados clara con keywords."); return None

        idx_fila_encabezados = encontrar_fila_encabezados_excel(df_preliminar, keywords_header, min_coincidencias=2)
        df = None
        if idx_fila_encabezados is not None:
            logger.info(f"[EXCEL_PROC] Usando fila en índice {idx_fila_encabezados} como encabezados.")
            try:
                current_encoding = df_preliminar.attrs.get('encoding', 'utf-8')
                if ext == ".csv": df = pd.read_csv(path, sep=None, engine='python', header=idx_fila_encabezados, on_bad_lines='warn', encoding=current_encoding, skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False)
                else: df = pd.read_excel(path, engine=None, header=idx_fila_encabezados, sheet_name=0, keep_default_na=False, na_filter=False)
                df.dropna(how='all', inplace=True)
                if df.empty: logger.warning(f"[EXCEL_PROC] DataFrame vacío después de leer con encabezados detectados.")
                else: logger.info(f"[EXCEL_PROC] DataFrame principal cargado con {len(df)} filas y {len(df.columns)} columnas.")
            except Exception as e_read_main: logger.error(f"[EXCEL_PROC] Error leyendo '{base_filename}' después de detectar encabezados: {e_read_main}", exc_info=True); return []
        else: 
            logger.warning(f"[EXCEL_PROC] No se determinó fila encabezados por keywords. Intentando usar primera fila si tiene texto...")
            if not df_preliminar.empty:
                primera_fila_como_str = df_preliminar.iloc[0].astype(str).tolist()
                primera_fila_valores_con_texto = [str(x).strip() for x in primera_fila_como_str if str(x).strip() and str(x).lower() != 'nan']
                if len(primera_fila_valores_con_texto) >= 2: 
                    df = df_preliminar.copy(); df.columns = [limpiar_texto_base(str(col)) for col in df.iloc[0]] 
                    df = df[1:].reset_index(drop=True); df.dropna(how='all', inplace=True)
                    logger.info(f"[EXCEL_PROC] Usando primera fila como encabezados. Columnas: {df.columns.tolist()}")
                else: logger.error(f"[EXCEL_PROC] Primera fila no es encabezado válido para '{base_filename}'. Celdas con texto: {len(primera_fila_valores_con_texto)}"); return []
            else: logger.error(f"[EXCEL_PROC] df_preliminar vacío para '{base_filename}'."); return []

        if df is None or df.empty: logger.warning(f"[EXCEL_PROC] DataFrame vacío para '{base_filename}'."); return []
            
        original_columns = df.columns.astype(str).tolist(); df.columns = [limpiar_texto_base(str(col)) for col in df.columns if str(col).strip()]
        df = df.loc[:, [col for col in df.columns if col and col != 'nan']]
        if df.empty: logger.warning(f"[EXCEL_PROC] DataFrame sin columnas válidas tras limpiar encabezados para '{base_filename}'."); return []
        logger.info(f"[EXCEL_PROC] Columnas originales: {original_columns} -> Columnas finales para mapeo: {df.columns.tolist()}")

        map_nombre = ["nombre", "producto", "articulo", "descripción producto", "designacion", "denominacion", "name", "item", "descripción", "varietal"]; map_descripcion = ["descripcion", "descripción adicional", "info adicional", "caracteristicas", "observaciones"]; map_precio = [ "precio", "valor", "importe", "price", "venta", "final", "lista", "sugerido", "pvp", "$ botella", "$ caja"]; map_unidad = ["unidad", "presentacion", "empaque", "formato", "un/caja"]; map_categoria = ["categoria", "línea", "rubro", "tipo"]; map_sku = ["sku", "código", "cod", "art", "referencia"]; map_marca = ["marca", "bodega", "fabricante"]; map_stock = ["stock", "cantidad", "disponible"]; map_moneda = ["moneda"]
        
        def encontrar_col_flexible_excel(df_cols_list, posibles_nombres_map_list): # ... (como estaba)
            for nombre_map in posibles_nombres_map_list:
                if nombre_map in df_cols_list: logger.info(f"[EXCEL_PROC_MAP] Col exacta: '{nombre_map}' para {posibles_nombres_map_list[0]}"); return nombre_map
            for nombre_map in posibles_nombres_map_list:
                for col_df in df_cols_list:
                    if nombre_map in col_df: logger.info(f"[EXCEL_PROC_MAP] Col parcial: '{col_df}' (buscando '{nombre_map}') para {posibles_nombres_map_list[0]}"); return col_df
            logger.info(f"[EXCEL_PROC_MAP] No se encontró columna para {posibles_nombres_map_list[0]}"); return None
        
        col_nombre = encontrar_col_flexible_excel(df.columns, map_nombre); col_descripcion = encontrar_col_flexible_excel(df.columns, map_descripcion); col_precio = encontrar_col_flexible_excel(df.columns, map_precio); col_unidad = encontrar_col_flexible_excel(df.columns, map_unidad); col_categoria = encontrar_col_flexible_excel(df.columns, map_categoria); col_sku = encontrar_col_flexible_excel(df.columns, map_sku); col_marca = encontrar_col_flexible_excel(df.columns, map_marca); col_stock = encontrar_col_flexible_excel(df.columns, map_stock); col_moneda_explicita = encontrar_col_flexible_excel(df.columns, map_moneda)
        logger.info(f"[EXCEL_PROC] Mapeo final: Nombre='{col_nombre}', Desc='{col_descripcion}', Precio='{col_precio}', Unidad='{col_unidad}', Categ='{col_categoria}', SKU='{col_sku}', Marca='{col_marca}', Stock='{col_stock}', MonedaCol='{col_moneda_explicita}'")

        if not col_nombre and not col_sku and not df.columns.empty: # Si no hay nombre ni SKU, usar la primera columna si existe
            logger.warning(f"[EXCEL_PROC] No se mapeó Nombre ni SKU. Usando primera columna '{df.columns[0]}' como Nombre por defecto.")
            col_nombre = df.columns[0] # Fallback muy básico
        elif not col_nombre and not col_sku and df.columns.empty:
             logger.error(f"[EXCEL_PROC] No hay columnas para usar como Nombre o SKU. '{base_filename}'.")
             return []


        for index, row_data_series in df.iterrows():
            try:
                row = row_data_series.to_dict()
                nombre_prod_construido = ""; marca_val_str = str(row.get(col_marca, "")).strip() if col_marca else ""
                if marca_val_str: nombre_prod_construido += marca_val_str
                nombre_generico_val_str = str(row.get(col_nombre, "")).strip() if col_nombre else "" # Usar col_nombre mapeado
                if nombre_generico_val_str: nombre_prod_construido = (nombre_prod_construido + " " + nombre_generico_val_str).strip()
                
                if not nombre_prod_construido.strip(): # Si sigue vacío
                    logger.debug(f"[EXCEL_PROC] Fila {index}: Nombre construido vacío, se omite.")
                    continue

                nombre_final = limpiar_texto_base(nombre_prod_construido); sku_val_str = str(row.get(col_sku, "")).strip() if col_sku else ""; sku_final = limpiar_texto_base(sku_val_str)
                if not nombre_final and not sku_final: logger.info(f"[EXCEL_PROC] Fila {index} omitida: nombre y SKU vacíos."); continue
                if not nombre_final and sku_final: nombre_final = sku_final; logger.info(f"[EXCEL_PROC] Fila {index}: Usando SKU '{sku_final}' como nombre.")
                
                desc_val_str = str(row.get(col_descripcion, "")).strip() if col_descripcion else ""; descripcion_final = limpiar_texto_base(desc_val_str)
                if descripcion_final == nombre_final: descripcion_final = ""
                precio_crudo_val_str = str(row.get(col_precio, "")).strip() if col_precio else ""
                precio_s, precio_f, mon_p = parse_precio_flexible(precio_crudo_val_str)
                moneda_final = mon_p if mon_p else "ARS"
                if col_moneda_explicita and pd.notna(row.get(col_moneda_explicita)):
                    moneda_de_col = str(row.get(col_moneda_explicita)).strip().upper()
                    if moneda_de_col in ["USD", "ARS", "EUR"]: moneda_final = moneda_de_col
                unidad_val_str = str(row.get(col_unidad, "")).strip() if col_unidad else ""; unidad_final = limpiar_texto_base(unidad_val_str) if unidad_val_str else "unidad"
                categoria_val_str = str(row.get(col_categoria, "")).strip() if col_categoria else ""; categoria_final = limpiar_texto_base(categoria_val_str) if categoria_val_str else pyme_rubro_nombre # Default al rubro si no hay categoría
                stock_val_str = str(row.get(col_stock, "1")).strip() if col_stock else "1"; cantidad_final = limpiar_texto_base(stock_val_str) if stock_val_str else "1"

                if len(nombre_final) < 3: logger.info(f"[EXCEL_PROC] Fila {index} omitida: nombre '{nombre_final}' muy corto."); continue
                if precio_f is None and not re.search(r'(?i)consultar', precio_s or ""): logger.info(f"[EXCEL_PROC] Fila {index} omitida: precio no parseable y no es 'consultar' ('{nombre_final}', precio_crudo: '{precio_crudo_val_str}')."); continue

                producto_dict = {
                    "user_id": pyme_user_id, "nombre": nombre_final[:250], "descripcion": descripcion_final[:1000], 
                    "precio_str": precio_s if precio_s else "", "precio_float": precio_f, "moneda": moneda_final, 
                    "unidad": unidad_final[:50], "cantidad_disponible": cantidad_final[:50], 
                    "categoria": categoria_final[:100], "sku": sku_final[:100], "marca": marca_val_str[:100]
                }
                productos_extraidos.append(producto_dict)
            except Exception as e_row:
                fila_real_excel = index + (idx_fila_encabezados + 2 if idx_fila_encabezados is not None else 2)
                logger.error(f"[EXCEL_PROC] Error procesando fila Excel {fila_real_excel} de '{base_filename}': {e_row}", exc_info=True)
        
        logger.info(f"[EXCEL_PROC] Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
        if not productos_extraidos and len(df) > 0 :
             logger.warning(f"[EXCEL_PROC] Se leyeron {len(df)} filas de '{base_filename}' pero no se extrajeron productos válidos.")
        return productos_extraidos
    except FileNotFoundError: logger.error(f"❌[EXCEL_PROC] Archivo no encontrado: {path}"); return []
    except pd.errors.EmptyDataError: logger.warning(f"⚠️[EXCEL_PROC] Archivo '{os.path.basename(path)}' vacío."); return []
    except Exception as e_general:
        logger.error(f"❌[EXCEL_PROC] Error fatal procesando archivo '{os.path.basename(path)}': {e_general}", exc_info=True)
        raise ValueError(f"No se pudo procesar el archivo '{os.path.basename(path)}'. Formato o contenido inválido.")