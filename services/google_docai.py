# En tu archivo: services/google_docai.py

import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account
# from price_parser import Price # Descomenta si decides usar price-parser

# --- Carga de Credenciales (igual que antes) ---
# (Tu código de carga de credenciales aquí)
# ... (igual que tu última versión) ...
if 'google.oauth2' not in globals() or 'service_account' not in globals()['google.oauth2'].__dict__:
    logging.warning("google.oauth2.service_account no parece estar disponible globalmente como se esperaba.")
try:
    ruta_render = "/etc/secrets/google_service_key.json"
    ruta_local = "instance/google-credentials.json"
    ruta_cred = ruta_render if os.path.exists(ruta_render) else ruta_local
    with open(ruta_cred, "r") as f:
        credentials_info = json.load(f)
    credentials = service_account.Credentials.from_service_account_info(credentials_info)
    logging.info(f"Credenciales de Google cargadas desde: {ruta_cred}")
except FileNotFoundError:
    logging.error(f"❌ Archivo de credenciales de Google no encontrado en {ruta_render} ni en {ruta_local}.")
    raise RuntimeError(f"Archivo de credenciales no encontrado.")
except Exception as e:
    logging.error(f"❌ Error crítico al cargar credenciales de Google: {e}")
    raise RuntimeError(f"❌ Error al cargar credenciales: {e}")
# --- Fin Carga de Credenciales ---

def limpiar_texto_base(texto: str) -> str:
    if not texto: return ""
    return re.sub(r'\s+', ' ', texto).strip()

def parse_precio_flexible(texto_precio: str) -> tuple[str | None, float | None, str | None]:
    """
    Intenta extraer y normalizar un precio y su moneda de un string.
    Devuelve (precio_str_original_limpio, precio_float, moneda_detectada).
    """
    if not texto_precio:
        return None, None, None

    # Opcional: Usar price-parser para una detección más robusta
    # try:
    #     parsed = Price.fromstring(texto_precio)
    #     if parsed.amount is not None:
    #         return str(parsed.amount), float(parsed.amount_float), parsed.currency
    # except Exception as e_pp:
    #     logging.debug(f"Price-parser no pudo analizar '{texto_precio}': {e_pp}")
    # Si no se usa price-parser o falla, usar regex:

    texto_limpio = limpiar_texto_base(texto_precio)
    moneda = "ARS" # Default

    # Regex mejorada para precios:
    # Grupo 1: Símbolo de moneda opcional ($, USD, ARS)
    # Grupo 2: El número en sí (con . para miles y , para decimales, O , para miles y . para decimales, O solo números)
    # Grupo 3: Símbolo de moneda opcional al final
    # (?i) para case-insensitive para USD, ARS
    # Permite espacios entre símbolo y número
    m = re.search(r"((?:(?i)\$|USD|ARS)\s*)?([\d.,]+)((?i)\s*(?:USD|ARS|\$))?", texto_limpio)
    
    if not m:
        return None, None, None

    simbolo_prefijo = m.group(1)
    numero_str = m.group(2)
    simbolo_sufijo = m.group(3)

    if simbolo_prefijo:
        if "USD" in simbolo_prefijo.upper(): moneda = "USD"
        elif "ARS" in simbolo_prefijo.upper(): moneda = "ARS"
    elif simbolo_sufijo:
        if "USD" in simbolo_sufijo.upper(): moneda = "USD"
        elif "ARS" in simbolo_sufijo.upper(): moneda = "ARS"

    # Normalizar el número_str (1.234,56 -> 1234.56 o 1,234.56 -> 1234.56)
    precio_normalizado_str = numero_str
    if ',' in numero_str and '.' in numero_str:
        if numero_str.rfind(',') > numero_str.rfind('.'): # Formato ARS: 1.234,56
            precio_normalizado_str = numero_str.replace('.', '').replace(',', '.')
        else: # Formato USA: 1,234.56
            precio_normalizado_str = numero_str.replace(',', '')
    elif ',' in numero_str: # Solo comas, asumir formato ARS decimal: 1234,56
        precio_normalizado_str = numero_str.replace(',', '.')
    
    try:
        precio_flt = float(precio_normalizado_str)
        # Devolver el número original extraído (antes de normalizar separadores) para precio_str
        # y el float para cálculos, y la moneda.
        return limpiar_texto_base(numero_str), precio_flt, moneda
    except ValueError:
        logging.warning(f"No se pudo convertir precio a float: '{numero_str}' (normalizado de '{texto_precio}')")
        return limpiar_texto_base(numero_str), None, moneda # Devolver el string aunque no sea float válido

def extraer_info_producto_de_linea(linea: str, pyme_rubro_nombre: str) -> dict | None:
    """
    Intenta extraer nombre, precio, unidad, etc., de una sola línea de texto.
    Esta es la función que necesitará más heurísticas y ajustes.
    """
    linea_limpia = limpiar_texto_base(linea)
    if not linea_limpia or len(linea_limpia) < 5: # Ignorar líneas muy cortas
        return None

    # --- 1. Extraer todos los precios candidatos y sus posiciones ---
    # Regex mejorada para precios, que también captura el texto completo del precio encontrado
    # Grupo 0: texto completo del precio (ej. "$ 1.234,50")
    # Grupo 1: símbolo de moneda prefijo (opcional)
    # Grupo 2: el número
    # Grupo 3: símbolo de moneda sufijo (opcional)
    precio_regex_full = r"((?:\$|\bARS\b|\bUSD\b)?\s*[\d.,]+(?:[.,]\d{2})?)((?i)\s*(?:USD|ARS|\$))?"
    
    candidatos_precio = [] # Lista de tuplas (texto_precio, precio_float, moneda, inicio_span, fin_span)
    for match_p in re.finditer(precio_regex_full, linea_limpia):
        texto_precio_encontrado = match_p.group(0) # El string completo del precio ej. "$ 123.45"
        # Parsear este texto para obtener el float y la moneda
        _, p_float, p_moneda = parse_precio_flexible(texto_precio_encontrado)
        if p_float is not None: # Solo considerar si se pudo convertir a float
            candidatos_precio.append(
                (texto_precio_encontrado, p_float, p_moneda if p_moneda else "ARS", match_p.start(), match_p.end())
            )
    
    nombre_prod = linea_limpia # Default
    desc_prod = ""
    precio_str_final = ""
    precio_float_final = None
    moneda_final = "ARS"
    unidad_final = ""
    tipo_precio_final = "" # ej. "mayorista", "oferta"

    # --- 2. Lógica para seleccionar el precio principal y aislar el nombre ---
    if candidatos_precio:
        # Heurística: tomar el precio más a la derecha (último) como el principal.
        # Podrías añadir lógica para elegir el "más grande" o basado en palabras clave cercanas.
        mejor_candidato = candidatos_precio[-1]
        precio_texto_crudo_elegido, precio_float_final, moneda_final, inicio_precio, fin_precio = mejor_candidato
        precio_str_final = precio_texto_crudo_elegido # Guardar el string original (limpio) del precio elegido

        # Intentar aislar el nombre del producto
        # Opción 1: Todo a la izquierda del precio elegido
        nombre_prod = limpiar_texto_base(linea_limpia[:inicio_precio])
        # Opción 2: Quitar todos los precios de la línea y ver qué queda
        # texto_sin_todos_precios = linea_limpia
        # for cp_texto, _, _, _, _ in candidatos_precio:
        #     texto_sin_todos_precios = texto_sin_todos_precios.replace(cp_texto, "")
        # nombre_prod = limpiar_texto_base(texto_sin_todos_precios)

        # Si el nombre aislado es muy corto o vacío, y la línea original es más larga,
        # podría ser que el precio estuviera al principio o que la línea sea solo un precio.
        if not nombre_prod and len(linea_limpia) > len(precio_str_final) + 3:
             # Si no queda nombre, y no es solo un precio, podría ser problemático.
             # Revertir a tomar la línea (sin el precio principal) como nombre si es más largo
             temp_nombre = limpiar_texto_base(linea_limpia.replace(precio_str_final, ""))
             if len(temp_nombre) > len(nombre_prod):
                 nombre_prod = temp_nombre

        if not nombre_prod: # Si sigue vacío, esta línea probablemente era solo un precio o basura
            logging.debug(f"Línea descartada (solo precio o nombre no extraíble): '{linea_limpia}'")
            return None
    else:
        # No se encontraron precios, la línea podría ser un nombre/descripción o un encabezado.
        nombre_prod = linea_limpia # Mantenemos la línea como nombre
        # logging.debug(f"No se encontró precio con regex en línea: '{linea_limpia}'")


    # --- 3. Extraer unidades y tipos de precio (usando la línea original o el nombre_prod) ---
    unidad_final, tipo_precio_final = extraer_unidades_y_tipos_precio(linea_limpia, pyme_rubro_nombre)
    
    # Limpiar el nombre del producto de unidades si ya se extrajeron por separado
    if unidad_final and nombre_prod:
        nombre_prod = limpiar_texto_base(nombre_prod.replace(unidad_final, "")) # Simple replace, puede necesitar regex
        # También podrías querer quitarlo de la descripción si la armas diferente


    # --- 4. Heurísticas de descarte ---
    if not nombre_prod or len(nombre_prod) < 4: # Nombre muy corto
        return None
    
    # Descartar si parece un encabezado muy obvio (incluso si se encontró un "precio" como un año "2024")
    palabras_encabezado_comunes = ["ARTICULO", "CODIGO", "DESCRIPCION", "TALLE", "PRECIO", "MARCA", "LINEA", "TOTAL", "SUBTOTAL", "CANTIDAD", "LISTA DE PRECIOS", "COLECCIÓN"]
    nombre_prod_upper = nombre_prod.upper()
    es_encabezado = any(palabra.upper() in nombre_prod_upper for palabra in palabras_encabezado_comunes)
    
    if es_encabezado and precio_float_final is None and len(nombre_prod.split()) < 6:
        logging.debug(f"Línea descartada (posible encabezado sin precio claro): '{linea_limpia}'")
        return None
    # Si no hay precio y el nombre es corto o parece un número, descartar
    if precio_float_final is None and (len(nombre_prod) < 10 or nombre_prod.isdigit() or re.fullmatch(r"ART\.?\s*\w+", nombre_prod_upper)):
        logging.debug(f"Línea descartada (nombre corto/código sin precio): '{linea_limpia}' -> '{nombre_prod}'")
        return None

    # --- 5. Construir el diccionario del producto ---
    producto = {
        "user_id": pyme_user_id if pyme_user_id is not None else 0,
        "nombre": nombre_prod[:250], # Limitar longitud
        "descripcion": desc_prod[:500] if desc_prod and desc_prod != nombre_prod else nombre_prod[:500],
        "precio_str": precio_str_final if precio_str_final else "",
        "precio_float": precio_float_final,
        "moneda": moneda_final,
        "unidad": unidad_final if unidad_final else "",
        "tipo_precio": tipo_precio_final if tipo_precio_final else "",
    }
    producto["texto_para_embedding"] = f"{producto['nombre']} {producto['descripcion']} {producto['unidad']} {producto['tipo_precio']} {producto['precio_str']}".strip()
    
    logging.debug(f"Producto procesado: {producto}")
    return producto


def procesar_catalogo_pdf_google(pdf_path, pyme_user_id=None, pyme_rubro_nombre="generico"):
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID", "ambient-stack-461118-k7")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us")
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID", "55c57b09a179531a")

        client = documentai.DocumentProcessorServiceClient(credentials=credentials)
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        # Considerar habilitar table extraction si tu procesador lo soporta
        # process_options = documentai.ProcessOptions(
        #     ocr_config=documentai.OcrConfig(
        #         enable_native_pdf_parsing=True, # Opcional, puede mejorar algunos PDFs
        #         # hints=documentai.OcrConfig.Hints(language_hints=["es"]) # Opcional
        #     ),
        #     # Si tu procesador es de tipo "FORM_PARSER" o uno que extraiga tablas:
        #     # layout_config=documentai.ProcessOptions.LayoutConfig(
        #     #     chunking_config=documentai.ProcessOptions.LayoutConfig.ChunkingConfig(
        #     #         chunk_size=1000, # ajusta según necesidad
        #     #         include_ancestor_headings=True
        #     #     )
        #     # )
        # )
        # request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document, process_options=process_options)
        request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document) # Tu llamada original
        
        result = client.process_document(request=request)
        document = result.document
        
        logging.info(f"📝 Documento '{os.path.basename(pdf_path)}' (PYME ID {pyme_user_id}) procesado: {len(document.pages)} pág. Rubro: {pyme_rubro_nombre}. Iniciando extracción...")
        productos_extraidos_final = []

        # --- Intento 1: Usar ANÁLISIS DE TABLAS de Document AI (LA MEJOR OPCIÓN PARA LISTAS DE PRECIOS) ---
        if document.tables:
            logging.info(f"✅ Se encontraron {len(document.tables)} tablas. Intentando procesarlas...")
            
            def get_text_from_layout_segments(text_segments, full_doc_text):
                content = ""
                if text_segments:
                    for segment in text_segments:
                        content += full_doc_text[int(segment.start_index):int(segment.end_index)]
                return limpiar_texto_base(content)

            for table_idx, table in enumerate(document.tables):
                logging.debug(f"Procesando Tabla {table_idx + 1}/{len(document.tables)}")
                header_cells_text = []
                if table.header_rows:
                    for header_row in table.header_rows:
                        for cell in header_row.cells:
                            header_cells_text.append(get_text_from_layout_segments(cell.layout.text_anchor.text_segments, document.text))
                logging.debug(f"  Encabezados de tabla detectados: {header_cells_text}")

                # --- Lógica para mapear encabezados a tus campos (MUY IMPORTANTE Y DEPENDIENTE DEL PDF) ---
                # Ejemplo de mapeo (necesitarás hacerlo mucho más robusto e inteligente):
                col_map = {}
                for idx, header_text in enumerate(header_cells_text):
                    ht_lower = header_text.lower()
                    if "art" in ht_lower or "cod" in ht_lower or "sku" in ht_lower : col_map["codigo_articulo"] = idx
                    elif "nombre" in ht_lower or "producto" in ht_lower or "descrip" in ht_lower or "varietal" in ht_lower: col_map["nombre"] = idx
                    elif "precio" in ht_lower or "valor" in ht_lower: col_map["precio"] = idx
                    elif "unidad" in ht_lower or "presentacion" in ht_lower or "caja" in ht_lower : col_map["unidad"] = idx
                    # Añade más mapeos para "descripción", "categoría", etc.

                if not col_map.get("nombre") or not col_map.get("precio"):
                    logging.warning(f"  Tabla {table_idx + 1}: No se pudieron mapear columnas cruciales de nombre/precio. Saltando esta tabla para procesamiento tabular.")
                    # Podrías decidir procesar esta tabla línea por línea si el mapeo de columnas falla.
                    # O simplemente continuar con la siguiente tabla.
                    # continue 
                    pass # Permitir que caiga en el procesamiento línea por línea si es necesario.


                for row_idx, body_row in enumerate(table.body_rows):
                    celdas = body_row.cells
                    try:
                        nombre_val = get_text_from_layout_segments(celdas[col_map["nombre"]].layout.text_anchor.text_segments, document.text) if "nombre" in col_map and col_map["nombre"] < len(celdas) else ""
                        precio_val = get_text_from_layout_segments(celdas[col_map["precio"]].layout.text_anchor.text_segments, document.text) if "precio" in col_map and col_map["precio"] < len(celdas) else ""
                        unidad_val = get_text_from_layout_segments(celdas[col_map.get("unidad", -1)].layout.text_anchor.text_segments, document.text) if "unidad" in col_map and col_map.get("unidad", -1) < len(celdas) else ""
                        codigo_val = get_text_from_layout_segments(celdas[col_map.get("codigo_articulo", -1)].layout.text_anchor.text_segments, document.text) if "codigo_articulo" in col_map and col_map.get("codigo_articulo",-1) < len(celdas) else ""
                        
                        if not nombre_val and codigo_val: nombre_val = codigo_val # Si no hay nombre pero sí código, usar código
                        if not nombre_val or len(nombre_val) < 3 : continue # Saltar si no hay nombre o es muy corto

                        p_str, p_float, p_moneda = parse_precio_flexible(precio_val)

                        # Si el nombre contiene el precio (a veces pasa en celdas fusionadas), intentar limpiarlo
                        if p_str and p_str in nombre_val:
                            nombre_val = limpiar_texto_base(nombre_val.replace(p_str, ""))

                        producto_tabla = {
                            "user_id": pyme_user_id if pyme_user_id is not None else 0,
                            "nombre": nombre_val[:250],
                            "descripcion": nombre_val[:500], # Por ahora, o tomar de otra columna
                            "precio_str": p_str if p_str else "",
                            "precio_float": p_float,
                            "moneda": p_moneda if p_moneda else "ARS",
                            "unidad": unidad_val[:100] if unidad_val else "",
                            "codigo_articulo": codigo_val[:100] if codigo_val else "",
                            "tipo_precio": "", # Podrías inferirlo de la tabla o contexto
                            "texto_para_embedding": f"{nombre_val} {unidad_val if unidad_val else ''} {p_str if p_str else ''}".strip()
                        }
                        productos_extraidos_final.append(producto_tabla)
                        logging.debug(f"  De Tabla {table_idx+1}, Fila {row_idx+1}: {producto_tabla}")
                    except IndexError:
                        logging.warning(f"  Error de índice procesando fila {row_idx} de tabla {table_idx+1}. Verificar mapeo de columnas y estructura de tabla.")
                    except Exception as e_row_table:
                         logging.error(f"  Error procesando fila {row_idx} de tabla {table_idx+1}: {e_row_table}")
            logging.info(f"Productos extraídos de tablas: {len(productos_extraidos_final)}")


        # --- Fallback o Procesamiento Adicional Línea por Línea del Texto Completo ---
        # Si el procesamiento de tablas no extrajo nada, O si quieres complementarlo.
        # (Podrías decidir no ejecutar esto si la extracción de tablas fue exitosa)
        if not productos_extraidos_final or len(productos_extraidos_final) < (len(document.text.splitlines()) / 10): # Heurística: si hay muy pocos productos de tablas
            logging.info("Pocos productos de tablas o ninguno. Iniciando/complementando con procesamiento línea por línea del texto completo...")
            
            # Usar la función refactorizada extraer_info_producto_de_linea
            # que definimos antes para procesar cada línea del documento.
            # Iterar por page.lines puede ser más granular que document.text.split('\n')
            # porque page.lines viene del layout analysis de Document AI.
            lineas_procesadas_como_texto = 0
            for page in document.pages:
                for line_obj in page.lines:
                    line_text = ""
                    if line_obj.layout.text_anchor and line_obj.layout.text_anchor.text_segments:
                        for segment in line_obj.layout.text_anchor.text_segments:
                            line_text += document.text[segment.start_index:segment.end_index]
                    
                    producto_linea = extraer_info_producto_de_linea(line_text, pyme_rubro_nombre)
                    if producto_linea:
                        producto_linea["user_id"] = pyme_user_id if pyme_user_id is not None else 0
                        productos_extraidos_final.append(producto_linea)
                        lineas_procesadas_como_texto +=1
            logging.info(f"Productos añadidos/encontrados por procesamiento línea por línea: {lineas_procesadas_como_texto}")


        logging.info(f"Total de productos extraídos (final): {len(productos_extraidos_final)}")
        if not productos_extraidos_final:
            logging.warning(f"⚠️ No se extrajo ningún producto del PDF: {os.path.basename(pdf_path)}")
        
        return productos_extraidos_final

    except Exception as e:
        logging.error(f"❌ Error fatal en procesar_catalogo_pdf_google ({os.path.basename(pdf_path)}): {e}", exc_info=True)
        return []