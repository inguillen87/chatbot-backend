# En tu archivo: services/procesar_catalogo_excel.py

import pandas as pd
import logging
import re 
import os
from .utils import limpiar_texto_base, parse_precio_flexible # <--- IMPORTAR DE UTILS

# Si decides NO usar parse_precio_flexible de utils.py para Excel y mantener
# las funciones normalizar_precio_excel_str y extraer_precio_excel_float específicas
# aquí, entonces esas definiciones irían aquí (pero es mejor usar la de utils si es adaptable).
# Por ahora, asumiré que podrías querer usar parse_precio_flexible para consistencia.

def procesar_catalogo_excel(path: str, pyme_user_id: int = 0) -> list[dict]:
    try:
        ext = os.path.splitext(path)[1].lower()
        base_filename = os.path.basename(path)
        
        # ... (lógica de lectura de df_preliminar y detección de encabezados como te la pasé antes) ...
        # ... (asegúrate que esta parte esté completa y sea la versión mejorada) ...
        df_preliminar = None
        if ext == ".csv":
            try:
                df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='skip', encoding='utf-8', skip_blank_lines=True)
            except UnicodeDecodeError:
                logging.warning(f"Error leyendo CSV {base_filename} con UTF-8, intentando con latin1.")
                df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='skip', encoding='latin1', skip_blank_lines=True)
        elif ext in [".xlsx", ".xls"]:
            df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0)
        else:
            logging.error(f"Extensión no soportada '{ext}' para {base_filename}.")
            return []

        if df_preliminar is None or df_preliminar.empty:
            logging.warning(f"⚠️ El archivo {base_filename} está vacío o no se pudo leer preliminarmente.")
            return []

        keywords_header = ["BRAND", "VARIETAL", "UNIT/CAJA", "PALLET", "CONTADO", "PRECIO LISTA", "SUGERIDO", "NOMBRE", "PRODUCTO", "ARTICULO", "PRECIO", "DESCRIPCION"]
        
        def encontrar_fila_encabezados(df_check, posibles_palabras): # Función helper local
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

        idx_fila_encabezados = encontrar_fila_encabezados(df_preliminar.head(10), keywords_header)
        df = None
        if idx_fila_encabezados is not None:
            if ext == ".csv": df = pd.read_csv(path, sep=None, engine='python', header=0, skiprows=idx_fila_encabezados, on_bad_lines='skip', encoding='utf-8')
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

        def encontrar_col_flexible(df_column_list, posibles_nombres_map): # Función helper local
            for nombre_map_exacto in posibles_nombres_map:
                if nombre_map_exacto in df_column_list: return nombre_map_exacto
            for nombre_map_parcial in posibles_nombres_map:
                for col_df in df_column_list:
                    if nombre_map_parcial in col_df: return col_df
            return None
        
        map_brand = ["brand", "marca"]
        map_varietal = ["varietal"]
        map_nombre = ["nombre", "producto", "nombreproducto", "articulo", "item", "título", "title", "detalle"] 
        map_descripcion = ["descripcion", "descripción"]
        map_precio = ["precio", "valor", "importe", "price", "precio venta", "precio final", "contado", "precio lista", "sugerido", "$ box", "$ bottle"] # Añadido $ box y $ bottle
        map_unidad = ["unidad", "presentacion", "presentación", "empaque", "unit/box", "unit/caja"]
        # ... (otros map_*)
        
        col_brand_usar = encontrar_col_flexible(df.columns, map_brand)
        col_varietal_usar = encontrar_col_flexible(df.columns, map_varietal)
        col_nombre_generico_usar = encontrar_col_flexible(df.columns, map_nombre)
        col_precio_usar = encontrar_col_flexible(df.columns, map_precio)
        col_unidad_usar = encontrar_col_flexible(df.columns, map_unidad)
        # ... (encontrar otras columnas como descripción, categoría, sku, moneda)
        col_desc_usar = encontrar_col_flexible(df.columns, map_descripcion)


        for index, row in df.iterrows():
            try:
                nombre_base = ""
                # Lógica para construir nombre_base (ej. concatenar brand y varietal, o tomar de col_nombre_generico_usar)
                if col_brand_usar and pd.notna(row.get(col_brand_usar)):
                    nombre_base += str(row.get(col_brand_usar, "")).strip()
                if col_varietal_usar and pd.notna(row.get(col_varietal_usar)):
                    nombre_base = (nombre_base + " " + str(row.get(col_varietal_usar, ""))).strip()
                
                if not nombre_base and col_nombre_generico_usar and pd.notna(row.get(col_nombre_generico_usar)):
                    nombre_base = str(row.get(col_nombre_generico_usar, "")).strip()
                elif not nombre_base and len(df.columns) > 0 and pd.notna(row[df.columns[0]]): # Fallback a primera columna
                     nombre_base = str(row[df.columns[0]]).strip()

                # Ahora sí, llamar a limpiar_texto_base que está importada de utils
                nombre_final = limpiar_texto_base(nombre_base)

                if not nombre_final or len(nombre_final) < 3:
                    continue

                desc_val = row.get(col_desc_usar) if col_desc_usar else None
                descripcion_final = limpiar_texto_base(str(desc_val)) if pd.notna(desc_val) and str(desc_val).strip() else nombre_final

                precio_crudo = row.get(col_precio_usar) if col_precio_usar else ""
                # Usar parse_precio_flexible de utils.py
                precio_str_norm, precio_flt, moneda = parse_precio_flexible(precio_crudo)
                
                if not precio_flt and len(nombre_final) < 10:
                    continue
                
                unidad_val = row.get(col_unidad_usar) if col_unidad_usar else "" # Usar col_unidad_usar
                unidad = limpiar_texto_base(str(unidad_val)) if pd.notna(unidad_val) else "unidad"
                
                # ... (extraer otros campos: cantidad_disponible, categoria, sku) ...
                # (Asegúrate de tener mapeos para estas columnas si existen en tus Excels)
                cantidad_str = str(row.get(encontrar_col_flexible(df.columns, ["cantidad", "stock"]), "1")).strip()
                categoria_str = str(row.get(encontrar_col_flexible(df.columns, ["categoria", "línea"]), "")).strip()
                sku_str = str(row.get(encontrar_col_flexible(df.columns, ["sku", "código", "cod", "art."]), "")).strip()


                producto_dict = {
                    "user_id": pyme_user_id,
                    "nombre": nombre_final[:250],
                    "descripcion": descripcion_final[:500],
                    "precio_str": precio_str_norm if precio_str_norm else "",
                    "precio_float": precio_flt,
                    "moneda": moneda if moneda else "ARS",
                    "unidad": unidad,
                    "cantidad_disponible": cantidad_str,
                    "categoria": categoria_str,
                    "sku": sku_str,
                }
                productos_extraidos.append(producto_dict)

            except Exception as e_row:
                logging.error(f"Error procesando fila {index+2} de {base_filename}: {e_row}", exc_info=True)
        
        logging.info(f"Total productos extraídos de {base_filename}: {len(productos_extraidos)}")
        if not productos_extraidos and len(df) > 0 :
             logging.warning(f"Se leyeron {len(df)} filas de {base_filename} pero no se extrajeron productos válidos. Revisa el mapeo de columnas o la estructura del archivo Excel/CSV.")
        return productos_extraidos

    except FileNotFoundError:
        logging.error(f"❌ Archivo no encontrado: {path}")
        return []
    except pd.errors.EmptyDataError:
        logging.warning(f"⚠️ Archivo {os.path.basename(path)} vacío (Pandas EmptyDataError).")
        return []
    except Exception as e:
        filename_for_error = os.path.basename(path) if 'os' in globals() and hasattr(os, 'path') else path # Chequeo más seguro
        logging.error(f"❌ Error fatal procesando Excel/CSV {filename_for_error}: {e}", exc_info=True)
        return []