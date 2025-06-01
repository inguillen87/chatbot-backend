# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re # Importar re si se usa para limpieza adicional
from .utils import limpiar_texto_base, parse_precio_flexible # Importar de utils

logger = logging.getLogger(__name__)

def procesar_catalogo_excel(path: str, pyme_user_id: int) -> list[dict]:
    productos_extraidos = []
    base_filename = os.path.basename(path)
    logger.info(f"Iniciando procesamiento de archivo Excel/CSV: {base_filename} para user_id: {pyme_user_id}")

    try:
        ext = os.path.splitext(path)[1].lower()
        df_preliminar = None
        encoding_to_try = ['utf-8', 'latin1', 'iso-8859-1', 'cp1252'] # Lista de codificaciones comunes

        if ext == ".csv":
            for enc in encoding_to_try:
                try:
                    df_preliminar = pd.read_csv(path, sep=None, engine='python', header=None, on_bad_lines='warn', encoding=enc, skip_blank_lines=True, low_memory=False)
                    logger.info(f"CSV '{base_filename}' leído exitosamente con encoding '{enc}'.")
                    break 
                except UnicodeDecodeError:
                    logger.warning(f"Error leyendo CSV '{base_filename}' con encoding '{enc}'. Intentando siguiente...")
                except Exception as e_csv_read_prelim:
                    logger.warning(f"Error inesperado leyendo CSV '{base_filename}' con encoding '{enc}': {e_csv_read_prelim}")
                    df_preliminar = None # Asegurar que sea None si falla
                    break # Salir del bucle de encodings si hay otro error que no sea UnicodeDecodeError
            if df_preliminar is None:
                 logger.error(f"No se pudo leer el CSV '{base_filename}' con ninguna de las codificaciones probadas.")
                 return []
        elif ext in [".xlsx", ".xls"]:
            try:
                df_preliminar = pd.read_excel(path, engine=None, header=None, sheet_name=0) # Probar primera hoja
            except Exception as e_excel_read:
                logger.error(f"Error leyendo archivo Excel '{base_filename}': {e_excel_read}", exc_info=True)
                return []
        else:
            logger.error(f"Extensión no soportada '{ext}' para archivo '{base_filename}'.")
            return []

        if df_preliminar is None or df_preliminar.empty:
            logger.warning(f"⚠️ El archivo '{base_filename}' está vacío o no se pudo leer preliminarmente.")
            return []

        # Palabras clave expandidas para detectar la fila de encabezados
        keywords_header = [
            "nombre", "producto", "articulo", "item", "título", "title", "detalle", "descripción producto", "designación",
            "descripcion", "descripción", "observaciones", "info adicional", "caracteristicas",
            "precio", "valor", "importe", "price", "venta", "final", "contado", "lista", "sugerido", "pvp", "oferta", "costo", "tarifa",
            "unidad", "presentacion", "presentación", "empaque", "formato", "medida", "u/m", "unidades x bulto", "fraccion",
            "categoria", "línea", "rubro", "familia", "tipo", "subrubro",
            "sku", "código", "cod", "art", "referencia", "ref", "id producto", "item no", "codigo ean", "codigo interno",
            "marca", "brand", "fabricante", "bodega", "elaborado por",
            "stock", "cantidad", "disponible", "existencias", "cant.", "disponibilidad",
            "moneda", "currency", "divisa",
            "ean", "upc", "codigo de barras", "código de barras",
            "color", "talle", "tamaño", "variante", "modelo", "año", "cosecha"
        ]
        
        def encontrar_fila_encabezados_excel(df_check, posibles_palabras, min_coincidencias=2):
            # ... (la lógica de esta función se mantiene como la tenías, es buena) ...
            logger.debug(f"Buscando encabezados en las primeras {len(df_check)} filas.")
            for i, row in df_check.iterrows():
                coincidencias = 0; celdas_con_keywords = set()
                for cell_idx, cell_value in enumerate(row):
                    if isinstance(cell_value, str) and cell_value.strip():
                        cell_lower = cell_value.lower()
                        for palabra_clave in posibles_palabras:
                            if palabra_clave.lower() in cell_lower:
                                celdas_con_keywords.add(cell_idx); break
                coincidencias = len(celdas_con_keywords)
                if coincidencias >= min_coincidencias:
                    logger.info(f"Fila de encabezados potencial detectada en índice Excel {i} (0-based) con {coincidencias} coincidencias. Contenido: {row.dropna().tolist()}")
                    return i
            logger.warning(f"No se encontró una fila de encabezados clara con {min_coincidencias} o más palabras clave. Se intentarán otras heurísticas.")
            return None

        idx_fila_encabezados = encontrar_fila_encabezados_excel(df_preliminar.head(20), keywords_header)
        
        df = None
        if idx_fila_encabezados is not None:
            logger.info(f"Usando fila en índice {idx_fila_encabezados} como encabezados.")
            header_row_index = idx_fila_encabezados
            try:
                if ext == ".csv": 
                    df = pd.read_csv(path, sep=None, engine='python', header=0, skiprows=header_row_index, on_bad_lines='warn', encoding=df_preliminar.attrs.get('encoding', 'utf-8'), skip_blank_lines=True, low_memory=False)
                else: 
                    df = pd.read_excel(path, engine=None, header=0, skiprows=header_row_index, sheet_name=0)
                df.dropna(how='all', inplace=True)
            except Exception as e_read_main:
                logger.error(f"Error leyendo el archivo '{base_filename}' después de detectar encabezados: {e_read_main}", exc_info=True)
                return []
        else: # Si no se encontró por keywords, intentar primera fila como último recurso
            logger.warning(f"No se detectó fila de encabezados por keywords. Usando primera fila como encabezados para '{base_filename}'.")
            df = df_preliminar.copy()
            if not df.empty:
                 df.columns = df.iloc[0].astype(str).apply(limpiar_texto_base) # Limpiar headers de primera fila
                 df = df[1:].reset_index(drop=True)
                 df.dropna(how='all', inplace=True)


        if df is None or df.empty: 
            logger.warning(f"DataFrame vacío después de procesar encabezados para '{base_filename}'. No se pueden extraer productos.")
            return []
            
        original_columns = df.columns.tolist() # Guardar para logging
        df.columns = [limpiar_texto_base(str(col)) for col in df.columns]
        logger.info(f"DataFrame para '{base_filename}' cargado con {len(df)} filas. Columnas originales: {original_columns} -> Normalizadas: {df.columns.tolist()}")

        # MAPEO DE COLUMNAS MUY EXPANDIDO
        map_nombre = ["nombre", "producto", "nombreproducto", "articulo", "artículo", "item", "título", "title", "detalle", "descripción producto", "designacion", "denominacion", "name", "product name"]
        map_descripcion = ["descripcion", "descripción", "detalle producto", "info adicional", "caracteristicas", "observaciones", "notas", "description", "product description"]
        map_precio = [
            "precio", "valor", "importe", "price", "precio venta", "precio final", "contado", "efectivo",
            "precio lista", "lista", "sugerido", "pvp", "p.v.p.", "p.v.p", "oferta", "precio oferta",
            "costo", "tarifa", "$ box", "$ bottle", "precio distribuidor", "precio sugerido publico", "unit price", "sale price"
        ]
        map_unidad = ["unidad", "presentacion", "presentación", "empaque", "formato", "medida", "u/m", "unidades x bulto", "fraccion", "unit/box", "unit/caja", "package", "size"]
        map_categoria = ["categoria", "categoría", "línea", "linea", "rubro", "familia", "tipo", "subrubro", "clase", "department", "category"]
        map_sku = ["sku", "código", "codigo", "cód.", "cod", "art", "articulo nro", "referencia", "ref", "id producto", "item no", "product id", "item code"]
        map_marca = ["marca", "brand", "fabricante", "bodega", "elaborado por", "productor", "manufacturer"]
        map_stock = ["stock", "cantidad", "disponible", "disponibilidad", "existencias", "cant.", "inventory", "quantity", "qty", "available"]
        map_ean = ["ean", "upc", "codigo de barras", "código de barras", "barcode"]
        map_moneda = ["moneda", "currency", "divisa"] # Aunque parse_precio_flexible ya lo intenta

        def encontrar_col_flexible_excel(df_cols_list, posibles_nombres_map_list):
            # ... (tu función se mantiene, es buena) ...
            for nombre_exacto in posibles_nombres_map_list:
                if nombre_exacto in df_cols_list: return nombre_exacto
            for nombre_parcial in posibles_nombres_map_list: # Buscar como substring
                for col_df_actual in df_cols_list:
                    if nombre_parcial in col_df_actual: return col_df_actual
            return None
        
        col_nombre = encontrar_col_flexible_excel(df.columns, map_nombre)
        col_descripcion = encontrar_col_flexible_excel(df.columns, map_descripcion)
        col_precio = encontrar_col_flexible_excel(df.columns, map_precio)
        col_unidad = encontrar_col_flexible_excel(df.columns, map_unidad)
        col_categoria = encontrar_col_flexible_excel(df.columns, map_categoria)
        col_sku = encontrar_col_flexible_excel(df.columns, map_sku)
        col_marca = encontrar_col_flexible_excel(df.columns, map_marca)
        col_stock = encontrar_col_flexible_excel(df.columns, map_stock)
        col_moneda_explicita = encontrar_col_flexible_excel(df.columns, map_moneda) # Para moneda explícita

        logger.info(f"Mapeo final de columnas para '{base_filename}': Nombre='{col_nombre}', Desc='{col_descripcion}', Precio='{col_precio}', Unidad='{col_unidad}', Categ='{col_categoria}', SKU='{col_sku}', Marca='{col_marca}', Stock='{col_stock}', MonedaCol='{col_moneda_explicita}'")

        for index, row in df.iterrows():
            try:
                # Construir nombre (Marca + Nombre Genérico)
                nombre_prod_construido = ""
                marca_val = str(row.get(col_marca, "")).strip() if col_marca and pd.notna(row.get(col_marca)) else ""
                if marca_val: nombre_prod_construido += marca_val
                
                nombre_generico_val = str(row.get(col_nombre, "")).strip() if col_nombre and pd.notna(row.get(col_nombre)) else ""
                if nombre_generico_val: nombre_prod_construido = (nombre_prod_construido + " " + nombre_generico_val).strip()
                
                if not nombre_prod_construido and len(df.columns) > 0: # Fallback a la primera columna si no hay nombre
                    nombre_prod_construido = str(row[df.columns[0]]).strip() if pd.notna(row[df.columns[0]]) else ""
                
                nombre_final = limpiar_texto_base(nombre_prod_construido)

                if not nombre_final or len(nombre_final) < 2:
                    # logger.debug(f"Fila {index+2} omitida: nombre de producto inválido o muy corto ('{nombre_final}')")
                    continue

                desc_val = str(row.get(col_descripcion, "")).strip() if col_descripcion and pd.notna(row.get(col_descripcion)) else ""
                descripcion_final = limpiar_texto_base(desc_val)
                if descripcion_final == nombre_final: descripcion_final = "" # Evitar redundancia

                precio_crudo_val = str(row.get(col_precio, "")).strip() if col_precio and pd.notna(row.get(col_precio)) else ""
                precio_str_norm, precio_flt, moneda_detectada_en_precio = parse_precio_flexible(precio_crudo_val)
                
                moneda_final = moneda_detectada_en_precio if moneda_detectada_en_precio else "ARS" # Default ARS
                if col_moneda_explicita and pd.notna(row.get(col_moneda_explicita)) and str(row.get(col_moneda_explicita)).strip():
                    moneda_de_columna = str(row.get(col_moneda_explicita)).strip().upper()
                    if moneda_de_columna in ["USD", "ARS", "EUR", "GBP", "CLP"]: # Validar
                        moneda_final = moneda_de_columna # Priorizar columna de moneda si existe y es válida
                
                unidad_val = str(row.get(col_unidad, "")).strip() if col_unidad and pd.notna(row.get(col_unidad)) else ""
                unidad_final = limpiar_texto_base(unidad_val) if unidad_val else "unidad"

                categoria_val = str(row.get(col_categoria, "")).strip() if col_categoria and pd.notna(row.get(col_categoria)) else ""
                categoria_final = limpiar_texto_base(categoria_val)

                sku_val = str(row.get(col_sku, "")).strip() if col_sku and pd.notna(row.get(col_sku)) else ""
                sku_final = limpiar_texto_base(sku_val)

                stock_val = str(row.get(col_stock, "1")).strip() if col_stock and pd.notna(row.get(col_stock)) else "1" # Default a "1" si no hay stock
                cantidad_final = limpiar_texto_base(stock_val) if stock_val else "1"


                # Filtro final: si no hay nombre Y (no hay precio_str ni precio_float), probablemente no es un producto válido
                if not nombre_final and not precio_str_norm and precio_flt is None:
                    # logger.debug(f"Fila {index+2} omitida: sin nombre ni precio válidos.")
                    continue

                producto_dict = {
                    "user_id": pyme_user_id,
                    "nombre": nombre_final[:250],
                    "descripcion": descripcion_final[:1000],
                    "precio_str": precio_str_norm if precio_str_norm else "",
                    "precio_float": precio_flt, # Puede ser None
                    "moneda": moneda_final,
                    "unidad": unidad_final[:50],
                    "cantidad_disponible": cantidad_final[:50], # Nombre de campo consistente con PDF
                    "categoria": categoria_final[:100],
                    "sku": sku_final[:100],
                    "marca": marca_val[:100] if marca_val else "" # Guardar marca si se extrajo
                }
                productos_extraidos.append(producto_dict)

            except Exception as e_row:
                # idx_fila_encabezados puede ser None, así que chequear antes de sumar
                fila_real = index + (idx_fila_encabezados + 1 if idx_fila_encabezados is not None else 0) + 1
                logger.error(f"Error procesando fila {fila_real} de '{base_filename}': {e_row}", exc_info=True)
        
        logger.info(f"Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
        if not productos_extraidos and len(df) > 0 :
             logger.warning(f"Se leyeron {len(df)} filas de '{base_filename}' pero no se extrajeron productos válidos. Revisa el mapeo de columnas o la estructura del archivo.")
        return productos_extraidos

    except FileNotFoundError:
        logger.error(f"❌ Archivo no encontrado en la ruta: {path}")
        raise # Relanzar para que el endpoint lo maneje
    except pd.errors.EmptyDataError:
        logger.warning(f"⚠️ Archivo '{os.path.basename(path)}' parece estar vacío (Pandas EmptyDataError).")
        return [] 
    except Exception as e_general:
        filename_for_error = os.path.basename(path)
        logger.error(f"❌ Error fatal procesando archivo Excel/CSV '{filename_for_error}': {e_general}", exc_info=True)
        raise ValueError(f"No se pudo procesar el archivo '{filename_for_error}'. Verifique el formato y contenido.")