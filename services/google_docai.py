# En tu archivo: services/google_docai.py

import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account
# Importar funciones de utilidad desde tu archivo utils.py en la misma carpeta 'services'
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio

# --- Carga de Credenciales ---
# (Tu código de carga de credenciales se mantiene igual)
# ...
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

def _obtener_documento_ai(pdf_path: str) -> documentai.Document | None:
    """
    Función interna para enviar el PDF a Document AI y obtener el objeto Document.
    """
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID", "ambient-stack-461118-k7")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us")
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID", "55c57b09a179531a")

        client = documentai.DocumentProcessorServiceClient(credentials=credentials)
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        
        # Aquí puedes configurar process_options si quieres habilitar específicamente
        # la extracción de tablas o otras características avanzadas del procesador.
        # Ejemplo para un procesador que soporte extracción de tablas:
        # process_options = documentai.ProcessOptions(
        #     from_start=True, # Asegura procesar desde el inicio del documento
        #     # Habilitar específicamente la extracción de entidades si tu procesador lo soporta y es útil
        #     # entity_extraction_config=documentai.ProcessOptions.EntityExtractionConfig(enabled=True),
        #     # Habilitar específicamente la extracción de tablas
        #     # table_extraction_config=documentai.ProcessOptions.TableExtractionConfig(enabled=True)
        # )
        # request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document, process_options=process_options)
        
        # Tu llamada original (sin process_options explícitos, usa los defaults del procesador)
        request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document)
        
        result = client.process_document(request=request)
        return result.document
    except Exception as e:
        logging.error(f"❌ Error en la llamada a la API de Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> dict | None:
    """
    Intenta extraer nombre, precio, unidad, etc., de una sola línea de texto de un PDF.
    Esta es la función que refina la extracción línea por línea usando heurísticas.
    """
    linea_limpia = limpiar_texto_base(linea_procesar)
    if not linea_limpia or len(linea_limpia) < 5: # Umbral mínimo de longitud
        return None

    # Regex para precios y códigos (puedes seguir refinándolas)
    precio_regex_captura_completa = r"((?:\$\s*|\bARS\s*|\bUSD\s*)?[\s\d]*[\d.,]+[\d.,\s]*(?:\s*\$|\s*\bARS\b|\s*\bUSD\b)?)"
    codigo_pattern = re.compile(r"(\b(?:ART|COD|REF|SKU|ID)\.?\s*[\w\d/-]+)", re.IGNORECASE)
    
    candidatos_precio_info = []
    for match_p in re.finditer(precio_regex_captura_completa, linea_limpia):
        texto_precio_encontrado = match_p.group(1)
        if re.search(r"\d", texto_precio_encontrado): # Asegurar que el match contenga dígitos
             candidatos_precio_info.append((texto_precio_encontrado, match_p.start(), match_p.end()))

    nombre_prod_final = linea_limpia # Default
    precio_str_final = ""
    precio_float_final = None
    moneda_final = "ARS" # Default
    unidad_final = ""
    tipo_precio_final = ""
    codigo_articulo_extraido = ""
    descripcion_final = "" # Iniciar descripción vacía

    if candidatos_precio_info:
        # Heurística: Tomar el último precio encontrado como el más probable
        texto_precio_elegido, inicio_precio, fin_precio = candidatos_precio_info[-1]
        
        # Usar parse_precio_flexible de utils.py
        precio_str_parseado, precio_flt_parseado, moneda_parseada = parse_precio_flexible(texto_precio_elegido)

        if precio_flt_parseado is not None: # Solo si el precio parseado es un número válido
            precio_str_final = precio_str_parseado if precio_str_parseado else ""
            precio_float_final = precio_flt_parseado # Corregido: usar la variable correcta
            moneda_final = moneda_parseada if moneda_parseada else "ARS"
            
            # Intentar aislar el nombre del producto: texto a la izquierda del precio
            nombre_prod_final = limpiar_texto_base(linea_limpia[:inicio_precio])
            
            # Si el nombre quedó vacío (precio al inicio) o es muy corto, intentar con el texto a la derecha
            if (not nombre_prod_final or len(nombre_prod_final) < 3) and len(linea_limpia) > fin_precio:
                texto_derecha_precio = limpiar_texto_base(linea_limpia[fin_precio:])
                if len(texto_derecha_precio) > len(nombre_prod_final) : # Si hay más texto a la derecha
                    nombre_prod_final = texto_derecha_precio 
            
            if not nombre_prod_final: # Si aún así no hay nombre
                logging.debug(f"[DOCAI] Línea '{linea_limpia}' parece ser solo un precio o nombre no extraíble.")
                return None # Descartar si no se puede aislar un nombre junto al precio
        else:
            # Si el mejor candidato a precio no pudo convertirse a float, la línea es ambigua.
            # No se asigna precio, y se toma la línea como nombre (se filtrará más adelante si no es válido).
            nombre_prod_final = linea_limpia
            logging.debug(f"[DOCAI] Precio ambiguo o no convertible en: '{linea_limpia}'. Texto precio crudo: '{texto_precio_elegido}'")
    else:
        # No se encontraron precios con la regex. La línea es probablemente nombre/descripción o un encabezado.
        nombre_prod_final = linea_limpia
    
    # Extraer unidades y tipos de precio del nombre_prod_final o de la línea original (para más contexto)
    texto_base_para_unidades = nombre_prod_final if len(nombre_prod_final) > 10 else linea_limpia # Usar línea original si nombre_prod es corto
    unidad_ext, tipo_precio_ext = extraer_unidades_y_tipos_precio(texto_base_para_unidades, pyme_rubro_nombre)
    unidad_final = unidad_ext if unidad_ext else ""
    tipo_precio_final = tipo_precio_ext if tipo_precio_ext else ""

    # Limpiar el nombre del producto de las unidades/tipos y códigos
    texto_para_nombre = nombre_prod_final
    if unidad_final and unidad_final.lower() in texto_para_nombre.lower():
        texto_para_nombre = limpiar_texto_base(re.sub(r'(?i)' + re.escape(unidad_final), '', texto_para_nombre))
    if tipo_precio_final and tipo_precio_final.lower() in texto_para_nombre.lower():
        texto_para_nombre = limpiar_texto_base(re.sub(r'(?i)' + re.escape(tipo_precio_final), '', texto_para_nombre))
    
    match_codigo = codigo_pattern.search(texto_para_nombre)
    if match_codigo:
        codigo_articulo_extraido = limpiar_texto_base(match_codigo.group(0))
        texto_para_nombre = limpiar_texto_base(texto_para_nombre.replace(codigo_articulo_extraido, ""))
    
    nombre_prod_final = limpiar_texto_base(texto_para_nombre)

    # Asignar descripción
    # Si el nombre final es significativamente más corto que la línea limpia original (después de quitar precios),
    # la línea original (sin el precio) podría ser una buena descripción.
    # Esto es muy heurístico.
    if len(nombre_prod_final) < len(linea_limpia) * 0.7 and precio_str_final : # Si se quitó bastante texto (probablemente el precio)
        descripcion_final = limpiar_texto_base(linea_limpia.replace(precio_str_final, ''))
        if descripcion_final == nombre_prod_final : descripcion_final = "" # Evitar redundancia
    elif nombre_prod_final != linea_limpia :
         descripcion_final = nombre_prod_final # Si el nombre se limpió pero no drásticamente
    else:
        descripcion_final = ""


    # --- Filtros finales de calidad ---
    if not nombre_prod_final or len(nombre_prod_final) < 3:
        logging.debug(f"[DOCAI] Descarte final (nombre muy corto): '{linea_limpia}' -> '{nombre_prod_final}'")
        return None

    palabras_encabezado_comunes = ["ARTICULO", "DESCRIPCION", "MODELO", "TALLE", "PRECIO", "MARCA", "LINEA", "TOTAL", "CODIGO", "IMPORTE", "CANTIDAD", "LISTA DE PRECIOS", "COLECCIÓN", "RUBRO", "CLIENTE", "FECHA", "PAGINA", "CONTACTO"]
    nombre_prod_upper = nombre_prod_final.upper()
    count_palabras_header = sum(1 for palabra in palabras_encabezado_comunes if palabra in nombre_prod_upper)
    
    # Descartar si es muy probable que sea un encabezado o una línea de información general
    if precio_float_final is None:
        if count_palabras_header >= 1 and len(nombre_prod_final.split()) <= 5: # Contiene palabra clave y es corta
            logging.debug(f"[DOCAI] Descarte (posible encabezado/info general sin precio): '{linea_limpia}' -> '{nombre_prod_final}'")
            return None
        if len(nombre_prod_final) < 10 or nombre_prod_final.isdigit() or re.fullmatch(r"ART\.?\s*\w+", nombre_prod_upper):
             logging.debug(f"[DOCAI] Descarte (nombre corto, código o número sin precio): '{linea_limpia}' -> '{nombre_prod_final}'")
             return None
    # Descartar si el nombre parece ser solo un precio no convertido
    if parse_precio_flexible(nombre_prod_final)[1] is not None and precio_float_final is None:
         logging.debug(f"[DOCAI] Descarte (nombre parece ser solo un precio): '{linea_limpia}' -> '{nombre_prod_final}'")
         return None


    producto_final_dict = {
        "nombre": nombre_prod_final[:250],
        "descripcion": descripcion_final[:500] if descripcion_final else nombre_prod_final[:500],
        "precio_str": precio_str_final,
        "precio_float": precio_float_final,
        "moneda": moneda_final,
        "unidad": unidad_final,
        "tipo_precio": tipo_precio_final,
        "codigo_articulo": codigo_articulo_extraido[:100],
    }
    producto_final_dict["texto_para_embedding"] = f"{producto_final_dict['nombre']} {producto_final_dict['codigo_articulo']} {producto_final_dict['descripcion']} {producto_final_dict['unidad']} {producto_final_dict['tipo_precio']} {producto_final_dict['precio_str']}".strip()
    
    logging.debug(f"[DOCAI] Producto extraído de línea: {producto_final_dict}")
    return producto_final_dict


def procesar_catalogo_pdf_google(pdf_path, pyme_user_id=None, pyme_rubro_nombre="generico"):
    """
    Función principal para procesar un catálogo PDF.
    Intenta primero extraer de tablas, y si no, procesa línea por línea.
    """
    try:
        document = _obtener_documento_ai(pdf_path)
        if not document:
            logging.error(f"No se pudo obtener el objeto Document de Document AI para {os.path.basename(pdf_path)}")
            return []
        
        logging.info(f"📝 Documento '{os.path.basename(pdf_path)}' (PYME ID {pyme_user_id}) procesado: {len(document.pages)} pág. Rubro: {pyme_rubro_nombre}. Iniciando extracción...")
        productos_extraidos_final = []

        # --- Intento 1: Usar ANÁLISIS DE TABLAS de Document AI (LA MEJOR OPCIÓN PARA LISTAS DE PRECIOS) ---
        # ESTA SECCIÓN REQUIERE QUE TÚ LA DESARROLLES BASÁNDOTE EN LA ESTRUCTURA DE TUS PDFS
        # Y LA RESPUESTA DE LA API DE DOCUMENT AI.
        if document.tables:
            logging.info(f"✅ Se encontraron {len(document.tables)} tablas. (Lógica de procesamiento de tablas es un TODO).")
            # def get_text_from_layout_segments(text_segments, full_doc_text):
            #     # ... (función helper para extraer texto de celdas)
            # for table_idx, table in enumerate(document.tables):
            #     # 1. Mapear encabezados de la tabla a tus campos (nombre, precio, sku, unidad, etc.)
            #     # 2. Iterar por table.body_rows
            #     # 3. Extraer datos de celdas según el mapeo
            #     # 4. Usar parse_precio_flexible para precios
            #     # 5. Construir diccionario de producto y añadir a productos_extraidos_final
            #     pass 
            # logging.info(f"Productos extraídos de tablas: {len(productos_extraidos_final)}")
            pass # Placeholder para tu futura implementación de extracción de tablas


        # --- Fallback o Procesamiento Línea por Línea si no hay productos de tablas (o si decides complementarlo) ---
        # Podrías tener una condición como: if not productos_extraidos_final:
        logging.info("Iniciando procesamiento línea por línea del texto del documento...")
        
        lineas_del_documento = document.text.split('\n')
        for linea_cruda in lineas_del_documento:
            producto_candidato = extraer_info_producto_de_linea_pdf(linea_cruda, pyme_rubro_nombre)
            if producto_candidato:
                # Antes de añadir, verificar si un producto muy similar ya fue extraído de tablas
                # (esto es para evitar duplicados si usas AMBOS métodos: tablas y líneas)
                # Esta lógica de de-duplicación puede ser compleja.
                # Por ahora, simplemente añadimos si el procesamiento de tablas no se implementó o no dio resultados.
                producto_candidato["user_id"] = pyme_user_id if pyme_user_id is not None else 0
                productos_extraidos_final.append(producto_candidato)
        
        logging.info(f"Total de productos extraídos (final) para '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
        if not productos_extraidos_final:
            logging.warning(f"⚠️ No se pudo extraer ningún producto estructurado del PDF: {os.path.basename(pdf_path)}")
        
        return productos_extraidos_final

    except Exception as e:
        logging.error(f"❌ Error fatal en procesar_catalogo_pdf_google ({os.path.basename(pdf_path)}): {e}", exc_info=True)
        return []