# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
from .utils import limpiar_texto_base, parse_precio_flexible 

logger = logging.getLogger(__name__)

def procesar_catalogo_excel(path: str, pyme_user_id: int) -> list[dict]: # pyme_user_id es requerido
    """
    Procesa un archivo Excel o CSV para extraer información de productos.
    Devuelve una lista de diccionarios, donde cada diccionario representa un producto.
    """
    productos_extraidos = []
    base_filename = os.path.basename(path)
    logger.info(f"Iniciando procesamiento de archivo Excel/CSV: {base_filename} para user_id: {pyme_user_id}")

    try:
        ext = os.path.splitext(path)[1].lower()
        df_preliminar = None

        if ext == ".csv":
            try:
                # Intentar detectar separador, o permitir que pandas lo haga con sep=None
                df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='warn', encoding='utf-8', skip_blank_lines=True, low_memory=False)
            except UnicodeDecodeError:
                logger.warning(f"Error leyendo CSV '{base_filename}' con UTF-8, intentando con latin1.")
                df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='warn', encoding='latin1', skip_blank_lines=True, low_memory=False)
            except Exception as e_csv_read:
                 logger.error(f"Error general leyendo CSV '{base_filename}': {e_csv_read}", exc_info=True)
                 return [] # Devolver lista vacía si no se puede leer
        elif ext in [".xlsx", ".xls"]:
            df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0) # Probar primera hoja por defecto
        else:
            logger.error(f"Extensión no soportada '{ext}' para archivo '{base_filename}'.")
            return []

        if df_preliminar is None or df_preliminar.empty:
            logger.warning(f"⚠️ El archivo '{base_filename}' está vacío o no se pudo leer preliminarmente.")
            return []

        # Palabras clave para detectar la fila de encabezados
        keywords_header = [
            "nombre", "producto", "articulo", "item", "título", "title", "detalle", 
            "descripcion", "descripción", 
            "precio", "valor", "importe", "price", "venta", "final", "contado", "lista", "sugerido", 
            "unidad", "presentacion", "presentación", "empaque", 
            "categoria", "línea", "rubro", 
            "sku", "código", "cod", "art", "referencia", "ref",
            "marca", "brand", "fabricante",
            "stock", "cantidad", "disponible",
            "moneda", "currency"
        ]
        
        def encontrar_fila_encabezados_excel(df_check, posibles_palabras, min_coincidencias=2):
            logger.debug(f"Buscando encabezados en las primeras {len(df_check)} filas.")
            for i, row in df_check.iterrows():
                coincidencias = 0
                celdas_con_keywords = set() # Para no contar múltiples keywords en la misma celda varias veces
                for cell_idx, cell_value in enumerate(row):
                    if isinstance(cell_value, str) and cell_value.strip():
                        cell_lower = cell_value.lower()
                        for palabra_clave in posibles_palabras:
                            if palabra_clave.lower() in cell_lower:
                                celdas_con_keywords.add(cell_idx) # Contar la celda como coincidente
                                break # Pasar a la siguiente celda
                coincidencias = len(celdas_con_keywords)
                if coincidencias >= min_coincidencias:
                    logger.info(f"Fila de encabezados potencial detectada en índice Excel {i} (0-based) con {coincidencias} coincidencias. Contenido: {row.dropna().tolist()}")
                    return i
            logger.warning("No se encontró una fila de encabezados clara con las palabras clave.")
            return None

        # Tomar hasta 20 filas para buscar encabezados
        idx_fila_encabezados = encontrar_fila_encabezados_excel(df_preliminar.head(20), keywords_header)
        
        df = None
        if idx_fila_encabezados is not None:
            logger.info(f"Usando fila en índice {idx_fila_encabezados} como encabezados.")
            if ext == ".csv": 
                # Releer el CSV con la fila de encabezados correcta y manejo de errores robusto
                try:
                    df = pd.read_csv(path, sep=None, engine='python', header=0, skiprows=idx_fila_encabezados, on_bad_lines='warn', encoding='utf-8', skip_blank_lines=True, low_memory=False)
                except UnicodeDecodeError:
                    df = pd.read_csv(path, sep=None, engine='python', header=0, skiprows=idx_fila_encabezados, on_bad_lines='warn', encoding='latin1', skip_blank_lines=True, low_memory=False)
            else: 
                df = pd.read_excel(path, engine=None, header=0, skiprows=idx_fila_encabezados, sheet_name=0)
            df.dropna(how='all', inplace=True) # Eliminar filas completamente vacías
        else:
            logger.warning(f"No se encontró fila de encabezados. Intentando usar la primera fila como encabezados o columnas genéricas para '{base_filename}'.")
            # Si no se encuentran encabezados, se podría intentar usar la primera fila como header
            # o asignar nombres genéricos si la primera fila tampoco parece un header.
            # Esta parte puede necesitar más heurísticas. Por ahora, continuamos con df_preliminar
            # y asignamos columnas genéricas si la primera fila no es adecuada.
            df = df_preliminar.copy()
            # Una heurística simple: si la primera fila tiene muchos strings y no muchos números, podría ser header
            if not df.empty and all(isinstance(c, (str,int,float)) for c in df.iloc[0]): # Chequeo básico si son strings o números
                 df.columns = df.iloc[0].astype(str) # Usar primera fila como header
                 df = df[1:].reset_index(drop=True)
                 df.dropna(how='all', inplace=True)
            else: # Columnas genéricas
                df.columns = [f"columna_{i}" for i in range(len(df.columns))]


        if df is None or df.empty: 
            logger.warning(f"DataFrame vacío después de procesar encabezados para '{base_filename}'.")
            return []
            
        logger.info(f"DataFrame para '{base_filename}' cargado. {len(df)} filas. Columnas originales: {df.columns.tolist()}")
        df.columns = [limpiar_texto_base(str(col)) for col in df.columns] # Limpiar y normalizar nombres de columnas
        logger.info(f"Columnas normalizadas y limpiadas: {df.columns.tolist()}")

        # Mapeo flexible de columnas (puedes expandir estas listas)
        map_nombre = ["nombre", "producto", "articulo", "item", "título", "title", "detalle", "modelo", "descripción producto"]
        map_descripcion = ["descripcion", "descripción", "observaciones", "info adicional", "caracteristicas"]
        map_precio = ["precio", "valor", "importe", "price", "venta", "final", "contado", "lista", "sugerido", "pvp", "oferta"]
        map_unidad = ["unidad", "presentacion", "presentación", "empaque", "formato", "medida"]
        map_categoria = ["categoria", "línea", "rubro", "familia", "tipo"]
        map_sku = ["sku", "código", "cod", "articulo", "referencia", "ref", "id producto"]
        map_marca = ["marca", "brand", "fabricante"]
        map_stock = ["stock", "cantidad", "disponible", "existencias", "cant."]
        map_ean = ["ean", "upc", "codigo de barras", "código de barras"]

        def encontrar_col_flexible_excel(df_cols_list, posibles_nombres_map_list):
            for nombre_exacto in posibles_nombres_map_list:
                if nombre_exacto in df_cols_list: return nombre_exacto
            for nombre_parcial in posibles_nombres_map_list:
                for col_df_actual in df_cols_list:
                    if nombre_parcial in col_df_actual: return col_df_actual # Devolver el nombre real de la columna del DF
            return None
        
        col_nombre = encontrar_col_flexible_excel(df.columns, map_nombre)
        col_descripcion = encontrar_col_flexible_excel(df.columns, map_descripcion)
        col_precio = encontrar_col_flexible_excel(df.columns, map_precio)
        col_unidad = encontrar_col_flexible_excel(df.columns, map_unidad)
        col_categoria = encontrar_col_flexible_excel(df.columns, map_categoria)
        col_sku = encontrar_col_flexible_excel(df.columns, map_sku)
        col_marca = encontrar_col_flexible_excel(df.columns, map_marca) # Para el nombre
        col_stock = encontrar_col_flexible_excel(df.columns, map_stock)
        # col_ean = encontrar_col_flexible_excel(df.columns, map_ean) # Si necesitas EAN

        logger.info(f"Mapeo de columnas para '{base_filename}': Nombre='{col_nombre}', Desc='{col_descripcion}', Precio='{col_precio}', Unidad='{col_unidad}', Categ='{col_categoria}', SKU='{col_sku}', Marca='{col_marca}', Stock='{col_stock}'")

        for index, row in df.iterrows():
            try:
                # Construir nombre (Marca + Nombre Genérico)
                nombre_prod = ""
                if col_marca and pd.notna(row.get(col_marca)):
                    nombre_prod += str(row.get(col_marca, "")).strip()
                
                nombre_generico_val = row.get(col_nombre) if col_nombre else None
                if pd.notna(nombre_generico_val) and str(nombre_generico_val).strip():
                    nombre_prod = (nombre_prod + " " + str(nombre_generico_val).strip()).strip()
                
                if not nombre_prod and len(df.columns) > 0: # Fallback a la primera columna si no hay nombre
                    nombre_prod = str(row[df.columns[0]]).strip() if pd.notna(row[df.columns[0]]) else ""
                
                nombre_final_limpio = limpiar_texto_base(nombre_prod)

                if not nombre_final_limpio or len(nombre_final_limpio) < 2: # Nombre muy corto o vacío
                    logger.debug(f"Fila {index+2} omitida: nombre de producto inválido o muy corto ('{nombre_final_limpio}')")
                    continue

                # Descripción
                desc_val = row.get(col_descripcion) if col_descripcion else None
                descripcion_final_limpia = limpiar_texto_base(str(desc_val)) if pd.notna(desc_val) and str(desc_val).strip() else ""
                # Si la descripción es igual al nombre, dejarla vacía para no ser redundante
                if descripcion_final_limpia == nombre_final_limpio:
                    descripcion_final_limpia = ""

                # Precio
                precio_crudo_val = row.get(col_precio) if col_precio else ""
                precio_str_norm, precio_flt, moneda_norm = parse_precio_flexible(precio_crudo_val)
                
                # Considerar si un producto sin precio es válido para el catálogo.
                # Por ahora, si no hay precio_str ni precio_float, se guarda como "Consultar" implícitamente.
                # if not precio_str_norm and precio_flt is None:
                #     logger.debug(f"Fila {index+2} omitida: producto '{nombre_final_limpio}' sin precio interpretable.")
                #     continue
                
                # Unidad
                unidad_val = row.get(col_unidad) if col_unidad else ""
                unidad_final_limpia = limpiar_texto_base(str(unidad_val)) if pd.notna(unidad_val) and str(unidad_val).strip() else "unidad" # Default "unidad"

                # Otros campos
                categoria_val = row.get(col_categoria) if col_categoria else ""
                categoria_final_limpia = limpiar_texto_base(str(categoria_val)) if pd.notna(categoria_val) else ""

                sku_val = row.get(col_sku) if col_sku else ""
                sku_final_limpio = limpiar_texto_base(str(sku_val)) if pd.notna(sku_val) else ""

                stock_val = row.get(col_stock) if col_stock else "1" # Default a "1" si no hay stock
                cantidad_final_str = limpiar_texto_base(str(stock_val)) if pd.notna(stock_val) else "1"


                producto_dict = {
                    "user_id": pyme_user_id, # Ya lo recibes como argumento
                    "nombre": nombre_final_limpio[:250], # Limitar longitud
                    "descripcion": descripcion_final_limpia[:1000], # Limitar longitud
                    "precio_str": precio_str_norm if precio_str_norm else "", # String numérico limpio
                    "precio_float": precio_flt, # Float o None
                    "moneda": moneda_norm if moneda_norm else "ARS", # Moneda detectada o ARS
                    "unidad": unidad_final_limpia[:50],
                    "cantidad_disponible": cantidad_final_str[:50], # Usar el mapeo de stock
                    "categoria": categoria_final_limpia[:100],
                    "sku": sku_final_limpio[:100], 
                    # No es necesario "texto_para_embedding" aquí, se construye en upload_processor
                }
                productos_extraidos.append(producto_dict)

            except Exception as e_row:
                logger.error(f"Error procesando fila {index + (idx_fila_encabezados + 1 if idx_fila_encabezados is not None else 0) +1} de '{base_filename}': {e_row}", exc_info=True)
        
        logger.info(f"Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
        if not productos_extraidos and len(df) > 0 :
             logger.warning(f"Se leyeron {len(df)} filas de '{base_filename}' pero no se extrajeron productos válidos. Revisa el mapeo de columnas o la estructura del archivo.")
        return productos_extraidos

    except FileNotFoundError:
        logger.error(f"❌ Archivo no encontrado en la ruta: {path}")
        raise # Relanzar para que el endpoint lo maneje
    except pd.errors.EmptyDataError:
        logger.warning(f"⚠️ Archivo '{os.path.basename(path)}' parece estar vacío (Pandas EmptyDataError).")
        return [] # Devolver lista vacía
    except Exception as e_general:
        filename_for_error = os.path.basename(path)
        logger.error(f"❌ Error fatal procesando archivo Excel/CSV '{filename_for_error}': {e_general}", exc_info=True)
        # Relanzar una excepción más genérica o específica del dominio si se prefiere
        raise ValueError(f"No se pudo procesar el archivo '{filename_for_error}'. Verifique el formato y contenido.")