# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re 
from typing import List, Dict, Any, Optional
from .utils import limpiar_texto_base, parse_precio_flexible

logger = logging.getLogger(__name__)

PALABRAS_CLAVE_ENCABEZADOS_PRIORITARIAS = [
    "codigo", "código", "cod.", "sku", "art.", "articulo", "ref", "id",
    "producto", "nombre", "descripción", "descripcion", "detalle", "variedad", "designacion", 
    "precio", "lista", "pvp", "valor", "importe", "$", "contado", "oferta", "sugerido", # Clave para FCA MZA
    "marca", "linea", "línea", "categoria", "categoría", "rubro", "tipo"
]
PALABRAS_CLAVE_ENCABEZADOS_SECUNDARIAS = [
    "unidad", "unidades", "unid", "u/m", "un.caja", "un/caja", "caja x", "cajas", "presentacion", "envase", # "envase" y "unidades x caja" de tu Excel
    "stock", "cantidad", "disponible", "existencias", "cant.", 
    "moneda", "currency", "divisa", "iva", "observaciones", "notas", "ean", "caja/pallet", "empaquetado", "talle",
    "cc", "ml", "lts", "kg", "grs", "color", "origen", "bodega"
]

def encontrar_fila_encabezados_excel(
    df_check: pd.DataFrame, 
    keywords_prioritarias: List[str], 
    keywords_secundarias: List[str],
    min_prioritarias_req: int = 3, # Aumentado para ser más estrictos con FCA MZA
    min_totales_req: int = 5,    # Aumentado para ser más estrictos
    max_filas_a_revisar: int = 10 # Reducido, ya que en FCA MZA los headers están cerca del inicio después de metadatos
) -> Optional[int]:
    logger.info(f"[EXCEL_PROC_HEADER] Buscando encabezados en primeras {min(len(df_check), max_filas_a_revisar)} filas (min_prioritarias={min_prioritarias_req}, min_totales={min_totales_req}).")
    
    best_header_row_index: Optional[int] = None
    highest_score: float = -1.0

    for i, row_series in df_check.head(max_filas_a_revisar).iterrows():
        # Convertir toda la fila a strings y limpiar espacios
        row_values_str = [str(cell).strip() for cell in row_series.tolist() if pd.notna(cell) and str(cell).strip()]
        
        # Una fila de encabezado real debe tener al menos tantas celdas con texto como min_totales_req
        if not row_values_str or len(row_values_str) < min_totales_req: 
            logger.debug(f"[EXCEL_PROC_HEADER] Fila {i} descartada por pocas celdas con texto ({len(row_values_str)}).")
            continue

        num_coincidencias_prioritarias = 0
        num_coincidencias_secundarias = 0
        celdas_limpias_lower = [limpiar_texto_base(cell) for cell in row_values_str]
        found_keywords_in_row = set()

        for cell_text_l in celdas_limpias_lower:
            matched_priority_for_cell = False
            for kw_p in keywords_prioritarias:
                if re.search(r'\b' + re.escape(kw_p) + r'\b', cell_text_l): 
                    found_keywords_in_row.add(kw_p)
                    num_coincidencias_prioritarias +=1 # Contar cada match prioritario
                    matched_priority_for_cell = True
                    break 
            if matched_priority_for_cell: 
                continue 
            
            for kw_s in keywords_secundarias:
                if re.search(r'\b' + re.escape(kw_s) + r'\b', cell_text_l):
                    found_keywords_in_row.add(kw_s)
                    num_coincidencias_secundarias +=1 # Contar cada match secundario
                    break
        
        total_coincidencias_en_fila = num_coincidencias_prioritarias + num_coincidencias_secundarias
        
        # Score: Ponderar prioritarias, y también la densidad de keywords en la fila
        score_actual = (num_coincidencias_prioritarias * 3.0) + (num_coincidencias_secundarias * 1.0)
        if len(row_values_str) > 0 : 
            # Penalizar si hay muchas celdas vacías o no keywords en una fila larga
            score_actual *= (total_coincidencias_en_fila / len(row_values_str)) 
        
        logger.debug(f"[EXCEL_PROC_HEADER] Fila Excel {i} (0-idx): {row_values_str} -> Prio: {num_coincidencias_prioritarias}, Sec: {num_coincidencias_secundarias}, Score: {score_actual:.2f}")

        # Condición para ser un buen candidato: suficientes keywords prioritarias y totales
        if num_coincidencias_prioritarias >= min_prioritarias_req and total_coincidencias_en_fila >= min_totales_req:
            if score_actual > highest_score:
                highest_score = score_actual
                best_header_row_index = i 
                logger.info(f"[EXCEL_PROC_HEADER] Mejor candidato a encabezado (fila índice {i}) con score {score_actual:.2f}. Contenido: {row_values_str}")
    
    if best_header_row_index is not None:
        logger.info(f"[EXCEL_PROC_HEADER] Fila encabezados final seleccionada en índice Excel {best_header_row_index} (score: {highest_score:.2f}).")
        return best_header_row_index
        
    logger.warning("[EXCEL_PROC_HEADER] No se encontró una fila de encabezados clara con las palabras clave y heurísticas.")
    return None

def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    productos_extraidos: List[Dict[str, Any]] = []
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Iniciando procesamiento de: {base_filename} para user_id: {pyme_user_id}, rubro: {pyme_rubro_nombre}")

    try:
        ext = os.path.splitext(path)[1].lower()
        df_preliminar: Optional[pd.DataFrame] = None
        
        if ext == ".csv":
            # Para CSV, intentar con varios encodings y detectar separador
            encodings_to_try = ['utf-8-sig', 'utf-8', 'latin1', 'iso-8859-1', 'cp1252']
            for enc in encodings_to_try:
                try:
                    with open(path, 'r', encoding=enc) as f_test_csv:
                        sample_lines_csv = "".join([f_test_csv.readline() for _ in range(25)]) # Leer más líneas para sniff
                    dialect_csv = pd.io.common.sniff_csv_dialect(sample_lines_csv, प्रयास_separadores=[',', ';', '\t', '|'])
                    sep_detectado_csv = dialect_csv.delimiter if dialect_csv else ','
                    
                    df_preliminar = pd.read_csv(path, sep=sep_detectado_csv, header=None, on_bad_lines='skip', encoding=enc, skip_blank_lines=False, low_memory=False, keep_default_na=False, na_filter=False, dtype=str, nrows=30)
                    df_preliminar.attrs['encoding'] = enc; logger.info(f"[EXCEL_PROC] CSV '{base_filename}' leído preliminarmente (primeras 30 filas) con encoding '{enc}' y separador '{sep_detectado_csv}'."); break 
                except UnicodeDecodeError: logger.debug(f"[EXCEL_PROC] Falló CSV '{base_filename}' con encoding '{enc}'.")
                except Exception as e_csv_prelim: logger.warning(f"[EXCEL_PROC] Error inesperado leyendo CSV '{base_filename}' con '{enc}': {e_csv_prelim}"); df_preliminar = None; break 
            if df_preliminar is None: logger.error(f"[EXCEL_PROC] No se pudo leer CSV '{base_filename}'."); return []
        elif ext in [".xlsx", ".xls"]:
            try: 
                df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str, nrows=30) 
                df_preliminar.attrs['encoding'] = 'excel'
                logger.info(f"[EXCEL_PROC] Excel '{base_filename}' leído preliminarmente (primeras 30 filas). Filas: {len(df_preliminar)}")
            except Exception as e_excel_read: logger.error(f"[EXCEL_PROC] Error leyendo archivo Excel '{base_filename}': {e_excel_read}", exc_info=True); return []
        else: logger.error(f"[EXCEL_PROC] Extensión no soportada '{ext}' para '{base_filename}'."); return []
        
        if df_preliminar is None or df_preliminar.empty: logger.warning(f"⚠️ [EXCEL_PROC] Archivo '{base_filename}' vacío o no leído preliminarmente."); return []
        
        idx_fila_encabezados = encontrar_fila_encabezados_excel(
            df_preliminar, PALABRAS_CLAVE_ENCABEZADOS_PRIORITARIAS, PALABRAS_CLAVE_ENCABEZADOS_SECUNDARIAS
        )
        
        df: Optional[pd.DataFrame] = None
        skip_rows_param = idx_fila_encabezados if idx_fila_encabezados is not None else 0 
        header_param_for_read = 0 # La primera fila después de skip_rows_param será el header

        if idx_fila_encabezados is not None:
            logger.info(f"[EXCEL_PROC] Usando fila en índice Excel {idx_fila_encabezados} como base para encabezados (skiprows={skip_rows_param}, header={header_param_for_read}).")
            try:
                current_encoding = df_preliminar.attrs.get('encoding', 'utf-8-sig')
                sep_usado = sep_detectado_csv if ext == ".csv" and 'sep_detectado_csv' in locals() else None
                engine_usado = 'python' if ext == ".csv" and sep_usado is None else None # Usar python engine si sep no se detectó

                if ext == ".csv": 
                    df = pd.read_csv(path, sep=sep_usado, engine=engine_usado, header=header_param_for_read, skiprows=skip_rows_param, on_bad_lines='skip', encoding=current_encoding, skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False, dtype=str)
                else: 
                    df = pd.read_excel(path, engine=None, header=header_param_for_read, skiprows=skip_rows_param, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str)
                
                if df is not None and not df.empty:
                    df.columns = [limpiar_texto_base(str(col)) for col in df.columns]
                    df.dropna(how='all', inplace=True) 
                    df = df.loc[:, [col for col in df.columns if col and col != 'nan' and col.strip() != '' and not col.startswith('unnamed')]]
                    if df.empty: logger.warning(f"[EXCEL_PROC] DataFrame vacío después de leer con encabezados en fila {idx_fila_encabezados} y limpiar.")
                    else: logger.info(f"[EXCEL_PROC] DataFrame principal cargado con {len(df)} filas y columnas: {df.columns.tolist()}.")
                else: logger.warning(f"[EXCEL_PROC] DataFrame None o vacío después de leer con encabezados en fila {idx_fila_encabezados}.")
            except Exception as e_read_main: 
                logger.error(f"[EXCEL_PROC] Error leyendo '{base_filename}' con encabezados en fila {idx_fila_encabezados}: {e_read_main}", exc_info=True); df = None 
        
        if df is None or df.empty: # Si la detección falló o resultó en df vacío, intentar leer con header=0
            logger.warning(f"[EXCEL_PROC] Fallback: leyendo desde primera fila (header=0).")
            try:
                current_encoding = df_preliminar.attrs.get('encoding', 'utf-8-sig')
                sep_usado = sep_detectado_csv if ext == ".csv" and 'sep_detectado_csv' in locals() else None
                engine_usado = 'python' if ext == ".csv" and sep_usado is None else None
                if ext == ".csv": df = pd.read_csv(path, sep=sep_usado, engine=engine_usado, header=0, on_bad_lines='skip', encoding=current_encoding, skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False, dtype=str)
                else: df = pd.read_excel(path, engine=None, header=0, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str)
                df.columns = [limpiar_texto_base(str(col)) for col in df.columns]
                df.dropna(how='all', inplace=True)
                df = df.loc[:, [col for col in df.columns if col and col != 'nan' and col.strip() != '' and not col.startswith('unnamed')]]
                if df.empty: logger.error(f"[EXCEL_PROC] DataFrame vacío incluso leyendo con header=0."); return []
                logger.info(f"[EXCEL_PROC] DataFrame cargado con header=0. Columnas: {df.columns.tolist()}. {len(df)} filas.")
            except Exception as e_fallback_read: logger.error(f"[EXCEL_PROC] Error fatal en lectura de fallback para '{base_filename}': {e_fallback_read}", exc_info=True); return []

        if df.empty: logger.warning(f"[EXCEL_PROC] DataFrame final vacío para '{base_filename}'."); return []
            
        df_column_list = df.columns.astype(str).tolist()

        # --- MAPEO DE COLUMNAS (Tus listas de mapeo) ---
        map_nombre = ["producto", "nombre", "articulo", "descripción producto", "designacion", "denominacion", "name", "item", "descripción", "variedad", "título"]
        map_descripcion = ["descripcion", "descripción adicional", "info adicional", "caracteristicas", "observaciones", "notas", "description", "detalle producto"]
        map_precio = [ "precio lista", "precio", "valor", "importe", "price", "venta", "final", "lista", "sugerido", "pvp", "$ botella", "$ caja", "precio unitario", "contado", "tarifa", "precio distribuidor", "precio publico", "$ sugerido contado", "$ oferta contado"]
        map_unidad = ["unidad", "presentacion", "presentación", "empaque", "formato", "u/m", "un/caja", "unidades x caja", "unidades por caja", "caja x", "pack x", "unid.", "contenido neto", "envase"]
        map_categoria = ["categoria", "categoría", "línea", "linea", "rubro", "familia", "tipo", "subrubro", "clase", "department"]
        map_sku = ["sku", "código", "codigo", "cód.", "cod", "art", "artículo nro", "referencia", "ref", "id producto", "item no", "product id", "item code", "ean"]
        map_marca = ["marca", "brand", "fabricante", "bodega", "elaborado por", "productor", "manufacturer"]
        map_stock = ["stock", "cantidad", "disponible", "disponibilidad", "existencias", "cant.", "inventory", "quantity", "qty", "available"]
        map_moneda = ["moneda", "currency", "divisa"]
        
        def encontrar_col_flexible_excel(cols_disponibles, posibles_nombres_map, field_debug_name=""):
            for nombre_map_exacto in posibles_nombres_map:
                if nombre_map_exacto in cols_disponibles: logger.info(f"[EXCEL_PROC_MAP] Columna EXACTA para '{field_debug_name}': '{nombre_map_exacto}'"); return nombre_map_exacto
            for nombre_map_parcial in posibles_nombres_map:
                for col_df_actual in cols_disponibles:
                    if nombre_map_parcial in col_df_actual: logger.info(f"[EXCEL_PROC_MAP] Columna PARCIAL para '{field_debug_name}': '{col_df_actual}' (buscando '{nombre_map_parcial}')"); return col_df_actual
            logger.info(f"[EXCEL_PROC_MAP] No se encontró columna para {field_debug_name} con términos: {posibles_nombres_map} en {cols_disponibles[:10]}...")
            return None
        
        col_nombre = encontrar_col_flexible_excel(df_column_list, map_nombre, "Nombre")
        col_descripcion = encontrar_col_flexible_excel(df_column_list, map_descripcion, "Descripción")
        col_precio_oferta = encontrar_col_flexible_excel(df_column_list, ["$ oferta contado", "oferta", "precio oferta"], "Precio Oferta")
        col_precio_sugerido = encontrar_col_flexible_excel(df_column_list, ["$ sugerido contado", "sugerido"], "Precio Sugerido")
        col_precio_lista = encontrar_col_flexible_excel(df_column_list, ["precio lista", "lista", "precio"], "Precio Lista")
        col_precio = col_precio_oferta or col_precio_sugerido or col_precio_lista 
        
        col_unidad = encontrar_col_flexible_excel(df_column_list, map_unidad, "Unidad")
        col_categoria = encontrar_col_flexible_excel(df_column_list, ["linea", "línea"] + map_categoria, "Categoría/Línea")
        col_sku = encontrar_col_flexible_excel(df_column_list, map_sku, "SKU")
        col_marca = encontrar_col_flexible_excel(df_column_list, map_marca, "Marca")
        # col_stock y col_moneda_explicita se mantienen como antes
        col_stock = encontrar_col_flexible_excel(df_column_list, map_stock, "Stock")
        col_moneda_explicita = encontrar_col_flexible_excel(df_column_list, map_moneda, "Moneda")


        logger.info(f"[EXCEL_PROC] Mapeo final: Nombre='{col_nombre}', Desc='{col_descripcion}', Precio(final)='{col_precio}', Unidad='{col_unidad}', Categ='{col_categoria}', SKU='{col_sku}', Marca='{col_marca}'")

        if not col_nombre and not col_sku: 
            if df_column_list: logger.warning(f"[EXCEL_PROC] No se mapeó Nombre ni SKU. Usando primera col '{df_column_list[0]}' como Nombre."); col_nombre = df_column_list[0] 
            else: logger.error(f"[EXCEL_PROC] No hay columnas para Nombre o SKU en '{base_filename}'."); return []
        
        for index, row_data_series in df.iterrows():
            try:
                row = row_data_series.to_dict()
                
                nombre_prod_final_parts = []
                marca_val_str = str(row.get(col_marca, "")).strip() if col_marca and col_marca in row else ""
                if marca_val_str: nombre_prod_final_parts.append(marca_val_str)
                
                nombre_col_val = str(row.get(col_nombre, "")).strip() if col_nombre and col_nombre in row else ""
                if nombre_col_val: nombre_prod_final_parts.append(nombre_col_val)

                # Para tu excel FCA MZA, el producto y variedad están en columnas separadas
                # después de "LINEA". Si "PRODUCTO" y "VARIEDAD" se mapean bien, los usamos.
                col_producto_fca = encontrar_col_flexible_excel(df_column_list, ["producto"], "Producto (específico)")
                col_variedad_fca = encontrar_col_flexible_excel(df_column_list, ["variedad"], "Variedad (específico)")

                producto_fca_val = str(row.get(col_producto_fca, "")).strip() if col_producto_fca and col_producto_fca in row else ""
                variedad_fca_val = str(row.get(col_variedad_fca, "")).strip() if col_variedad_fca and col_variedad_fca in row else ""
                
                if producto_fca_val and producto_fca_val not in nombre_prod_final_parts: nombre_prod_final_parts.append(producto_fca_val)
                if variedad_fca_val and variedad_fca_val not in nombre_prod_final_parts: nombre_prod_final_parts.append(variedad_fca_val)
                
                nombre_prod_construido = " ".join(filter(None, nombre_prod_final_parts)).strip()
                                
                if not nombre_prod_construido: continue

                nombre_final = limpiar_texto_base(nombre_prod_construido); sku_val_str = str(row.get(col_sku, "")).strip() if col_sku and col_sku in row else ""; sku_final = limpiar_texto_base(sku_val_str)
                if not nombre_final and not sku_final: continue
                if not nombre_final and sku_final: nombre_final = sku_final
                
                desc_val_str = str(row.get(col_descripcion, "")).strip() if col_descripcion and col_descripcion in row else ""
                descripcion_final = limpiar_texto_base(desc_val_str)
                if descripcion_final == nombre_final: descripcion_final = ""
                
                precio_crudo_val_str = str(row.get(col_precio, "")).strip() if col_precio and col_precio in row else ""
                precio_s, precio_f, mon_p = parse_precio_flexible(precio_crudo_val_str)
                
                moneda_final = "USD" if "fca mza" in base_filename.lower() else (mon_p if mon_p else "ARS") # Default USD para FCA
                if col_moneda_explicita and col_moneda_explicita in row and pd.notna(row.get(col_moneda_explicita)):
                    moneda_de_col = str(row.get(col_moneda_explicita)).strip().upper()
                    if moneda_de_col in ["USD", "ARS", "EUR"]: moneda_final = moneda_de_col
                
                unidad_val_str = str(row.get(col_unidad, "")).strip() if col_unidad and col_unidad in row else ""
                # Para FCA MZA, "UNIDADES X CAJA" puede ser relevante para unidad
                col_unidades_caja_fca = encontrar_col_flexible_excel(df_column_list, ["unidades x caja"], "Unidades Por Caja (FCA)")
                unidades_caja_fca_val = str(row.get(col_unidades_caja_fca, "")).strip() if col_unidades_caja_fca and col_unidades_caja_fca in row else ""
                if unidades_caja_fca_val: unidad_val_str = f"Caja x{unidades_caja_fca_val}" if not unidad_val_str else f"{unidad_val_str} (Caja x{unidades_caja_fca_val})"
                unidad_final = limpiar_texto_base(unidad_val_str) if unidad_val_str else "unidad"
                
                categoria_val_str = str(row.get(col_categoria, "")).strip() if col_categoria and col_categoria in row else ""
                # Para FCA MZA, la categoría es la "LINEA"
                if "fca mza" in base_filename.lower() and not categoria_val_str:
                     col_linea_fca = encontrar_col_flexible_excel(df_column_list, ["linea", "línea"], "Línea (para categ FCA)")
                     if col_linea_fca and col_linea_fca in row: categoria_val_str = str(row.get(col_linea_fca, "")).strip()
                categoria_final = limpiar_texto_base(categoria_val_str) if categoria_val_str else pyme_rubro_nombre 
                
                stock_val_str = str(row.get(col_stock, "1")).strip() if col_stock and col_stock in row else "1"; cantidad_final = limpiar_texto_base(stock_val_str) if stock_val_str else "1"

                if len(nombre_final) < 3: continue
                if precio_f is None and not re.search(r'(?i)consultar|s/p', precio_s or ""): continue

                producto_dict = {
                    "user_id": pyme_user_id, "nombre": nombre_final[:250], "descripcion": descripcion_final[:1000], 
                    "precio_str": precio_s if precio_s else "Consultar", "precio_float": precio_f, "moneda": moneda_final, 
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
        return []# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re
from typing import List, Dict, Any, Optional, Tuple

# Asumimos que utils.py existe en el mismo nivel de carpeta (services/)
from .utils import limpiar_texto_base, parse_precio_flexible

logger = logging.getLogger(__name__)

# --- 1. DICCIONARIO CENTRAL DE PALABRAS CLAVE ---
# Unificamos todas las palabras clave en un solo lugar. Es nuestra fuente de verdad.
KEYWORD_MAP = {
    'sku': ["codigo", "código", "cod.", "sku", "art.", "articulo", "ref", "id", "item code", "ean"],
    'nombre': ["producto", "nombre", "descripción", "descripcion", "detalle", "variedad", "designacion", "item", "title"],
    'precio': ["precio", "lista", "pvp", "valor", "importe", "$", "contado", "oferta", "sugerido", "minorista", "publico", "tarifa"],
    'marca': ["marca", "linea", "línea", "brand", "fabricante", "bodega"],
    'categoria': ["categoria", "categoría", "rubro", "tipo", "familia", "clase"],
    'unidad': ["unidad", "unidades", "unid", "u/m", "presentacion", "envase", "caja x", "pack x", "contenido"],
    'stock': ["stock", "cantidad", "disponible", "existencias", "cant.", "quantity", "qty"],
}

# --- 2. EL NUEVO CEREBRO: MAPEADOR DE COLUMNAS INTELIGENTE ---
def crear_mapa_de_columnas_inteligente(
    df: pd.DataFrame, 
    max_filas_a_revisar: int = 15
) -> Optional[Tuple[Dict[str, str], int]]:
    """
    Analiza las primeras N filas de un DataFrame para encontrar la fila de encabezado
    y crear un mapa de columnas {'campo_estandar': 'nombre_columna_original'}.

    Devuelve: Una tupla (mapa_de_columnas, indice_fila_datos_inicio) o None si no encuentra un mapa válido.
    """
    logger.info(f"[MAPPER] Iniciando búsqueda inteligente de mapa de columnas en {max_filas_a_revisar} filas.")
    
    mejor_mapa = {}
    mejor_fila_idx = -1
    max_campos_encontrados = 0

    # Itera por las primeras filas del DataFrame para encontrar el mejor encabezado
    for i, row in df.head(max_filas_a_revisar).iterrows():
        mapa_actual = {}
        # Convierte la fila a una lista de strings limpios
        celdas_fila = [limpiar_texto_base(str(cell)) for cell in row.tolist() if pd.notna(cell) and str(cell).strip()]
        
        # Para cada campo estándar que queremos encontrar (nombre, precio, etc.)
        for campo_estandar, keywords in KEYWORD_MAP.items():
            # Si ya encontramos un mapeo para este campo, no lo buscamos de nuevo
            if campo_estandar in mapa_actual:
                continue
            
            # Busca en las celdas de la fila actual una coincidencia con las palabras clave
            for celda in celdas_fila:
                for keyword in keywords:
                    if re.search(r'\b' + re.escape(keyword) + r'\b', celda, re.IGNORECASE):
                        # Encontramos una! Mapeamos el campo estándar al nombre de la celda original
                        mapa_actual[campo_estandar] = celda
                        break # Pasamos a la siguiente celda
                if campo_estandar in mapa_actual:
                    break # Pasamos al siguiente campo estándar

        # Si el mapa actual es mejor que el que teníamos, lo guardamos
        if len(mapa_actual) > max_campos_encontrados:
            max_campos_encontrados = len(mapa_actual)
            mejor_mapa = mapa_actual
            mejor_fila_idx = i
            logger.info(f"[MAPPER] Nuevo mejor candidato a encabezado en fila {i} con {len(mapa_actual)} campos encontrados. Mapa: {mapa_actual}")

    # Después de revisar todas las filas, validamos si el mejor mapa encontrado es suficientemente bueno
    if 'nombre' in mejor_mapa and 'precio' in mejor_mapa:
        # El índice de la fila donde empiezan los datos es la siguiente al encabezado
        fila_inicio_datos = mejor_fila_idx + 1
        logger.info(f"✅ [MAPPER] Mapa de columnas final aceptado. Encabezados en fila {mejor_fila_idx}. Datos comienzan en {fila_inicio_datos}.")
        return mejor_mapa, fila_inicio_datos
    else:
        logger.error("[MAPPER] No se pudo crear un mapa de columnas válido. Faltan los campos esenciales 'nombre' y/o 'precio'.")
        return None

# --- 3. FUNCIÓN PRINCIPAL REFACTORIZADA ---
def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """
    Procesa un archivo Excel o CSV, detecta inteligentemente las columnas y extrae los productos.
    """
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Iniciando NUEVO procesamiento de: {base_filename} para user_id: {pyme_user_id}")

    try:
        # Lee el archivo sin encabezados para analizarlo en bruto
        df_bruto = pd.read_excel(path, header=None, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str)
    except Exception as e_read:
        logger.error(f"❌ Error fatal al leer el archivo Excel/CSV '{base_filename}': {e_read}", exc_info=True)
        return []

    if df_bruto.empty:
        logger.warning(f"⚠️ El archivo '{base_filename}' está vacío o no se pudo leer.")
        return []

    # Llama al nuevo cerebro para obtener el mapa y la fila de inicio
    resultado_mapeo = crear_mapa_de_columnas_inteligente(df_bruto)
    
    if not resultado_mapeo:
        # Si el cerebro no pudo entender el archivo, lo notificamos y salimos.
        raise ValueError("No se pudieron identificar las columnas de 'Nombre' y 'Precio' en el archivo. Por favor, asegúrate de que el archivo tenga encabezados claros.")

    mapa_columnas, fila_inicio_datos = resultado_mapeo
    
    # Renombramos las columnas del DataFrame según nuestro mapa
    # Primero, invertimos el mapa para tener {'nombre_col_original': 'campo_estandar'}
    mapa_inverso = {v: k for k, v in mapa_columnas.items()}
    
    # Usamos la fila de encabezados para renombrar las columnas del DataFrame
    df_datos = df_bruto.copy()
    df_datos.columns = df_datos.iloc[fila_inicio_datos - 1].apply(limpiar_texto_base)
    # Ahora renombramos usando el mapa inverso
    df_datos = df_datos.rename(columns=mapa_inverso)
    # Quitamos las filas que estaban antes de los datos
    df_datos = df_datos.iloc[fila_inicio_datos:].reset_index(drop=True)
    df_datos.dropna(how='all', inplace=True)

    logger.info(f"DataFrame procesado con {len(df_datos)} filas. Columnas mapeadas: {df_datos.columns.tolist()}")

    productos_extraidos: List[Dict[str, Any]] = []
    # Itera por las filas del DataFrame ya limpio y mapeado
    for index, row in df_datos.iterrows():
        try:
            # Extraer datos es ahora mucho más simple y directo
            nombre_prod = str(row.get('nombre', '')).strip()
            precio_crudo = str(row.get('precio', '')).strip()

            # Si no hay nombre o precio en una fila, se salta.
            if not nombre_prod or not precio_crudo:
                continue

            precio_str, precio_float, moneda = parse_precio_flexible(precio_crudo)

            # Si el precio no se puede parsear y no es un texto como "consultar", se salta.
            if precio_float is None and not re.search(r'consultar|s/p', precio_str or "", re.IGNORECASE):
                continue
                
            producto = {
                "nombre": nombre_prod[:250],
                "precio_str": precio_str if precio_str else "Consultar",
                "precio_float": precio_float,
                "moneda": moneda if moneda else "ARS",
                "sku": str(row.get('sku', ''))[:100],
                "descripcion": str(row.get('descripcion', ''))[:1000],
                "marca": str(row.get('marca', ''))[:100],
                "categoria_qdrant": str(row.get('categoria', pyme_rubro_nombre))[:100],
                "unidad": str(row.get('unidad', 'unidad'))[:50],
                "cantidad_disponible": str(row.get('stock', '1'))[:50],
            }
            productos_extraidos.append(producto)

        except Exception as e_row:
            logger.warning(f"⚠️ Error procesando la fila {index + fila_inicio_datos} del archivo. Saltando. Error: {e_row}")
            continue

    logger.info(f"✅ Proceso completado. Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
    return productos_extraidos