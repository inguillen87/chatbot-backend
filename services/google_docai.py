# services/google_docai.py
import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional # Mejoras en tipado

# Asegúrate que la ruta a utils.py sea correcta desde este archivo
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio

logger = logging.getLogger(__name__)

# --- Carga de Credenciales ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
try:
    ruta_cred_render = "/etc/secrets/google_service_key.json"
    ruta_cred_local = os.path.join(os.getcwd(), "instance", "google-credentials.json")
    ruta_cred_final = None

    if os.path.exists(ruta_cred_render):
        ruta_cred_final = ruta_cred_render
    elif os.path.exists(ruta_cred_local):
        ruta_cred_final = ruta_cred_local
    
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f:
            credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True
        logger.info(f"✅ Credenciales de Google cargadas exitosamente desde: {ruta_cred_final}")
    else:
        logger.error(f"❌ Archivo de credenciales de Google NO encontrado. Rutas verificadas: '{ruta_cred_render}', '{ruta_cred_local}'. Document AI no funcionará.")
except Exception as e:
    logger.error(f"❌ Error crítico al cargar o parsear credenciales de Google: {e}", exc_info=True)
# --- Fin Carga de Credenciales ---

def _get_text_from_layout_segments(text_anchor: documentai.Document.TextAnchor, full_doc_text: str) -> str:
    """Helper para extraer y concatenar texto de segmentos de layout de Document AI."""
    response = ""
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            start_index = int(segment.start_index)
            end_index = int(segment.end_index)
            response += full_doc_text[start_index:end_index]
    return limpiar_texto_base(response) # Limpiar el texto extraído

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    """Envía el PDF a Document AI y devuelve el objeto Document."""
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("Imposible procesar PDF: Credenciales de Google no están cargadas o son inválidas.")
        return None

    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")

        if not all([project_id, location, processor_id]):
            missing_vars = [var for var, val in [("GOOGLE_PROJECT_ID", project_id), ("GOOGLE_DOCAI_LOCATION", location), ("GOOGLE_DOCAI_PROCESSOR_ID", processor_id)] if not val]
            logger.error(f"❌ Faltan variables de entorno para Google Document AI: {', '.join(missing_vars)}.")
            return None

        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        
        # Habilitar la extracción de tablas explícitamente es una buena práctica
        # si tu procesador está configurado para ello y es lo que esperas.
        process_options = documentai.ProcessOptions(
            table_extraction_params=documentai.ProcessOptions.TableExtractionParams(enabled=True)
            # ocr_config=documentai.OcrConfig(enable_native_pdf_parsing=True) # Podría mejorar la calidad del texto
        )
        
        request_doc_ai = documentai.ProcessRequest(
            name=resource_name, 
            raw_document=raw_document,
            process_options=process_options, # Usar process_options
            skip_human_review=True # Importante para procesamiento automático
        )
        
        logger.info(f"Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id} en {location})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            if not result.document.text and not result.document.tables:
                 logger.warning(f"Documento '{os.path.basename(pdf_path)}' procesado por Document AI, pero no se extrajo texto ni tablas. El PDF podría estar vacío, ser solo imágenes o tener un formato no soportado.")
                 return None # O devolver result.document si quieres inspeccionarlo
            logger.info(f"Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. Tablas encontradas: {len(result.document.tables)}")
            return result.document
        else:
            logger.error(f"Document AI no devolvió un resultado de documento válido para '{os.path.basename(pdf_path)}'.")
            return None
    except Exception as e:
        logger.error(f"❌ Error en la llamada a API de Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None


def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    """
    Procesa las tablas detectadas en un objeto documentai.Document.
    NECESITAS ADAPTAR LA LÓGICA DE MAPEADO DE COLUMNAS (HEADER_MAP) A TUS PDFS.
    """
    productos_de_tablas: List[Dict[str, Any]] = []
    if not document.tables:
        logger.info("No se encontraron tablas en el documento para procesar.")
        return productos_de_tablas

    logger.info(f"Detectadas {len(document.tables)} tablas. Iniciando extracción estructurada...")
    full_doc_text = document.text

    for table_idx, table_obj in enumerate(document.tables):
        logger.info(f"Procesando Tabla {table_idx + 1} de {len(document.tables)}...")
        if not table_obj.header_rows or not table_obj.body_rows:
            logger.warning(f"Tabla {table_idx + 1} no tiene filas de encabezado o cuerpo. Se omite.")
            continue

        # --- 1. Extraer y Mapear Encabezados ---
        # Esta es la parte MÁS CRÍTICA que DEBES ADAPTAR.
        # Necesitas identificar los nombres de tus columnas y a qué campo de producto corresponden.
        raw_headers = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in table_obj.header_rows[0].cells]
        logger.debug(f"  Encabezados crudos de Tabla {table_idx + 1}: {raw_headers}")

        # TU LÓGICA DE MAPEADO DE ENCABEADOS AQUÍ:
        # Ejemplo:
        HEADER_MAP = { 
            # Posibles nombres de columna en tu PDF -> Campo en tu diccionario de producto
            "nombre": ["producto", "artículo", "item", "descripción detallada", "nombre del producto"],
            "descripcion": ["descripción", "detalle", "características"],
            "precio_str": ["precio", "valor", "costo", "pvp", "precio unitario"],
            "sku": ["código", "sku", "ref", "id producto", "articulo nro"],
            "unidad": ["unidad", "presentación", "u/m"],
            "categoria": ["categoría", "familia", "línea", "rubro"],
            "marca": ["marca", "fabricante"],
            "stock": ["stock", "disponible", "cantidad"]
        }
        
        column_to_field_map: Dict[int, str] = {} # Mapea índice de columna a nombre de campo_producto
        for col_idx, header_text in enumerate(raw_headers):
            header_text_lower = header_text.lower()
            for field_name, possible_headers in HEADER_MAP.items():
                if any(ph.lower() in header_text_lower for ph in possible_headers):
                    if col_idx not in column_to_field_map: # Tomar el primer mapeo encontrado para un índice
                        column_to_field_map[col_idx] = field_name
                        logger.debug(f"    Mapeo: Columna {col_idx} ('{header_text}') -> Campo '{field_name}'")
                        break 
        
        if not column_to_field_map or "nombre" not in column_to_field_map.values():
            logger.warning(f"Tabla {table_idx + 1}: No se pudo generar un mapeo de columnas útil o falta columna de 'nombre'. Se omite esta tabla.")
            continue

        # --- 2. Iterar por Filas del Cuerpo de la Tabla ---
        for row_idx, body_row in enumerate(table_obj.body_rows):
            producto_candidato: Dict[str, Any] = {"user_id": pyme_user_id, "categoria": pyme_rubro_nombre} # Categoria default al rubro
            
            for cell_idx, cell in enumerate(body_row.cells):
                field_name_mapped = column_to_field_map.get(cell_idx)
                if field_name_mapped:
                    cell_text = _get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text)
                    producto_candidato[field_name_mapped] = cell_text # Guardar texto crudo

            # --- 3. Validar y Procesar Datos Extraídos de la Fila ---
            nombre_crudo = producto_candidato.get("nombre")
            if not nombre_crudo or len(limpiar_texto_base(nombre_crudo)) < 3 :
                # logger.debug(f"    Fila {row_idx+1} (Tabla {table_idx+1}) omitida: nombre ausente o muy corto.")
                continue 
            
            producto_candidato["nombre"] = limpiar_texto_base(nombre_crudo)[:250] # Limpiar y truncar
            
            if "descripcion" in producto_candidato:
                producto_candidato["descripcion"] = limpiar_texto_base(producto_candidato["descripcion"])[:1000]
            else:
                 producto_candidato["descripcion"] = "" # O tomar del nombre si es necesario

            if "precio_str" in producto_candidato:
                precio_s, precio_f, moneda = parse_precio_flexible(producto_candidato["precio_str"])
                producto_candidato["precio_str"] = precio_s if precio_s else ""
                producto_candidato["precio_float"] = precio_f
                producto_candidato["moneda"] = moneda if moneda else "ARS"
            else: # Si no hay columna de precio mapeada
                 producto_candidato["precio_str"] = ""
                 producto_candidato["precio_float"] = None
                 producto_candidato["moneda"] = "ARS"


            # Convertir otros campos si es necesario y limpiar
            for key in ["sku", "unidad", "categoria", "marca", "stock"]:
                if key in producto_candidato:
                    producto_candidato[key] = limpiar_texto_base(str(producto_candidato[key]))[:100]
            
            # Renombrar stock a cantidad_disponible si es necesario para el procesador posterior
            if "stock" in producto_candidato:
                producto_candidato["cantidad_disponible"] = producto_candidato.pop("stock")

            productos_de_tablas.append(producto_candidato)
            # logger.debug(f"    Producto de tabla agregado: {producto_candidato.get('nombre')}")

    if productos_de_tablas:
        logger.info(f"Extraídos {len(productos_de_tablas)} productos del análisis de tablas del PDF.")
    return productos_de_tablas


def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    """Intenta extraer información de producto de una sola línea de texto (Fallback)."""
    # (Tu lógica de extraer_info_producto_de_linea_pdf se mantiene en gran medida, 
    #  ya que es muy heurística. He hecho algunos ajustes menores para claridad y robustez.)

    linea_limpia_inicial = limpiar_texto_base(linea_procesar)
    if not linea_limpia_inicial or len(linea_limpia_inicial) < 4:
        return None

    # Patrones (ajustados ligeramente)
    # Regex más general para precio, permitiendo varios formatos y opcionalmente moneda al final.
    # El objetivo es encontrar un candidato a precio, parse_precio_flexible hará el trabajo fino.
    precio_pattern_str = r"((?:[\$\€\£]?\s*\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?)|(?:\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?\s*[\$\€\£]?))\b(?:\s*(ARS|USD|EUR|GBP|CLP))?"
    codigo_pattern = re.compile(r"(\b(?:ART|COD|REF|SKU|ID|ITEM|NRO)\.?\s*[:\-]?\s*[\w\d\/-]+)\b|(\b\d{4,}[\w\d-]*\b)", re.IGNORECASE)
    
    texto_principal = linea_limpia_inicial
    texto_precio_crudo = None
    moneda_explicita_en_linea = None

    # Buscar el precio. Se prioriza la última ocurrencia.
    best_price_match = None
    for match_p in re.finditer(precio_pattern_str, linea_limpia_inicial, re.IGNORECASE):
        if re.search(r"\d", match_p.group(1)): # El grupo 1 es la parte numérica/símbolo
            best_price_match = match_p
            
    if best_price_match:
        texto_precio_crudo = limpiar_texto_base(best_price_match.group(1)) 
        moneda_explicita_en_linea = limpiar_texto_base(best_price_match.group(2).upper()) if best_price_match.group(2) else None
        
        # Intentar aislar texto principal
        inicio_match, fin_match = best_price_match.span()
        parte_antes = linea_limpia_inicial[:inicio_match].strip()
        parte_despues = linea_limpia_inicial[fin_match:].strip()

        if len(parte_antes) > len(parte_despues) and len(parte_antes) >= 3:
            texto_principal = parte_antes
        elif len(parte_despues) >= 3:
            texto_principal = parte_despues
        elif parte_antes: # Si parte_despues es muy corta o vacía
            texto_principal = parte_antes
        elif parte_despues: # Si parte_antes es muy corta o vacía
            texto_principal = parte_despues
        else: # Si ambos son muy cortos, la línea original sin el precio
            texto_principal = linea_limpia_inicial.replace(best_price_match.group(0), "").strip()
        
        if not texto_principal.strip() and texto_precio_crudo:
             logger.debug(f"[DOCAI LINE] Descartando, parece solo precio: '{linea_limpia_inicial}'")
             return None

    precio_str_final, precio_float_final, moneda_final_parsed = parse_precio_flexible(texto_precio_crudo)
    moneda_final = moneda_explicita_en_linea if moneda_explicita_en_linea else (moneda_final_parsed if moneda_final_parsed else "ARS")

    if not texto_principal.strip(): # Si después de todo no quedó texto principal
        return None
        
    texto_nombre_candidato = texto_principal
    unidad_final, tipo_precio_final = extraer_unidades_y_tipos_precio(texto_nombre_candidato, pyme_rubro_nombre)

    if unidad_final:
        texto_nombre_candidato = limpiar_texto_base(re.sub(r'(?i)\b' + re.escape(unidad_final) + r'\b', '', texto_nombre_candidato, 1))
    if tipo_precio_final:
        texto_nombre_candidato = limpiar_texto_base(re.sub(r'(?i)\b' + re.escape(tipo_precio_final) + r'\b', '', texto_nombre_candidato, 1))
    
    match_codigo = codigo_pattern.search(texto_nombre_candidato)
    codigo_articulo_extraido = ""
    if match_codigo:
        codigo_encontrado_raw = match_codigo.group(1) if match_codigo.group(1) else match_codigo.group(2)
        codigo_encontrado_limpio = limpiar_texto_base(codigo_encontrado_raw)
        # Heurística para evitar tomar años o números muy largos como códigos si no tienen prefijo
        es_potencial_codigo = True
        if codigo_encontrado_limpio.isdigit():
            if len(codigo_encontrado_limpio) == 4 and (1900 < int(codigo_encontrado_limpio) < 2100): # Es un año?
                es_potencial_codigo = False
            elif len(codigo_encontrado_limpio) > 8 and not any(pref in codigo_encontrado_raw.upper() for pref in ["ART","COD","REF","SKU"]): # Número largo sin prefijo
                 es_potencial_codigo = False
        if es_potencial_codigo:
            codigo_articulo_extraido = codigo_encontrado_limpio
            texto_nombre_candidato = limpiar_texto_base(texto_nombre_candidato.replace(codigo_encontrado_raw, "", 1))
    
    nombre_final_producto = limpiar_texto_base(texto_nombre_candidato)
    
    descripcion_final = ""
    if texto_principal != nombre_final_producto and len(texto_principal) > len(nombre_final_producto) + 5 :
        descripcion_final = texto_principal
        if limpiar_texto_base(descripcion_final) == nombre_final_producto: descripcion_final = ""
            
    # Filtros de calidad
    if not nombre_final_producto or len(nombre_final_producto) < 3: return None
    if nombre_final_producto.isdigit() and precio_float_final is None: return None

    palabras_a_evitar = ["total", "subtotal", "iva", "fecha", "página", "cliente", "pedido", "factura", "contacto", "teléfono", "email", "descuento", "observaciones", "referencia", "lista de precios"]
    nombre_eval_lower = nombre_final_producto.lower()
    if len(nombre_final_producto.split()) <= 3 and any(palabra_evitar in nombre_eval_lower for palabra_evitar in palabras_a_evitar):
        logger.debug(f"[DOCAI LINE] Descarte por palabra clave irrelevante en nombre corto y sin precio claro: '{linea_limpia_inicial}' -> '{nombre_final_producto}'")
        return None
            
    return {
        "nombre": nombre_final_producto[:250],
        "descripcion": descripcion_final[:500] if descripcion_final else "",
        "precio_str": precio_str_final if precio_str_final else "",
        "precio_float": precio_float_final,
        "moneda": moneda_final if moneda_final else "ARS",
        "unidad": unidad_final[:50] if unidad_final else "",
        "tipo_precio": tipo_precio_final[:50] if tipo_precio_final else "",
        "codigo_articulo": codigo_articulo_extraido[:100],
        "categoria": "" # La categoría es difícil de determinar de una sola línea
    }


def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """
    Función principal para procesar un catálogo PDF.
    Intenta usar análisis de tablas (si se implementa) y luego procesamiento línea por línea.
    """
    document = _obtener_documento_ai(pdf_path)
    if not document:
        return [] 
    
    productos_extraidos_final: List[Dict[str, Any]] = []

    # --- 1. PROCESAMIENTO DE TABLAS ---
    # Esta es la parte que DEBES personalizar.
    # Si tus PDFs tienen tablas claras, esta es la mejor forma de extraer datos.
    productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
    if productos_de_tablas:
        productos_extraidos_final.extend(productos_de_tablas)

    # --- 2. PROCESAMIENTO LÍNEA POR LÍNEA (Fallback o Complementario) ---
    # Considera si solo quieres procesar líneas si las tablas no dieron resultados,
    # o si quieres procesar ambos y luego de-duplicar.
    
    if not document.text:
        logger.warning(f"Document AI no extrajo texto del PDF '{os.path.basename(pdf_path)}'. No se puede procesar línea por línea.")
    else:
        if not productos_extraidos_final: # Solo procesar líneas si las tablas no dieron nada
            logger.info(f"No se extrajeron productos de tablas (o la lógica no está implementada). Procediendo con análisis línea por línea del PDF '{os.path.basename(pdf_path)}'...")
            lineas_del_documento = document.text.split('\n')
            logger.info(f"Procesando {len(lineas_del_documento)} líneas de texto.")
            
            for i, linea_cruda in enumerate(lineas_del_documento):
                linea_strip = linea_cruda.strip()
                if len(linea_strip) < 5 or (linea_strip.isdigit() and len(linea_strip)<=3): # Ignorar líneas muy cortas o que solo son números pequeños (ej. pág)
                    continue
                
                producto_candidato = extraer_info_producto_de_linea_pdf(linea_strip, pyme_rubro_nombre)
                if producto_candidato:
                    producto_candidato["user_id"] = user_id 
                    # Aquí podrías tener una lógica de de-duplicación más inteligente si combinas
                    # resultados de tablas y de líneas. Por ahora, se añaden si las tablas no dieron nada.
                    productos_extraidos_final.append(producto_candidato)
            logger.info(f"Productos extraídos del procesamiento línea por línea: {len(productos_extraidos_final) - len(productos_de_tablas if productos_de_tablas else []) }")
        else:
            logger.info("Se extrajeron productos de tablas. Se omite el procesamiento línea por línea para evitar duplicados (o implementa de-duplicación).")


    if not productos_extraidos_final:
        logger.warning(f"⚠️ No se pudo extraer ningún producto estructurado del PDF: {os.path.basename(pdf_path)}.")
    else:
        logger.info(f"Total de productos estructurados finales del PDF '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
        # Podrías loguear algunos ejemplos: logger.debug(f"Ejemplos: {productos_extraidos_final[:2]}")
        
    return productos_extraidos_final