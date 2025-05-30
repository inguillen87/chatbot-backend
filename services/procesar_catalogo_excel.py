# En tu archivo: services/procesar_catalogo_excel.py

import pandas as pd
import logging
import re # Para limpieza más avanzada si es necesario

# Podrías reusar las funciones de google_docai.py si las centralizas
# Por ahora, defino una similar aquí para precios.
def normalizar_precio_excel_str(precio_val) -> str:
    """Limpia y normaliza un valor de precio (puede ser string o numérico de pandas) a 'DDDD.CC'."""
    if pd.isna(precio_val) or precio_val == "":
        return ""
    
    precio_str = str(precio_val) # Convertir a string primero
    
    # Quitar símbolo de moneda y espacios
    precio = precio_str.replace("$", "").strip()
    
    # Lógica de normalización de comas y puntos (similar a la de google_docai.py)
    if ',' in precio and '.' in precio:
        if precio.rfind(',') > precio.rfind('.'): # Coma es decimal (1.234,56)
            precio = precio.replace('.', '') 
            precio = precio.replace(',', '.') 
        else: # Punto es decimal (1,234.56)
            precio = precio.replace(',', '') 
    elif ',' in precio: 
        precio = precio.replace(',', '.')
    
    # Validar que sea un formato numérico válido después de la normalización inicial
    if re.fullmatch(r"\d+(\.\d{1,2})?", precio):
        # Asegurar dos decimales si es un número que podría no tenerlos.
        # Esto es opcional y depende de cómo quieras mostrar los precios.
        # if '.' not in precio:
        #     precio += ".00"
        # elif len(precio.split('.')[1]) == 1:
        #     precio += "0"
        return precio
    
    logging.warning(f"Precio no pudo normalizarse a formato D.CC: '{precio_str}' -> '{precio}'")
    return "" # Devolver vacío si no se pudo normalizar

def extraer_precio_excel_float(precio_normalizado_str: str) -> float | None:
    if not precio_normalizado_str: return None
    try:
        return float(precio_normalizado_str)
    except ValueError:
        return None

def procesar_catalogo_excel(path: str, pyme_user_id: int = 0) -> list[dict]: # Añadido pyme_user_id para consistencia
    try:
        ext = os.path.splitext(path)[1].lower() # Usar os.path.splitext
        
        if ext == ".csv":
            # Para CSV, intentar detectar separador si no es coma.
            # Esto es una heurística simple. Librerías como 'chardet' pueden ayudar con encoding.
            try:
                df = pd.read_csv(path, sep=None, engine='python', on_bad_lines='warn') # sep=None para autodetectar
            except Exception as e_csv:
                logging.error(f"Error leyendo CSV {os.path.basename(path)}: {e_csv}. Intentando con delimitador diferente.")
                try: # Fallback a punto y coma si la coma falla
                    df = pd.read_csv(path, sep=';', engine='python', on_bad_lines='warn')
                except Exception as e_csv2:
                    logging.error(f"Error leyendo CSV {os.path.basename(path)} con ';' : {e_csv2}. Re-lanzando.")
                    raise
        elif ext in [".xlsx", ".xls"]:
            df = pd.read_excel(path, engine=None) # engine=None para que pandas elija
        else:
            logging.error(f"Extensión no soportada '{ext}' en procesar_catalogo_excel.")
            return []

        if df.empty:
            logging.warning(f"⚠️ El archivo Excel/CSV {os.path.basename(path)} está vacío o no se pudo leer correctamente.")
            return []

        logging.info(f"Archivo Excel/CSV {os.path.basename(path)} cargado. {len(df)} filas encontradas. Columnas: {df.columns.tolist()}")
        productos_extraidos = []

        # --- Nombres de columna esperados (puedes hacerlos configurables o pedir a PYMES que los usen) ---
        # Estos son los nombres que buscará tu script. Si la PYME usa otros, no los encontrará.
        col_nombre = "nombre" # Tu nombre actual
        col_descripcion = "descripcion" # Tu nombre actual
        col_precio = "precio" # Tu nombre actual
        col_cantidad = "cantidad" # Tu nombre actual
        col_categoria = "categoria" # Ejemplo de columna adicional
        col_sku = "sku" # Ejemplo
        col_unidad = "unidad" # Ejemplo
        col_moneda = "moneda" # Ejemplo

        for index, row in df.iterrows():
            try:
                nombre = str(row.get(col_nombre, "")).strip()
                
                # Usar el nombre para la descripción si la columna de descripción está vacía o no existe
                descripcion_val = row.get(col_descripcion)
                descripcion = str(descripcion_val).strip() if pd.notna(descripcion_val) and str(descripcion_val).strip() else nombre

                if not nombre: # Requerir al menos un nombre
                    logging.warning(f"Fila {index+2} omitida: nombre de producto vacío.")
                    continue

                precio_crudo = row.get(col_precio) # Puede ser numérico o string
                precio_str_normalizado = normalizar_precio_excel_str(precio_crudo)
                precio_float = extraer_precio_excel_float(precio_str_normalizado)

                # Cantidad: si es una columna, o default "1"
                cantidad_val = row.get(col_cantidad)
                cantidad_str = str(cantidad_val).strip() if pd.notna(cantidad_val) and str(cantidad_val).strip() else "1"
                
                # Otros campos que podrías querer extraer si existen en el Excel
                categoria = str(row.get(col_categoria, "")).strip()
                sku = str(row.get(col_sku, "")).strip()
                unidad = str(row.get(col_unidad, "")).strip()
                moneda = str(row.get(col_moneda, "ARS")).strip().upper()


                # Crear el diccionario estructurado
                producto_dict = {
                    "user_id": pyme_user_id, # Incluir para consistencia con procesar_pdf
                    "nombre": nombre[:250], # Limitar longitud
                    "descripcion": descripcion[:500],
                    "precio_str": precio_str_normalizado, # Precio formateado como string
                    "precio_float": precio_float, # Precio como float
                    "moneda": moneda if moneda else "ARS",
                    "cantidad_disponible": cantidad_str, # Renombrar para claridad vs "cantidad pedida"
                    "categoria": categoria,
                    "sku": sku,
                    "unidad": unidad,
                    # texto_para_embedding se construirá en upload_processor.py
                }
                productos_extraidos.append(producto_dict)

            except Exception as e_row:
                logging.error(f"Error procesando fila {index+2} del archivo {os.path.basename(path)}: {e_row}", exc_info=True)
                continue # Saltar a la siguiente fila

        logging.info(f"Total de productos estructurados extraídos de Excel/CSV: {len(productos_extraidos)}")
        return productos_extraidos

    except FileNotFoundError:
        logging.error(f"❌ Archivo no encontrado en la ruta: {path}")
        return []
    except pd.errors.EmptyDataError:
        logging.warning(f"⚠️ El archivo {os.path.basename(path)} está vacío (EmptyDataError de Pandas).")
        return []
    except Exception as e:
        logging.error(f"❌ Error fatal procesando archivo Excel/CSV {os.path.basename(path)}: {e}", exc_info=True)
        return []