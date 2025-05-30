# En tu archivo: services/google_docai.py

import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account
# from price_parser import Price # Descomenta si decides usar price-parser

# --- Carga de Credenciales (igual que antes) ---
# ... (tu código de carga de credenciales) ...
if 'google.oauth2' not in globals() or 'service_account' not in globals()['google.oauth2'].__dict__:
    logging.warning("google.oauth2.service_account no parece estar disponible globalmente como se esperaba.")
try:
    ruta_render = "/etc/secrets/google_service_key.json"
    ruta_local = "instance/google-credentials.json" # Ajusta si tu ruta local es diferente
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
    Devuelve (precio_str_original_limpio_del_numero, precio_float, moneda_detectada).
    """
    if not texto_precio:
        return None, None, None

    texto_limpio_original = limpiar_texto_base(texto_precio) # El texto original del precio encontrado
    moneda = "ARS" 
    numero_str_extraido = texto_limpio_original # El string que intentaremos convertir a float

    # Detectar y quitar símbolos de moneda, actualizando 'moneda' y 'numero_str_extraido'
    # Esta regex busca símbolos al principio o al final, y también palabras como USD/ARS
    match_moneda = re.match(r"^((?i)\$|USD|ARS)?\s*(.*?)\s*((?i)USD|ARS|\$)?$", texto_limpio_original)
    if match_moneda:
        simbolo_izq = match_moneda.group(1)
        contenido_central = match_moneda.group(2)
        simbolo_der = match_moneda.group(3)

        if simbolo_izq:
            if "USD" in simbolo_izq.upper(): moneda = "USD"
            elif "ARS" in simbolo_izq.upper(): moneda = "ARS"
        if simbolo_der: # El sufijo tiene prioridad si ambos existen y son diferentes
            if "USD" in simbolo_der.upper(): moneda = "USD"
            elif "ARS" in simbolo_der.upper(): moneda = "ARS"
        
        numero_str_extraido = limpiar_texto_base(contenido_central) # El número sin símbolos de moneda
    
    # Normalizar separadores numéricos del 'numero_str_extraido'
    precio_normalizado_para_float = numero_str_extraido
    if ',' in numero_str_extraido and '.' in numero_str_extraido:
        if numero_str_extraido.rfind(',') > numero_str_extraido.rfind('.'): # Formato ARS: 1.234,56
            precio_normalizado_para_float = numero_str_extraido.replace('.', '').replace(',', '.')
        else: # Formato USA: 1,234.56
            precio_normalizado_para_float = numero_str_extraido.replace(',', '')
    elif ',' in numero_str_extraido: # Solo comas, asumir formato ARS decimal: 1234,56
        precio_normalizado_para_float = numero_str_extraido.replace(',', '.')
    
    try:
        precio_flt = float(precio_normalizado_para_float)
        # Devolvemos el 'numero_str_extraido' (que es el número sin símbolos de moneda pero con su formato original de puntos/comas)
        # y el 'precio_flt' (el número puro para cálculos)
        return numero_str_extraido, precio_flt, moneda
    except ValueError:
        logging.debug(f"No se pudo convertir precio a float: '{numero_str_extraido}' (de '{texto_precio}')")
        # Si no se puede convertir a float, pero parece un número, devolvemos el string y None para float
        if re.fullmatch(r"[\d.,]+", numero_str_extraido):
             return numero_str_extraido, None, moneda
        return None, None, moneda


def extraer_unidades_y_tipos_precio(texto_linea: str, rubro: str = "generico") -> tuple[str | None, str | None]:
    texto_linea_lower = texto_linea.lower()
    unidad = None
    tipo_precio = None

    # Patrones comunes de unidades (más específicos primero)
    # Considerar el contexto del rubro para listas de palabras clave más específicas si es necesario
    unidades_patrones = [
        (r"\b(caja(?:s)?\s*x\s*\d{1,2})\b", "caja_especifica"), # "caja x6", "cajas x12"
        (r"\b(pack\s*x\s*\d{1,2})\b", "pack_especifico"),   # "pack x4"
        (r"\b(\d{3,4}\s*ml)\b", "volumen_ml"),            # "750ml", "1000 ml"
        (r"\b(\d{1,2}\s*lts?)\b", "volumen_lts"),           # "1 lts", "1.5 lt"
        (r"\b(docena(?:s)?)\b", "docena"),
        (r"\b(par(?:es)?)\b", "par"),
        (r"\b(kilo(?:s)?|kg)\b", "kilo"),
        (r"\b(gr(?:s)?|gramo(?:s)?)\b", "gramo"),
        (r"\b(caja)\b", "caja_generica"), # Menos específico, después de "caja xN"
        (r"\b(botella)\b", "botella_generica"),
        (r"\b(pack)\b", "pack_generico"),
        (r"\b(unidad|unid|u\.?)\b", "unidad")
    ]
    
    tipos_precio_keywords = {
        "mayorista": ["mayorista", "por mayor", "distribuidor", "dist."],
        "minorista": ["minorista", "al detalle", "público", "consumidor final"],
        "promocion": ["promo", "oferta", "descuento", "especial", "sale"]
    }

    for patron, tipo_unidad_match in unidades_patrones:
        match = re.search(patron, texto_linea_lower)
        if match:
            unidad = limpiar_texto_base(match.group(1))
            # Podrías quitar la unidad del texto_linea_lower para evitar que se tome como nombre después
            # texto_linea_lower = texto_linea_lower.replace(match.group(0), "").strip() # Cuidado con esto
            break 

    for tipo, keywords in tipos_precio_keywords.items():
        if any(kw in texto_linea_lower for kw in keywords):
            tipo_precio = tipo
            break
            
    return unidad, tipo_precio


def extraer_info_producto_de_linea(linea_procesar: str, pyme_rubro_nombre: str) -> dict | None:
    linea_limpia = limpiar_texto_base(linea_procesar)
    if not linea_limpia or len(linea_limpia) < 5:
        return None

    # Regex general para precios (intenta ser robusta)
    # Grupo 1: el string numérico del precio (ej. "1.250,00" o "1250.00")
    # Grupo 2: (opcional) la moneda detectada por palabra clave (ARS, USD) o símbolo $
    # Esta regex busca el precio como un todo, incluyendo el símbolo
    precio_regex_captura_completa = r"((?:\$\s*|\bARS\s*|\bUSD\s*)?[\s\d]*[\d.,]+[\d.,\s]*(?:\s*\$|\s*\bARS\b|\s*\bUSD\b)?)"
    
    # Palabras clave que usualmente NO son parte del nombre del producto si están al final o separadas
    palabras_a_separar = ["precio", "oferta", "promo", "contado", "efectivo", "unidad", "caja", "docena", "mayorista"]
    
    candidatos_precio_info = [] # (texto_del_precio_encontrado, inicio_span, fin_span)
    for match_p in re.finditer(precio_regex_captura_completa, linea_limpia):
        texto_precio_crudo = match_p.group(1)
        # Validar que el texto_precio_crudo realmente contenga dígitos
        if re.search(r"\d", texto_precio_crudo):
             candidatos_precio_info.append((texto_precio_crudo, match_p.start(), match_p.end()))

    nombre_prod_final = linea_limpia # Default
    desc_prod_final = ""
    precio_str_final = ""
    precio_float_final = None
    moneda_final = "ARS"
    unidad_final = None
    tipo_precio_final = None

    if candidatos_precio_info:
        # Heurística: Tomar el último candidato a precio como el más probable.
        # Podrías refinar esto para tomar el "más grande" o el que tenga formato más claro.
        texto_precio_elegido, inicio_precio, fin_precio = candidatos_precio_info[-1]
        
        # Parsear el precio elegido
        precio_str_parseado, precio_flt_parseado, moneda_parseada = parse_precio_flexible(texto_precio_elegido)

        if precio_flt_parseado is not None: # Si se pudo convertir a float, es un buen candidato
            precio_str_final = precio_str_parseado
            precio_float_final = precio_flt_parseado
            moneda_final = moneda_parseada if moneda_parseada else "ARS"
            
            # Intentar aislar el nombre: tomar el texto a la izquierda del precio
            nombre_prod_final = limpiar_texto_base(linea_limpia[:inicio_precio])
            
            # Si el nombre quedó vacío, podría ser que el precio estuviera al principio,
            # o que la línea fuera solo un precio.
            if not nombre_prod_final and len(linea_limpia) > len(texto_precio_elegido):
                nombre_prod_final = limpiar_texto_base(linea_limpia[fin_precio:]) # Intentar tomar lo de la derecha
            
            if not nombre_prod_final: # Si aún es vacío, es probable que la línea sea solo el precio
                logging.debug(f"Línea parece ser solo un precio y se descarta como producto: '{linea_limpia}'")
                return None
        else:
            # Si el mejor candidato a precio no pudo convertirse a float, la línea es ambigua.
            # Podríamos no asignar precio y tomar toda la línea como nombre/descripción.
            nombre_prod_final = linea_limpia # Tomar toda la línea como nombre
            logging.debug(f"Precio ambiguo o no convertible en: '{linea_limpia}'. Texto precio: '{texto_precio_elegido}'")
    else:
        # No se encontraron patrones de precio claros. La línea es probablemente nombre/descripción/encabezado.
        nombre_prod_final = linea_limpia
        # logging.debug(f"No se encontró precio con regex detallada en: '{linea_limpia}'")

    # Intentar extraer unidades y tipo de precio de la línea original (o del nombre_prod si es más limpio)
    texto_para_unidades = nombre_prod_final if len(nombre_prod_final) < len(linea_limpia) / 1.5 else linea_limpia
    unidad_ext, tipo_precio_ext = extraer_unidades_y_tipos_precio(texto_para_unidades, pyme_rubro_nombre)
    unidad_final = unidad_ext if unidad_ext else ""
    tipo_precio_final = tipo_precio_ext if tipo_precio_ext else ""

    # Limpiar el nombre del producto de las unidades/tipos si se extrajeron y estaban incluidas
    if unidad_final and nombre_prod_final and unidad_final.lower() in nombre_prod_final.lower():
        # Usar regex para quitar la unidad de forma más segura (case insensitive)
        nombre_prod_final = limpiar_texto_base(re.sub(r'(?i)' + re.escape(unidad_final), '', nombre_prod_final))
    if tipo_precio_final and nombre_prod_final and tipo_precio_final.lower() in nombre_prod_final.lower():
        nombre_prod_final = limpiar_texto_base(re.sub(r'(?i)' + re.escape(tipo_precio_final), '', nombre_prod_final))
    
    # Quitar códigos de artículo comunes del nombre si aún están
    codigo_pattern = r"\b(?:ART|COD|REF|SKU)\.?\s*[\w\d/-]+"
    match_codigo = re.match(codigo_pattern, nombre_prod_final, re.IGNORECASE)
    codigo_articulo_extraido = ""
    if match_codigo:
        codigo_articulo_extraido = limpiar_texto_base(match_codigo.group(0))
        nombre_prod_final = limpiar_texto_base(nombre_prod_final.replace(codigo_articulo_extraido, ""))


    # Asignar descripción: si el nombre es ahora diferente de la línea original (porque quitamos precio/código),
    # la descripción podría ser el nombre más completo, o el nombre actual si es suficientemente descriptivo.
    # Esto es muy heurístico.
    if limpiar_texto_base(nombre_prod_final) != linea_limpia and len(nombre_prod_final) > 3 :
        desc_prod_final = nombre_prod_final # O una versión más completa si la tienes.
    else: # Si el nombre es la línea completa (o muy similar) o muy corto, no hay descripción separada.
        desc_prod_final = "" # Dejar descripción vacía para no ser redundante con el nombre


    # --- Filtros finales de calidad ---
    if not nombre_prod_final or len(nombre_prod_final) < 4:
        logging.debug(f"Descarte final (nombre muy corto): '{linea_limpia}' -> '{nombre_prod_final}'")
        return None

    # Descartar si parece un encabezado (incluso si se encontró un "precio" como "2024")
    # Esta heurística es la que puede mejorarse mucho con el análisis de tablas de Document AI.
    palabras_encabezado_comunes = ["ARTICULO", "DESCRIPCION", "TALLE", "PRECIO", "MARCA", "LINEA", "TOTAL", "CODIGO", "IMPORTE", "CANTIDAD", "LISTA DE PRECIOS", "COLECCIÓN", "RUBRO"]
    # Si la línea contiene MUCHAS de estas palabras, y NO tiene un precio float válido, es probable un encabezado
    count_palabras_header = sum(1 for palabra in palabras_encabezado_comunes if palabra in nombre_prod_final.upper())
    if count_palabras_header >= 2 and precio_float_final is None and len(nombre_prod_final.split()) <= len(palabras_encabezado_comunes) + 2 :
        logging.debug(f"Línea descartada (posible encabezado complejo sin precio claro): '{linea_limpia}'")
        return None
    # Si no tiene precio y el nombre es corto o solo un código.
    if precio_float_final is None and (len(nombre_prod_final) < 10 or re.fullmatch(r"ART\.?\s*\w+", nombre_prod_final.upper())):
        logging.debug(f"Línea descartada (nombre corto/código sin precio): '{linea_limpia}' -> '{nombre_prod_final}'")
        return None
    # Si el "nombre" es idéntico a un patrón de precio que no se pudo convertir a float.
    if precio_float_final is None and parse_precio_flexible(nombre_prod_final)[0] == nombre_prod_final:
        logging.debug(f"Línea descartada (nombre es solo un string de precio no convertible): '{linea_limpia}'")
        return None

    producto_final = {
        "user_id": 0, # Se asignará en la función principal
        "nombre": nombre_prod_final[:250],
        "descripcion": desc_prod_final[:500] if desc_prod_final else nombre_prod_final[:500],
        "precio_str": precio_str_final,
        "precio_float": precio_float_final,
        "moneda": moneda_final,
        "unidad": unidad_final,
        "tipo_precio": tipo_precio_final,
        "codigo_articulo": codigo_articulo_extraido[:100] if codigo_articulo_extraido else "",
    }
    producto_final["texto_para_embedding"] = f"{producto_final['nombre']} {producto_final['codigo_articulo']} {producto_final['descripcion']} {producto_final['unidad']} {producto_final['tipo_precio']} {producto_final['precio_str']}".strip()
    
    # Logging del producto final extraído de la línea
    # logging.info(f"    Línea Procesada: '{linea_limpia}' -> EXTRAÍDO: {producto_final}")
    return producto_final


def procesar_catalogo_pdf_google(pdf_path, pyme_user_id=None, pyme_rubro_nombre="generico"):
    try:
        # ... (código de inicialización de Document AI y carga del documento - sin cambios) ...
        project_id = os.getenv("GOOGLE_PROJECT_ID", "ambient-stack-461118-k7")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us")
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID", "55c57b09a179531a")

        client = documentai.DocumentProcessorServiceClient(credentials=credentials)
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document)
        result = client.process_document(request=request)
        document = result.document

        logging.info(f"📝 Documento '{os.path.basename(pdf_path)}' (PYME ID {pyme_user_id}) procesado: {len(document.pages)} pág. Rubro: {pyme_rubro_nombre}. Iniciando extracción...")
        productos_extraidos_final = []

        # --- Opción 1 (RECOMENDADA): Implementar lógica de extracción de tablas de Document AI aquí ---
        # if document.tables:
        #     logging.info(f"✅ Se encontraron {len(document.tables)} tablas. Procesándolas...")
        #     # ... (tu lógica detallada para iterar tablas, filas, celdas, mapear encabezados, etc.) ...
        #     # esta lógica llenaría 'productos_extraidos_final'
        # else:
        #     logging.info("No se detectaron tablas estructuradas, se procederá con análisis línea por línea.")

        # --- Opción 2 (Fallback o si no hay tablas): Procesamiento línea por línea del texto completo ---
        # (Si usas tablas, esta sección podría ser un fallback si la extracción de tablas no da resultados,
        # o podrías omitirla si confías en las tablas)

        lineas_utilizadas_para_productos = set() # Para evitar procesar la misma línea de texto varias veces si page.lines y document.text se superponen.

        for page_num, page in enumerate(document.pages):
            for line_obj in page.lines: # Document AI 'line' es un bloque de texto detectado
                line_text_completo = ""
                if line_obj.layout.text_anchor and line_obj.layout.text_anchor.text_segments:
                    for segment in line_obj.layout.text_anchor.text_segments:
                        # Construir el texto de la línea usando los segmentos y el texto completo del documento
                        line_text_completo += document.text[int(segment.start_index):int(segment.end_index)]
                
                line_text_limpia = limpiar_texto_base(line_text_completo)
                
                # Evitar procesar la misma línea de texto si ya se obtuvo de otra manera
                # (esto es una heurística, la segmentación de Document AI es compleja)
                # if line_text_limpia in lineas_utilizadas_para_productos:
                #     continue
                # lineas_utilizadas_para_productos.add(line_text_limpia)

                if line_text_limpia:
                    producto_candidato = extraer_info_producto_de_linea(line_text_limpia, pyme_rubro_nombre)
                    if producto_candidato:
                        producto_candidato["user_id"] = pyme_user_id if pyme_user_id is not None else 0
                        productos_extraidos_final.append(producto_candidato)
        
        if not productos_extraidos_final: # Si el bucle por page.lines no dio nada, intentar con todo el texto
            logging.info("Procesamiento por page.lines no extrajo productos, intentando con document.text completo línea por línea.")
            lineas_doc_completo = document.text.split('\n')
            for linea_cruda in lineas_doc_completo:
                producto_candidato = extraer_info_producto_de_linea(linea_cruda, pyme_rubro_nombre)
                if producto_candidato:
                    producto_candidato["user_id"] = pyme_user_id if pyme_user_id is not None else 0
                    productos_extraidos_final.append(producto_candidato)


        logging.info(f"Total de productos extraídos (final) para '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
        if not productos_extraidos_final:
            logging.warning(f"⚠️ No se pudo extraer ningún producto estructurado del PDF: {os.path.basename(pdf_path)}")
        
        return productos_extraidos_final

    except Exception as e:
        logging.error(f"❌ Error fatal en procesar_catalogo_pdf_google ({os.path.basename(pdf_path)}): {e}", exc_info=True)
        return []