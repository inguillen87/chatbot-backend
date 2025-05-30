# En tu archivo: services/procesar_catalogo_excel.py

import pandas as pd
import logging
import re
import os

# --- Funciones de utilidad para precios (normalizar_precio_excel_str, extraer_precio_excel_float) ---
# (Estas son las que te pasé antes, asegúrate que estén aquí y funcionen bien)
def normalizar_precio_excel_str(precio_val) -> str:
    if pd.isna(precio_val) if 'pd' in globals() and isinstance(precio_val, float) else not precio_val:
        return ""
    precio_str = str(precio_val)
    # Corregir errores comunes de formato como "18:150.00" -> "18150.00"
    precio_str = precio_str.replace(":", "")
    precio = precio_str.replace("$", "").strip()
    if ',' in precio and '.' in precio:
        if precio.rfind(',') > precio.rfind('.'): 
            precio = precio.replace('.', '') 
            precio = precio.replace(',', '.') 
        else: 
            precio = precio.replace(',', '') 
    elif ',' in precio: 
        precio = precio.replace(',', '.')
    
    # Permitir números que podrían no tener decimales pero son precios (ej. "16390" de "16.390,00")
    # O quitar puntos si son miles y no hay coma decimal, ej. "16.390" -> "16390"
    # Esta parte es delicada y depende mucho de la consistencia del archivo de origen.
    # Si el precio es como "16.390.00" (dos puntos), la lógica anterior ya lo maneja a "16390.00"
    # Si es "16.390", podría ser 16390 o 16.390. Asumiremos que si no hay coma, los puntos son miles.
    if '.' in precio and ',' not in precio and precio.count('.') > 1: # ej 16.390.00 (mal formateado, pero común)
        precio = precio.replace('.', '', precio.count('.') -1) # Dejar el último punto si es decimal
    elif '.' in precio and ',' not in precio and len(precio.split('.')[-1]) != 2: # ej 16.390
         precio = precio.replace('.', '')


    if re.fullmatch(r"\d+(\.\d{1,2})?", precio):
        return precio
    elif re.fullmatch(r"\d+", precio): # Entero sin decimales
        return f"{precio}.00" # Añadir .00 para consistencia

    logging.warning(f"Precio no pudo normalizarse a formato D.CC: '{precio_str}' -> '{precio}'")
    return ""

def extraer_precio_excel_float(precio_normalizado_str: str) -> float | None:
    if not precio_normalizado_str: return None
    try:
        return float(precio_normalizado_str)
    except ValueError:
        return None
# --- Fin funciones de utilidad ---

def encontrar_fila_encabezados(df, posibles_palabras_encabezado):
    """Intenta encontrar la fila que contiene los encabezados."""
    for i, row in df.iterrows():
        coincidencias = 0
        for cell_value in row:
            if isinstance(cell_value, str):
                for palabra_clave in posibles_palabras_encabezado:
                    if palabra_clave.lower() in cell_value.lower():
                        coincidencias += 1
                        break # Contar solo una vez por celda
        # Si encontramos suficientes palabras clave en una fila, asumimos que es el encabezado
        if coincidencias >= 2: # Ajusta este umbral (ej. al menos 2 o 3 palabras clave)
            logging.info(f"Fila de encabezados detectada en el índice: {i}. Contenido: {row.tolist()}")
            return i
    logging.warning("No se pudo detectar una fila de encabezados clara.")
    return None # No se encontró una fila de encabezados clara

def procesar_catalogo_excel(path: str, pyme_user_id: int = 0) -> list[dict]:
    try:
        ext = os.path.splitext(path)[1].lower()
        base_filename = os.path.basename(path)
        
        df_preliminar = None
        if ext == ".csv":
            # Leer CSV sin asumir encabezados primero para encontrar la fila correcta
            df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='skip', encoding='utf-8', skip_blank_lines=True)
        elif ext in [".xlsx", ".xls"]:
            df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0) # Leer la primera hoja
        else:
            logging.error(f"Extensión no soportada '{ext}' para {base_filename}.")
            return []

        if df_preliminar is None or df_preliminar.empty:
            logging.warning(f"⚠️ El archivo {base_filename} está vacío o no se pudo leer preliminarmente.")
            return []

        # Palabras clave para detectar la fila de encabezados
        keywords_header = ["BRAND", "VARIETAL", "UNIT/CAJA", "PALLET", "CONTADO", "PRECIO LISTA", "SUGERIDO", "NOMBRE", "PRODUCTO", "ARTICULO", "PRECIO"]
        
        idx_fila_encabezados = encontrar_fila_encabezados(df_preliminar.head(10), keywords_header) # Buscar en las primeras 10 filas

        df = None
        if idx_fila_encabezados is not None:
            # Releer el archivo usando la fila detectada como encabezado
            skip_rows_count = idx_fila_encabezados
            if ext == ".csv":
                df = pd.read_csv(path, sep=None, engine='python', header=0, skiprows=skip_rows_count, on_bad_lines='skip', encoding='utf-8')
            else:
                df = pd.read_excel(path, engine=None, header=0, skiprows=skip_rows_count, sheet_name=0)
            
            # Eliminar filas completamente vacías después de cargar con encabezados
            df.dropna(how='all', inplace=True)
            if df.empty:
                 logging.warning(f"⚠️ El archivo {base_filename} resultó vacío después de procesar encabezados y eliminar vacíos.")
                 return []
        else:
            logging.warning(f"No se encontró una fila de encabezados clara para {base_filename}. Se intentará procesar con las primeras columnas como guía.")
            # Si no hay encabezados claros, podríamos intentar usar las primeras N columnas
            # o pedir al usuario que el archivo tenga un formato más estándar.
            # Por ahora, si no hay encabezados, el mapeo de columnas fallará o será incorrecto.
            # Vamos a intentar usar el df_preliminar y renombrar columnas genéricamente si no hay encabezados.
            # Esto es un fallback y probablemente necesite más lógica.
            if len(df_preliminar.columns) >= 2 : # Necesitamos al menos nombre y precio
                df = df_preliminar.copy()
                # Asignar nombres genéricos si no se detectaron encabezados
                # Esto dependerá de la estructura más común de los archivos sin encabezado.
                # Ej: df.columns = ["col_nombre", "col_varietal", "col_precio_contado", ...]
                # Esta parte es muy heurística y difícil de generalizar.
                # Por ahora, la dejaremos así y confiaremos en encontrar_nombre_columna.
                # Pero es un punto débil si el Excel no tiene NINGÚN encabezado.
            else:
                logging.error(f"El archivo {base_filename} no tiene encabezados detectables ni suficientes columnas para procesar.")
                return []


        logging.info(f"DataFrame para {base_filename} cargado. {len(df)} filas. Columnas: {df.columns.tolist()}")
        
        df.columns = [str(col).strip().lower().replace('\n', ' ').replace('  ', ' ') for col in df.columns] # Limpiar nombres de columna
        logging.info(f"Columnas normalizadas: {df.columns.tolist()}")

        productos_extraidos = []

        # Mapeo más flexible de nombres de columna
        map_brand = ["brand", "marca"]
        map_varietal = ["varietal"]
        map_nombre = ["nombre", "producto", "descripción producto", "articulo", "item", "título", "title"] # Lista más genérica
        map_descripcion = ["descripcion", "descripción", "detalle", "description"]
        map_precio_contado = ["contado"]
        map_precio_lista = ["precio lista", "precio lista 30 y 45"]
        map_precio_sugerido = ["sugerido", "precio publico", "público"]
        map_unidad_caja = ["unit/caja", "un/caja", "unidades por caja"]
        # ... (otros mapeos: categoria, sku, unidad general, moneda)

        def encontrar_col(df_cols, posibles_nombres_map):
            for nombre_map in posibles_nombres_map:
                for col in df_cols: # Iterar sobre las columnas normalizadas del DataFrame
                    if nombre_map in col: # Usar 'in' para match parcial flexible
                        return col
            return None

        col_brand_usar = encontrar_col(df.columns, map_brand)
        col_varietal_usar = encontrar_col(df.columns, map_varietal)
        col_nombre_usar = encontrar_col(df.columns, map_nombre) # Para productos no-vinos
        col_desc_usar = encontrar_col(df.columns, map_descripcion)
        
        col_precio_lista_usar = encontrar_col(df.columns, map_precio_lista)
        col_precio_contado_usar = encontrar_col(df.columns, map_precio_contado)
        col_precio_sugerido_usar = encontrar_col(df.columns, map_precio_sugerido)

        # Decidir qué columna de precio usar como principal
        col_precio_principal_usar = col_precio_lista_usar or col_precio_contado_usar or col_precio_sugerido_usar
        if not col_precio_principal_usar and "precio" in df.columns: # Fallback a una columna genérica "precio"
             col_precio_principal_usar = "precio"

        col_unidad_caja_usar = encontrar_col(df.columns, map_unidad_caja)


        for index, row in df.iterrows():
            try:
                nombre_base = ""
                if col_brand_usar and pd.notna(row.get(col_brand_usar)):
                    nombre_base += str(row.get(col_brand_usar, "")).strip()
                if col_varietal_usar and pd.notna(row.get(col_varietal_usar)):
                    nombre_base += " " + str(row.get(col_varietal_usar, "")).strip()
                
                if not nombre_base and col_nombre_usar and pd.notna(row.get(col_nombre_usar)) : # Para productos no-vinos
                    nombre_base = str(row.get(col_nombre_usar, "")).strip()
                
                nombre_final = limpiar_texto_base(nombre_base)

                if not nombre_final or len(nombre_final) < 3:
                    # logging.warning(f"Fila {index+2} de {base_filename} omitida: nombre de producto corto o vacío ('{nombre_final}'). Fila original: {row.to_dict()}")
                    continue

                desc_val = row.get(col_desc_usar) if col_desc_usar else None
                descripcion_final = limpiar_texto_base(str(desc_val)) if pd.notna(desc_val) else nombre_final

                precio_crudo = row.get(col_precio_principal_usar) if col_precio_principal_usar else ""
                precio_str_norm, precio_flt, moneda = normalizar_precio_excel_str(precio_crudo)
                
                # Si no se encontró precio en la columna principal, intentar con otras
                if not precio_flt:
                    if col_precio_contado_usar and col_precio_contado_usar != col_precio_principal_usar:
                        precio_str_norm, precio_flt, moneda = normalizar_precio_excel_str(row.get(col_precio_contado_usar))
                    if not precio_flt and col_precio_sugerido_usar and col_precio_sugerido_usar != col_precio_principal_usar:
                         precio_str_norm, precio_flt, moneda = normalizar_precio_excel_str(row.get(col_precio_sugerido_usar))

                if not precio_flt: # Si aún no hay precio válido, podría ser una fila de texto
                    logging.debug(f"Fila {index+2} de {base_filename} sin precio válido para '{nombre_final}'. Precio crudo: '{precio_crudo}'")
                    # Aquí podrías decidir si omitir la fila o guardarla sin precio.
                    # Por ahora, la guardaremos si el nombre es suficientemente descriptivo.
                    if len(nombre_final) < 10 : continue


                unidad_caja_val = row.get(col_unidad_caja_usar) if col_unidad_caja_usar else ""
                unidad = limpiar_texto_base(str(unidad_caja_val)) if pd.notna(unidad_caja_val) else "unidad" # Default
                if "6" in unidad: unidad = "Caja x6" # Heurística simple

                producto_dict = {
                    "user_id": pyme_user_id,
                    "nombre": nombre_final[:250],
                    "descripcion": descripcion_final[:500],
                    "precio_str": precio_str_norm if precio_str_norm else "",
                    "precio_float": precio_flt, # Puede ser None
                    "moneda": moneda if moneda else "ARS",
                    "unidad": unidad,
                    # Añade más campos que puedas mapear/extraer:
                    # "categoria": str(row.get(col_cat_usar, "")).strip() if col_cat_usar else "",
                    # "sku": str(row.get(col_sku_usar, "")).strip() if col_sku_usar else "",
                }
                productos_extraidos.append(producto_dict)

            except Exception as e_row:
                logging.error(f"Error procesando fila {index+2} de {base_filename}: {e_row}", exc_info=True)
                continue

        logging.info(f"Total de productos estructurados extraídos de {base_filename}: {len(productos_extraidos)}")
        if not productos_extraidos and len(df) > 0:
             logging.warning(f"Se leyeron {len(df)} filas de {base_filename} pero no se extrajeron productos válidos. Revisa el mapeo de columnas o la estructura del archivo.")
        return productos_extraidos

    except FileNotFoundError:
        logging.error(f"❌ Archivo no encontrado: {path}")
        return []
    except pd.errors.EmptyDataError:
        logging.warning(f"⚠️ Archivo {os.path.basename(path)} vacío (Pandas EmptyDataError).")
        return []
    except Exception as e:
        filename_for_error = os.path.basename(path) if 'os' in globals() and os.path else path
        logging.error(f"❌ Error fatal procesando Excel/CSV {filename_for_error}: {e}", exc_info=True)
        return []