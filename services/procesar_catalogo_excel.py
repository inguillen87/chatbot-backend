
import pandas as pd
import logging
import re 
import os
from .utils import limpiar_texto_base, parse_precio_flexible # <--- IMPORTAR DE UTILS

def procesar_catalogo_excel(path: str, pyme_user_id: int = 0) -> list[dict]:
    try:
        ext = os.path.splitext(path)[1].lower()
        base_filename = os.path.basename(path)
        
        df_preliminar = None
        if ext == ".csv":
            # ... (tu lógica de lectura de CSV, considera añadir encoding='latin1' como fallback)
            try:
                df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='skip', encoding='utf-8', skip_blank_lines=True)
            except UnicodeDecodeError:
                logging.warning(f"Error leyendo CSV {base_filename} con UTF-8, intentando con latin1.")
                df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='skip', encoding='latin1', skip_blank_lines=True)
            # ... (más fallbacks si es necesario) ...
        elif ext in [".xlsx", ".xls"]:
            df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0)
        else:
            logging.error(f"Extensión no soportada '{ext}' para {base_filename}.")
            return []

        if df_preliminar is None or df_preliminar.empty:
            logging.warning(f"⚠️ El archivo {base_filename} está vacío o no se pudo leer preliminarmente.")
            return []

        keywords_header = ["BRAND", "VARIETAL", "UNIT/CAJA", "PALLET", "CONTADO", "PRECIO LISTA", "SUGERIDO", "NOMBRE", "PRODUCTO", "ARTICULO", "PRECIO", "DESCRIPCION"]
        
        # --- Función encontrar_fila_encabezados (definida aquí o en utils.py) ---
        def encontrar_fila_encabezados(df_check, posibles_palabras):
            for i, row in df_check.iterrows():
                coincidencias = 0
                for cell_value in row:
                    if isinstance(cell_value, str):
                        for palabra_clave in posibles_palabras:
                            if palabra_clave.lower() in cell_value.lower():
                                coincidencias += 1; break
                if coincidencias >= 2:
                    logging.info(f"Fila de encabezados detectada en índice Excel: {i}. Contenido: {row.tolist()}")
                    return i
            return None
        # --- Fin encontrar_fila_encabezados ---

        idx_fila_encabezados = encontrar_fila_encabezados(df_preliminar.head(10), keywords_header)
        df = None
        if idx_fila_encabezados is not None:
            if ext == ".csv": df = pd.read_csv(path, sep=None, engine='python', header=0, skiprows=idx_fila_encabezados, on_bad_lines='skip', encoding='utf-8') # Releer con encabezado
            else: df = pd.read_excel(path, engine=None, header=0, skiprows=idx_fila_encabezados, sheet_name=0)
            df.dropna(how='all', inplace=True)
        else:
            logging.warning(f"No se encontró fila de encabezados clara en {base_filename}. Usando columnas genéricas.")
            df = df_preliminar.copy()
            df.columns = [f"col_{i}" for i in range(len(df.columns))]

        if df.empty: return []
            
        logging.info(f"DataFrame para {base_filename} cargado. {len(df)} filas. Columnas: {df.columns.tolist()}")
        df.columns = [str(col).strip().lower().replace('\n', ' ').replace('  ', ' ') for col in df.columns]
        logging.info(f"Columnas normalizadas: {df.columns.tolist()}")

        productos_extraidos = []
        # ... (tu lógica de mapeo de columnas: map_nombre, map_precio, etc. y encontrar_col_flexible) ...
        # (Asegúrate que encontrar_col_flexible esté definida aquí o importada de utils.py si la creas ahí)
        # --- Helper para encontrar columnas de forma más flexible ---
        def encontrar_col_flexible(df_column_list, posibles_nombres_map):
            for nombre_map_exacto in posibles_nombres_map:
                if nombre_map_exacto in df_column_list: return nombre_map_exacto
            for nombre_map_parcial in posibles_nombres_map:
                for col_df in df_column_list:
                    if nombre_map_parcial in col_df: return col_df
            return None
        # --- Fin encontrar_col_flexible ---

        map_nombre = ["nombre", "producto", "nombreproducto", "articulo", "item", "título", "title", "detalle", "brand", "varietal"] # Añadido brand/varietal
        map_descripcion = ["descripcion", "descripción"]
        map_precio = ["precio", "valor", "importe", "price", "precio venta", "precio final", "contado", "precio lista", "sugerido"]
        # ... (otros map_*)
        
        col_nombre_usar = encontrar_col_flexible(df.columns, map_nombre)
        # ... (encontrar otras columnas)
        col_precio_usar = encontrar_col_flexible(df.columns, map_precio)


        for index, row in df.iterrows():
            try:
                nombre = str(row.get(col_nombre_usar, "")).strip() if col_nombre_usar else ""
                # Para bodegas, podrías tener lógica para concatenar BRAND y VARIETAL si existen
                
                if not nombre and len(df.columns) > 0: # Fallback si no hay col_nombre_usar
                    nombre = str(row[df.columns[0]]).strip()

                if not nombre or len(nombre) < 3:
                    # logging.warning(f"Fila {index+2} de {base_filename} omitida: nombre vacío o muy corto.")
                    continue

                desc_val = row.get(encontrar_col_flexible(df.columns, map_descripcion)) if encontrar_col_flexible(df.columns, map_descripcion) else None
                descripcion = limpiar_texto_base(str(desc_val)) if pd.notna(desc_val) and str(desc_val).strip() else nombre

                precio_crudo = row.get(col_precio_usar) if col_precio_usar else ""
                # Usar parse_precio_flexible de utils.py
                precio_str_norm, precio_flt, moneda = parse_precio_flexible(precio_crudo)
                
                # ... (extraer otros campos como cantidad, categoria, sku, unidad como antes) ...
                unidad = str(row.get(encontrar_col_flexible(df.columns, ["unidad", "presentacion"]), "")).strip()


                producto_dict = {
                    "user_id": pyme_user_id,
                    "nombre": nombre[:250],
                    "descripcion": descripcion[:500],
                    "precio_str": precio_str_norm if precio_str_norm else "",
                    "precio_float": precio_flt,
                    "moneda": moneda if moneda else "ARS",
                    "unidad": unidad,
                    # "cantidad_disponible": ..., "categoria": ..., "sku": ...,
                }
                productos_extraidos.append(producto_dict)
            except Exception as e_row:
                logging.error(f"Error procesando fila {index+2} de {base_filename}: {e_row}", exc_info=True)
        
        logging.info(f"Total productos extraídos de {base_filename}: {len(productos_extraidos)}")
        return productos_extraidos
    # ... (tus bloques except) ...
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