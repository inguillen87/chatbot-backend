import pandas as pd
import logging
import re 
import os 

def normalizar_precio_excel_str(precio_val) -> str:
    # ... (código como antes)
    if pd.isna(precio_val) if 'pd' in globals() and isinstance(precio_val, float) else not precio_val: # Asegurar que pd esté en scope
        return ""
    precio_str = str(precio_val)
    precio = precio_str.replace("$", "").strip()
    if ',' in precio and '.' in precio:
        if precio.rfind(',') > precio.rfind('.'): 
            precio = precio.replace('.', '') 
            precio = precio.replace(',', '.') 
        else: 
            precio = precio.replace(',', '') 
    elif ',' in precio: 
        precio = precio.replace(',', '.')
    if re.fullmatch(r"\d+(\.\d{1,2})?", precio): # Asegurar que re esté en scope
        return precio
    logging.warning(f"Precio no pudo normalizarse a formato D.CC: '{precio_str}' -> '{precio}'")
    return ""

def extraer_precio_excel_float(precio_normalizado_str: str) -> float | None:
    if not precio_normalizado_str: return None
    try:
        return float(precio_normalizado_str)
    except ValueError:
        return None

def procesar_catalogo_excel(path: str, pyme_user_id: int = 0) -> list[dict]:
    try:
        # Ahora 'os' está definido y estas líneas funcionarán
        ext = os.path.splitext(path)[1].lower() 
        base_filename = os.path.basename(path) # Para logging
        
        if ext == ".csv":
            try:
                # Añadir encoding='utf-8' o 'latin1' puede ayudar con CSVs problemáticos
                df = pd.read_csv(path, sep=None, engine='python', on_bad_lines='warn', encoding='utf-8')
            except UnicodeDecodeError:
                logging.warning(f"Error leyendo CSV {base_filename} con UTF-8, intentando con latin1.")
                df = pd.read_csv(path, sep=None, engine='python', on_bad_lines='warn', encoding='latin1')
            except Exception as e_csv:
                logging.error(f"Error leyendo CSV {base_filename}: {e_csv}. Intentando con delimitador ';'.")
                try:
                    df = pd.read_csv(path, sep=';', engine='python', on_bad_lines='warn', encoding='utf-8')
                except UnicodeDecodeError:
                    logging.warning(f"Error leyendo CSV {base_filename} con UTF-8 y ';', intentando con latin1.")
                    df = pd.read_csv(path, sep=';', engine='python', on_bad_lines='warn', encoding='latin1')
                except Exception as e_csv2:
                    logging.error(f"Error leyendo CSV {base_filename} con ';' : {e_csv2}. Re-lanzando.")
                    raise
        elif ext in [".xlsx", ".xls"]:
            df = pd.read_excel(path, engine=None)
        else:
            logging.error(f"Extensión no soportada '{ext}' en procesar_catalogo_excel para archivo {base_filename}.")
            return []

        if df.empty:
            logging.warning(f"⚠️ El archivo Excel/CSV {base_filename} está vacío o no se pudo leer.")
            return []

        logging.info(f"Archivo Excel/CSV {base_filename} cargado. {len(df)} filas. Columnas: {df.columns.tolist()}")
        productos_extraidos = []

        col_nombre = "nombre"
        col_descripcion = "descripcion"
        col_precio = "precio"
        # ... (tus otras definiciones de col_*)

        for index, row in df.iterrows():
            try:
                nombre = str(row.get(col_nombre, "")).strip()
                descripcion_val = row.get(col_descripcion)
                descripcion = str(descripcion_val).strip() if pd.notna(descripcion_val) and str(descripcion_val).strip() else nombre

                if not nombre:
                    logging.warning(f"Fila {index+2} de {base_filename} omitida: nombre vacío.")
                    continue

                precio_crudo = row.get(col_precio)
                precio_str_normalizado = normalizar_precio_excel_str(precio_crudo) # Usa la función definida arriba
                precio_float = extraer_precio_excel_float(precio_str_normalizado) # Usa la función definida arriba
                
                # ... (resto de tu lógica para extraer cantidad, categoria, sku, unidad, moneda)
                cantidad_val = row.get("cantidad") # Asegúrate que "cantidad" sea el nombre de tu columna
                cantidad_str = str(cantidad_val).strip() if pd.notna(cantidad_val) and str(cantidad_val).strip() else "1"
                
                categoria = str(row.get("categoria", "")).strip()
                sku = str(row.get("sku", "")).strip()
                unidad = str(row.get("unidad", "")).strip()
                moneda = str(row.get("moneda", "ARS")).strip().upper()

                producto_dict = {
                    "user_id": pyme_user_id,
                    "nombre": nombre[:250],
                    "descripcion": descripcion[:500],
                    "precio_str": precio_str_normalizado,
                    "precio_float": precio_float,
                    "moneda": moneda if moneda else "ARS",
                    "cantidad_disponible": cantidad_str,
                    "categoria": categoria,
                    "sku": sku,
                    "unidad": unidad,
                }
                productos_extraidos.append(producto_dict)

            except Exception as e_row:
                logging.error(f"Error procesando fila {index+2} de {base_filename}: {e_row}", exc_info=True)
                continue

        logging.info(f"Total de productos extraídos de {base_filename}: {len(productos_extraidos)}")
        return productos_extraidos

    except FileNotFoundError:
        logging.error(f"❌ Archivo no encontrado: {path}")
        return []
    except pd.errors.EmptyDataError: # Asegúrate que pd esté en scope o importa pandas as pd
        logging.warning(f"⚠️ Archivo {os.path.basename(path)} vacío (Pandas EmptyDataError).") # os.path.basename
        return []
    except Exception as e: # Fallback general
        # Necesitas 'os' para os.path.basename(path) aquí también
        filename_for_error = os.path.basename(path) if 'os' in globals() else path
        logging.error(f"❌ Error fatal procesando Excel/CSV {filename_for_error}: {e}", exc_info=True)
        return []