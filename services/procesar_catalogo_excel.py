# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re 
from .utils import limpiar_texto_base, parse_precio_flexible

logger = logging.getLogger(__name__)

def procesar_catalogo_excel(path: str, pyme_user_id: int) -> list[dict]:
    productos_extraidos = []
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Iniciando procesamiento de: {base_filename} para user_id: {pyme_user_id}")

    try:
        ext = os.path.splitext(path)[1].lower()
        df_preliminar = None
        encodings_to_try = ['utf-8', 'latin1', 'iso-8859-1', 'cp1252']

        if ext == ".csv":
            for enc in encodings_to_try:
                try:
                    # Leer sin asumir encabezados para la detección inicial
                    df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='warn', encoding=enc, skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False)
                    # keep_default_na=False y na_filter=False para que no interprete "NA" o "NaN" como NaN si son parte de un nombre.
                    logger.info(f"[EXCEL_PROC] CSV '{base_filename}' leído preliminarmente con encoding '{enc}'.")
                    df_preliminar.attrs['encoding'] = enc # Guardar el encoding exitoso
                    break 
                except UnicodeDecodeError:
                    logger.warning(f"[EXCEL_PROC] Error leyendo CSV '{base_filename}' con encoding '{enc}'. Intentando siguiente...")
                except Exception as e_csv_prelim:
                    logger.warning(f"[EXCEL_PROC] Error inesperado leyendo CSV '{base_filename}' preliminarmente con encoding '{enc}': {e_csv_prelim}")
                    df_preliminar = None
                    break 
            if df_preliminar is None:
                 logger.error(f"[EXCEL_PROC] No se pudo leer el CSV '{base_filename}' con ninguna codificación probada.")
                 return []
        elif ext in [".xlsx", ".xls"]:
            try:
                # Leer sin asumir encabezados para la detección inicial
                df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0, keep_default_na=False, na_filter=False)
                df_preliminar.attrs['encoding'] = 'excel' # Marcar como excel
            except Exception as e_excel_read:
                logger.error(f"[EXCEL_PROC] Error leyendo archivo Excel '{base_filename}': {e_excel_read}", exc_info=True)
                return []
        else:
            logger.error(f"[EXCEL_PROC] Extensión no soportada '{ext}' para archivo '{base_filename}'.")
            return []

        if df_preliminar is None or df_preliminar.empty:
            logger.warning(f"⚠️ [EXCEL_PROC] Archivo '{base_filename}' está vacío o no se pudo leer preliminarmente.")
            return []

        # Palabras clave expandidas para detectar la fila de encabezados
        keywords_header = [
            "nombre", "producto", "articulo", "item", "título", "title", "detalle", "descripción producto", "designación", "denominacion",
            "descripcion", "descripción", "observaciones", "info adicional", "caracteristicas",
            "precio", "valor", "importe", "price", "venta", "final", "contado", "lista", "sugerido", "pvp", "oferta", "costo", "tarifa",
            "unidad", "presentacion", "presentación", "empaque", "formato", "medida", "u/m", "unidades x bulto", "fraccion",
            "categoria", "línea", "rubro", "familia", "tipo", "subrubro", "clase",
            "sku", "código", "cod", "art", "referencia", "ref", "id producto", "item no", "codigo ean", "codigo interno",
            "marca", "brand", "fabricante", "bodega", "elaborado por",
            "stock", "cantidad", "disponible", "existencias", "cant.", "disponibilidad",
            "moneda", "currency", "divisa",
        ]
        
        def encontrar_fila_encabezados_excel(df_check, posibles_palabras, min_coincidencias=2):
            logger.info(f"[EXCEL_PROC] Buscando encabezados en las primeras {min(len(df_check), 20)} filas (min_coincidencias={min_coincidencias}).")
            best_i = None
            max_coincidencias_found = 0
            for i, row in df_check.head(20).iterrows(): # Limitar a 20 filas para búsqueda de header
                coincidencias = 0; celdas_con_keywords = set()
                num_celdas_con_texto = 0
                for cell_idx, cell_value in enumerate(row):
                    if isinstance(cell_value, str) and cell_value.strip():
                        num_celdas_con_texto += 1
                        cell_lower = cell_value.lower()
                        for palabra_clave in posibles_palabras:
                            # Ser más específico para evitar falsos positivos (ej. "no" en "nombre")
                            if re.search(r'\b' + re.escape(palabra_clave.lower()) + r'\b', cell_lower):
                                celdas_con_keywords.add(cell_idx); break
                coincidencias = len(celdas_con_keywords)
                
                # Heurística: una buena fila de encabezado tiene varias celdas con texto
                # y un buen número de coincidencias de keywords.
                if coincidencias >= min_coincidencias and num_celdas_con_texto > coincidencias:
                    if coincidencias > max_coincidencias_found:
                        max_coincidencias_found = coincidencias
                        best_i = i
                        logger.info(f"[EXCEL_PROC] Mejor candidato a encabezado (hasta ahora) en fila {i} con {coincidencias} keywords. Contenido: {row.dropna().tolist()}")
            
            if best_i is not None:
                logger.info(f"[EXCEL_PROC] Fila de encabezados final seleccionada en índice Excel {best_i} con {max_coincidencias_found} coincidencias.")
                return best_i
                
            logger.warning("[EXCEL_PROC] No se encontró una fila de encabezados clara con las palabras clave y heurísticas.")
            return None

        idx_fila_encabezados = encontrar_fila_encabezados_excel(df_preliminar, keywords_header, min_coincidencias=2)
        
        df = None
        if idx_fila_encabezados is not None:
            logger.info(f"[EXCEL_PROC] Usando fila en índice {idx_fila_encabezados} como encabezados.")
            header_row_index_for_read = idx_fila_encabezados # El índice de la fila a saltar para que la siguiente sea el header
            try:
                current_encoding = df_preliminar.attrs.get('encoding', 'utf-8')
                if ext == ".csv": 
                    df = pd.read_csv(path, sep=None, engine='python', header=0, skiprows=header_row_index_for_read, on_bad_lines='warn', encoding=current_encoding, skip_blank_lines=True, low_memory=False, keep_default_na=False, na_filter=False)
                else: # xlsx, xls
                    df = pd.read_excel(path, engine=None, header=0, skiprows=header_row_index_for_read, sheet_name=0, keep_default_na=False, na_filter=False)
                df.dropna(how='all', inplace=True) # Eliminar filas completamente vacías
                if df.empty:
                    logger.warning(f"[EXCEL_PROC] DataFrame vacío después de leer con encabezados detectados.")
                else:
                    logger.info(f"[EXCEL_PROC] DataFrame principal cargado con {len(df)} filas y {len(df.columns)} columnas.")
            except Exception as e_read_main:
                logger.error(f"[EXCEL_PROC] Error leyendo el archivo '{base_filename}' después de detectar encabezados: {e_read_main}", exc_info=True)
                return []
        else: 
            logger.warning(f"[EXCEL_PROC] No se pudo determinar una fila de encabezados. Intentando usar la primera fila de df_preliminar si tiene texto.")
            # Si no se encuentran encabezados, usar la primera fila de df_preliminar como header
            # si tiene al menos algunas celdas con texto.
            if not df_preliminar.empty:
                primera_fila_valores = [str(x).strip() for x in df_preliminar.iloc[0].tolist() if isinstance(x, str) and str(x).strip()]
                if len(primera_fila_valores) >= 2: # Necesita al menos 2 headers con texto
                    df = df_preliminar.copy()
                    df.columns = [limpiar_texto_base(str(col)) for col in df.iloc[0]] 
                    df = df[1:].reset_index(drop=True)
                    df.dropna(how='all', inplace=True)
                    logger.info(f"[EXCEL_PROC] Usando primera fila como encabezados. Columnas: {df.columns.tolist()}")
                else:
                    logger.error(f"[EXCEL_PROC] No se pudo usar la primera fila como encabezados válidos para '{base_filename}'.")
                    return []
            else:
                logger.error(f"[EXCEL_PROC] df_preliminar estaba vacío, no se pueden procesar encabezados para '{base_filename}'.")
                return []

        if df is None or df.empty: 
            logger.warning(f"[EXCEL_PROC] DataFrame vacío después de procesar encabezados para '{base_filename}'. No se pueden extraer productos.")
            return []
            
        original_columns = df.columns.tolist() 
        df.columns = [limpiar_texto_base(str(col)) for col in df.columns if str(col).strip()] # Limpiar y quitar columnas vacías
        # Filtrar columnas que quedaron como 'nan' o vacías después de limpiar
        df = df.loc[:, [col for col in df.columns if col and col != 'nan']]
        if df.empty:
            logger.warning(f"[EXCEL_PROC] DataFrame quedó sin columnas válidas después de limpiar nombres de encabezado para '{base_filename}'.")
            return []

        logger.info(f"[EXCEL_PROC] Columnas originales: {original_columns} -> Columnas finales para mapeo: {df.columns.tolist()}")

        # MAPEO DE COLUMNAS (Misma lista expandida que antes)
        map_nombre = ["nombre", "producto", "nombreproducto", "articulo", "artículo", "item", "título", "title", "detalle", "descripción producto", "designacion", "denominacion", "name", "product name", "descripción"] # 'descripción' también aquí
        map_descripcion = ["descripcion", "descripción adicional", "detalle producto", "info adicional", "caracteristicas", "observaciones", "notas", "description", "product description"]
        map_precio = [ "precio", "valor", "importe", "price", "precio venta", "precio final", "contado", "efectivo", "precio lista", "lista", "sugerido", "pvp", "p.v.p.", "p.v.p", "oferta", "precio oferta", "costo", "tarifa", "$ box", "$ bottle", "precio distribuidor", "precio sugerido publico", "unit price", "sale price" ]
        map_unidad = ["unidad", "presentacion", "presentación", "empaque", "formato", "medida", "u/m", "unidades x bulto", "fraccion", "unit/box", "unit/caja", "package", "size"]
        map_categoria = ["categoria", "categoría", "línea", "linea", "rubro", "familia", "tipo", "subrubro", "clase", "department", "category"]
        map_sku = ["sku", "código", "codigo", "cód.", "cod", "art", "articulo nro", "referencia", "ref", "id producto", "item no", "product id", "item code"]
        map_marca = ["marca", "brand", "fabricante", "bodega", "elaborado por", "productor", "manufacturer"]
        map_stock = ["stock", "cantidad", "disponible", "disponibilidad", "existencias", "cant.", "inventory", "quantity", "qty", "available"]
        map_moneda = ["moneda", "currency", "divisa"]

        def encontrar_col_flexible_excel(df_cols_list, posibles_nombres_map_list):
            # Intenta match exacto primero (case-insensitive ya hecho por limpiar_texto_base)
            for nombre_exacto_map in posibles_nombres_map_list:
                if nombre_exacto_map in df_cols_list: 
                    logger.info(f"[EXCEL_PROC_MAP] Columna encontrada por match exacto: '{nombre_exacto_map}' para {posibles_nombres_map_list[0]}")
                    return nombre_exacto_map
            # Luego intenta match parcial (substring)
            for nombre_parcial_map in posibles_nombres_map_list:
                for col_df_actual in df_cols_list:
                    if nombre_parcial_map in col_df_actual: # col_df_actual ya está en minúsculas y limpio
                        logger.info(f"[EXCEL_PROC_MAP] Columna encontrada por match parcial: '{col_df_actual}' (buscando '{nombre_parcial_map}') para {posibles_nombres_map_list[0]}")
                        return col_df_actual
            logger.info(f"[EXCEL_PROC_MAP] No se encontró columna para {posibles_nombres_map_list[0]} con los términos: {posibles_nombres_map_list}")
            return None
        
        col_nombre = encontrar_col_flexible_excel(df.columns, map_nombre)
        col_descripcion = encontrar_col_flexible_excel(df.columns, map_descripcion)
        col_precio = encontrar_col_flexible_excel(df.columns, map_precio)
        col_unidad = encontrar_col_flexible_excel(df.columns, map_unidad)
        col_categoria = encontrar_col_flexible_excel(df.columns, map_categoria)
        col_sku = encontrar_col_flexible_excel(df.columns, map_sku)
        col_marca = encontrar_col_flexible_excel(df.columns, map_marca)
        col_stock = encontrar_col_flexible_excel(df.columns, map_stock)
        col_moneda_explicita = encontrar_col_flexible_excel(df.columns, map_moneda)

        logger.info(f"[EXCEL_PROC] Mapeo final de columnas para '{base_filename}':\n  Nombre='{col_nombre}'\n  Desc='{col_descripcion}'\n  Precio='{col_precio}'\n  Unidad='{col_unidad}'\n  Categ='{col_categoria}'\n  SKU='{col_sku}'\n  Marca='{col_marca}'\n  Stock='{col_stock}'\n  MonedaCol='{col_moneda_explicita}'")

        if not col_nombre and not col_sku: # Si no encontramos ni nombre ni SKU, es muy difícil procesar
            logger.error(f"[EXCEL_PROC] No se pudieron mapear columnas esenciales (Nombre o SKU) para '{base_filename}'. No se pueden extraer productos.")
            return []

        for index, row_data_series in df.iterrows(): # row_data_series es una Serie de Pandas
            try:
                # Convertir la Serie de la fila a un diccionario para facilitar el acceso .get()
                row = row_data_series.to_dict()

                nombre_prod_construido = ""
                marca_val_str = str(row.get(col_marca, "")).strip() if col_marca else ""
                if marca_val_str: nombre_prod_construido += marca_val_str
                
                nombre_generico_val_str = str(row.get(col_nombre, "")).strip() if col_nombre else ""
                if nombre_generico_val_str: nombre_prod_construido = (nombre_prod_construido + " " + nombre_generico_val_str).strip()
                
                # CORRECCIÓN AL FALLBACK DEL NOMBRE:
                # Si no se encontró col_nombre o col_sku, pero hay columnas, intentar usar la primera.
                # El error "ValueError: The truth value of a Series is ambiguous" ocurre si row[col] es una Serie y no un valor escalar.
                # Esto no debería pasar si el df se leyó bien con encabezados. El problema anterior era que df.columns eran ['nan', ...]
                if not nombre_prod_construido and df.columns.any(): # df.columns.any() verifica que haya al menos una columna
                    first_col_name = df.columns[0]
                    # Acceder al valor de la primera columna para la fila actual
                    # pd.notna y .item() son para manejar celdas que podrían no ser escalares simples
                    # aunque si el DF se cargó bien con header=0, row[col_name] debería ser escalar.
                    celda_primera_col = row.get(first_col_name) # Usar .get() en el dict de la fila
                    if pd.notna(celda_primera_col):
                        nombre_prod_construido = str(celda_primera_col).strip()
                    else:
                        nombre_prod_construido = ""
                    logger.info(f"[EXCEL_PROC] Fila {index}: Usando fallback para nombre (primera columna '{first_col_name}'): '{nombre_prod_construido}'")

                
                nombre_final = limpiar_texto_base(nombre_prod_construido)
                sku_val_str = str(row.get(col_sku, "")).strip() if col_sku else ""
                sku_final = limpiar_texto_base(sku_val_str)

                # Condición mínima para seguir: tener un nombre o un SKU
                if not nombre_final and not sku_final:
                    logger.info(f"[EXCEL_PROC] Fila {index} omitida: nombre y SKU vacíos ('{nombre_final}', '{sku_final}')")
                    continue
                if not nombre_final and sku_final: # Si no hay nombre pero sí SKU, usar SKU como nombre
                    nombre_final = sku_final
                    logger.info(f"[EXCEL_PROC] Fila {index}: Usando SKU '{sku_final}' como nombre del producto.")


                desc_val_str = str(row.get(col_descripcion, "")).strip() if col_descripcion else ""
                descripcion_final = limpiar_texto_base(desc_val_str)
                if descripcion_final == nombre_final: descripcion_final = ""

                precio_crudo_val_str = str(row.get(col_precio, "")).strip() if col_precio else ""
                precio_str_norm, precio_flt, moneda_detectada_en_precio = parse_precio_flexible(precio_crudo_val_str)
                
                moneda_final = moneda_detectada_en_precio if moneda_detectada_en_precio else "ARS"
                if col_moneda_explicita and pd.notna(row.get(col_moneda_explicita)):
                    moneda_de_col = str(row.get(col_moneda_explicita)).strip().upper()
                    if moneda_de_col in ["USD", "ARS", "EUR", "GBP", "CLP"]: moneda_final = moneda_de_col
                
                unidad_val_str = str(row.get(col_unidad, "")).strip() if col_unidad else ""
                unidad_final = limpiar_texto_base(unidad_val_str) if unidad_val_str else "unidad"

                categoria_val_str = str(row.get(col_categoria, "")).strip() if col_categoria else ""
                categoria_final = limpiar_texto_base(categoria_val_str)

                stock_val_str = str(row.get(col_stock, "1")).strip() if col_stock else "1"
                cantidad_final = limpiar_texto_base(stock_val_str) if stock_val_str else "1"

                producto_dict = {
                    "user_id": pyme_user_id, "nombre": nombre_final[:250],
                    "descripcion": descripcion_final[:1000], "precio_str": precio_str_norm if precio_str_norm else "",
                    "precio_float": precio_flt, "moneda": moneda_final, "unidad": unidad_final[:50],
                    "cantidad_disponible": cantidad_final[:50], "categoria": categoria_final[:100],
                    "sku": sku_final[:100], "marca": marca_val_str[:100] if marca_val_str else ""
                }
                productos_extraidos.append(producto_dict)
                # logger.debug(f"[EXCEL_PROC] Fila {index} procesada: {producto_dict}")

            except Exception as e_row:
                fila_real_excel = index + (idx_fila_encabezados + 2 if idx_fila_encabezados is not None else 2)
                logger.error(f"[EXCEL_PROC] Error procesando fila Excel {fila_real_excel} de '{base_filename}': {e_row}", exc_info=True)
        
        logger.info(f"[EXCEL_PROC] Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
        if not productos_extraidos and len(df) > 0 :
             logger.warning(f"[EXCEL_PROC] Se leyeron {len(df)} filas de '{base_filename}' pero no se extrajeron productos válidos. Verificar mapeo o contenido del archivo.")
        return productos_extraidos

    except FileNotFoundError: logger.error(f"❌[EXCEL_PROC] Archivo no encontrado: {path}"); return []
    except pd.errors.EmptyDataError: logger.warning(f"⚠️[EXCEL_PROC] Archivo '{os.path.basename(path)}' vacío."); return []
    except Exception as e_general:
        logger.error(f"❌[EXCEL_PROC] Error fatal procesando archivo '{os.path.basename(path)}': {e_general}", exc_info=True)
        raise ValueError(f"No se pudo procesar el archivo '{os.path.basename(path)}'. Formato o contenido inválido.")