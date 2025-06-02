# services/google_docai.py
import os
import json
import logging
import re
from google.cloud import documentai 
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional 

# Asegúrate de que utils.py esté en el mismo directorio 'services' o ajusta la ruta
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio 

logger = logging.getLogger(__name__)

# --- Carga de Credenciales (Corregida) ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
try:
    ruta_cred_render = "/etc/secrets/google_service_key.json" # Nombre de variable consistente
    ruta_cred_local = os.path.join(os.getcwd(), "instance", "google-credentials.json")
    ruta_cred_final = None

    if os.path.exists(ruta_cred_render): 
        ruta_cred_final = ruta_cred_render
    elif os.path.exists(ruta_cred_local): 
        ruta_cred_final = ruta_cred_local
    
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f: credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True; logger.info(f"✅ [DOCAI] Credenciales Google cargadas desde: {ruta_cred_final}")
    else: logger.error(f"❌ [DOCAI] Archivo de credenciales Google NO encontrado. Rutas: '{ruta_cred_render}', '{ruta_cred_local}'.")
except Exception as e: logger.error(f"❌ [DOCAI] Error crítico cargando credenciales Google: {e}", exc_info=True)
# --- Fin Carga ---

def _get_text_from_layout_segments(text_anchor: Optional[documentai.Document.TextAnchor], full_doc_text: str) -> str:
    response = "";
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            start = int(segment.start_index); end = int(segment.end_index)
            if 0 <= start <= end <= len(full_doc_text): response += full_doc_text[start:end]
            else: logger.warning(f"[DOCAI-SEG] Segmento de texto inválido: start={start}, end={end}, len_text={len(full_doc_text)}")
    return limpiar_texto_base(response)

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("[DOCAI-GET] Imposible procesar PDF: Credenciales Google no cargadas/inválidas.")
        return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")
        if not all([project_id, location, processor_id]):
            missing = [v_name for v_name,val in [("GOOGLE_PROJECT_ID",project_id),("GOOGLE_DOCAI_LOCATION",location),("GOOGLE_DOCAI_PROCESSOR_ID",processor_id)] if not val]
            logger.error(f"❌ [DOCAI-GET] Faltan variables de entorno Google DocAI: {', '.join(missing)}.")
            return None 
            
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file: pdf_content = file.read()
        raw_document_proto = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        
        request_doc_ai = documentai.ProcessRequest(
            name=resource_name, 
            raw_document=raw_document_proto, 
            skip_human_review=True
        )
        
        logger.info(f"[DOCAI-GET] Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            num_tablas_directo = len(result.document.tables) if hasattr(result.document, 'tables') and result.document.tables else 0
            num_tablas_paginas = sum(len(page.tables) for page in result.document.pages if hasattr(page, 'tables') and page.tables) if result.document.pages else 0
            logger.info(f"[DOCAI-GET] Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. "
                        f"Tablas (directo en doc): {num_tablas_directo}. Tablas (en páginas): {num_tablas_paginas}.")
            if not result.document.text and num_tablas_directo == 0 and num_tablas_paginas == 0 :
                 logger.warning(f"[DOCAI-GET] DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas.")
            return result.document
        else:
            logger.error(f"[DOCAI-GET] Document AI no devolvió un resultado de documento válido para '{os.path.basename(pdf_path)}'.")
            return None
    except Exception as e:
        logger.error(f"❌ [DOCAI-GET] Error en llamada a API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

# --- FUNCIÓN procesar_tablas_document_ai (COMPLETA Y PROBADA) ---
def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    productos_de_tablas: List[Dict[str, Any]] = []
    
    all_document_tables: List[documentai.Document.Page.Table] = []
    if hasattr(document, 'tables') and document.tables:
        all_document_tables.extend(document.tables)
    if document.pages:
        for page in document.pages:
            if hasattr(page, 'tables') and page.tables:
                all_document_tables.extend(page.tables)
    
    if not all_document_tables:
        logger.info("[DOCAI-TABLES] No se encontraron tablas en el documento.")
        return productos_de_tablas

    logger.info(f"[DOCAI-TABLES] Procesando {len(all_document_tables)} tablas encontradas en el PDF...")
    full_doc_text = document.text if document.text else ""

    # --- ¡¡¡ATENCIÓN MARCELO: PERSONALIZA ESTE MAPA DE ENCABEZADOS!!! ---
    # Este es el HEADER_MAP que funcionó para tu PDF "Salvador Patti".
    # Deberás ajustarlo o tener diferentes mapas si subes PDFs con encabezados muy distintos.
    HEADER_MAP_PDF: Dict[str, List[str]] = {
        "marca": ["marca", "bodega"],
        "nombre": ["varietal", "producto", "descripción", "articulo"], # "varietal" fue clave
        "unidad": ["un/caja", "unidades", "presentacion", "empaque"], # "un/caja" fue clave
        "precio_str": ["$ botella", "$ caja", "precio", "pvp", "valor"], # "$ botella" y "$ caja" fueron clave
        "sku": ["código", "cod.", "ref.", "art."], # Si tus PDFs usan SKU
        "categoria": ["linea", "línea", "tipo", "categoria"], # Si tienes esta info en tablas
        # "caja_pallet": ["caja/pallet"] # Ejemplo
    }
    # --- FIN SECCIÓN DE PERSONALIZACIÓN ---
        
    for table_idx, table_obj in enumerate(all_document_tables):
        logger.info(f"[DOCAI-TABLES] Procesando Tabla {table_idx + 1} de {len(all_document_tables)}...")
        if not table_obj.header_rows or not table_obj.body_rows:
            logger.warning(f"[DOCAI-TABLES] Tabla {table_idx + 1} no tiene filas de encabezado o cuerpo. Se omite.")
            continue

        header_rows_texts_list: List[List[str]] = []
        for hr_idx, hr in enumerate(table_obj.header_rows):
            header_cells = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in hr.cells]
            if any(c.strip() for c in header_cells):
                header_rows_texts_list.append(header_cells)
                logger.info(f"[DOCAI-TABLES] Encabezados crudos Tabla {table_idx+1}, Fila Header {hr_idx+1}: {header_cells}")
        
        if not header_rows_texts_list:
            logger.warning(f"[DOCAI-TABLES] No se pudieron extraer encabezados con texto para Tabla {table_idx + 1}. Se omite.")
            continue
        
        actual_headers = header_rows_texts_list[0] # Usar la primera fila de encabezados con contenido
        
        column_to_field_map: Dict[int, str] = {}
        for col_idx, header_text_raw in enumerate(actual_headers):
            header_text = limpiar_texto_base(header_text_raw)
            if not header_text: continue
            for field_name, possible_headers in HEADER_MAP_PDF.items():
                if any(ph.lower() in header_text for ph in possible_headers if ph):
                    if col_idx not in column_to_field_map: 
                        column_to_field_map[col_idx] = field_name
                        logger.info(f"[DOCAI-TABLES] Mapeo Tabla {table_idx+1}: Col Idx {col_idx} ('{header_text_raw}') -> Campo '{field_name}'")
                        break 
        
        # Un producto necesita al menos un nombre (o SKU/descripción como fallback) Y un precio para ser útil.
        if not column_to_field_map or not any(f in ["nombre", "sku", "descripcion"] for f in column_to_field_map.values()) or "precio_str" not in column_to_field_map.values() :
            logger.warning(f"[DOCAI-TABLES] Tabla {table_idx + 1}: Mapeo de columnas insuficiente (falta nombre/sku/descripción o precio). Se omite. Mapa: {column_to_field_map}")
            continue

        for row_idx, body_row_obj in enumerate(table_obj.body_rows):
            producto_candidato: Dict[str, Any] = {"user_id": pyme_user_id, "categoria_qdrant": pyme_rubro_nombre}
            celdas_fila_actual = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in body_row_obj.cells]
            
            celdas_con_texto_significativo = [c for c in celdas_fila_actual if c and len(c) > 1]
            if len(celdas_con_texto_significativo) < 1: continue

            for cell_idx, cell_text_raw in enumerate(celdas_fila_actual):
                field_name_mapped = column_to_field_map.get(cell_idx)
                if field_name_mapped:
                    cell_text_limpio = cell_text_raw.strip()
                    # Si el campo ya tiene algo (ej. precio_str por mapeo múltiple), tomar el último.
                    # Para descripción, se podría concatenar, pero para otros campos, el último suele ser mejor.
                    if cell_text_limpio: # Solo asignar si hay texto
                        producto_candidato[field_name_mapped] = cell_text_limpio
            
            nombre_tabla = limpiar_texto_base(str(producto_candidato.get("nombre", producto_candidato.get("descripcion", ""))))[:250]
            sku_tabla = limpiar_texto_base(str(producto_candidato.get("sku", "")))[:100]

            if not nombre_tabla and not sku_tabla: continue 
            if not nombre_tabla and sku_tabla: nombre_tabla = sku_tabla 

            precio_s, precio_f, moneda = parse_precio_flexible(producto_candidato.get("precio_str"))
            if precio_f is None and not re.search(r'(?i)consultar', precio_s or ""): # Si no hay precio numérico y no dice "consultar"
                logger.debug(f"[DOCAI-TABLES] Producto '{nombre_tabla}' omitido por falta de precio parseable ('{producto_candidato.get('precio_str')}')")
                continue

            producto_final_tabla = {"user_id": pyme_user_id, "nombre": nombre_tabla, "sku": sku_tabla}
            desc_candidata = str(producto_candidato.get("descripcion", ""))
            if limpiar_texto_base(desc_candidata) and limpiar_texto_base(desc_candidata) != nombre_tabla : 
                producto_final_tabla["descripcion"] = desc_candidata.strip()[:1000]
            else: producto_final_tabla["descripcion"] = ""
            
            producto_final_tabla["precio_str"] = precio_s if precio_s else ""
            producto_final_tabla["precio_float"] = precio_f
            producto_final_tabla["moneda"] = moneda if moneda else "ARS"
            producto_final_tabla["unidad"] = limpiar_texto_base(str(producto_candidato.get("unidad", "")))[:50]
            categoria_mapeada = limpiar_texto_base(str(producto_candidato.get("categoria", "")))
            producto_final_tabla["categoria_qdrant"] = categoria_mapeada[:100] if categoria_mapeada else pyme_rubro_nombre[:100]
            producto_final_tabla["marca"] = limpiar_texto_base(str(producto_candidato.get("marca", "")))[:100]
            producto_final_tabla["cantidad_disponible"] = "1" 

            productos_de_tablas.append(producto_final_tabla)
            logger.info(f"[DOCAI-TABLES] Producto de tabla agregado: {nombre_tabla[:50]} | Precio: {precio_s or 'N/A'}")
            
    if productos_de_tablas:
         logger.info(f"[DOCAI-TABLES] Se extrajeron {len(productos_de_tablas)} productos del análisis de tablas del PDF.")
    return productos_de_tablas

# --- FUNCIÓN extraer_info_producto_de_linea_pdf (COMPLETA Y PROBADA) ---
def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    # (Esta es la versión de la respuesta @‶gANVneHZLGZv... que ya tenía mejoras)
    linea_limpia_original = limpiar_texto_base(linea_procesar) 
    if not linea_limpia_original or len(linea_limpia_original) < 4: return None
    precio_regex = r"((?:[\$\€\£]?\s*\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?)|(?:\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?\s*[\$\€\£]?))\b(?:\s*(ARS|USD|EUR|GBP|CLP))?"
    codigo_pattern = re.compile(r"(\b(?:ART|COD|REF|SKU|ID|ITEM|NRO)\.?\s*[:\-#]?\s*[\w\d\/-]+)\b|(\b\d{4,15}[\w\d-]*\b)", re.IGNORECASE) # Aumentado a 15 para códigos largos
    texto_principal_para_nombre = linea_limpia_original; texto_precio_crudo = None; moneda_explicita_en_linea = None; best_price_match = None
    for match_p in re.finditer(precio_regex, linea_limpia_original, re.IGNORECASE):
        if re.search(r"\d", match_p.group(1)): best_price_match = match_p
    if best_price_match:
        texto_precio_crudo = limpiar_texto_base(best_price_match.group(1)); moneda_explicita_en_linea = limpiar_texto_base(best_price_match.group(2).upper()) if best_price_match.group(2) else None
        texto_principal_para_nombre = linea_limpia_original.replace(best_price_match.group(0), "").strip()
        if not texto_principal_para_nombre.strip() and texto_precio_crudo: logger.debug(f"[DOCAI-LINE] Línea parece solo precio: '{linea_limpia_original}'"); return None
    if not texto_principal_para_nombre.strip(): return None
    precio_s, precio_f, mon_p = parse_precio_flexible(texto_precio_crudo); moneda_final = moneda_explicita_en_linea if moneda_explicita_en_linea else (mon_p if mon_p else "ARS")
    texto_nombre_candidato = texto_principal_para_nombre; unidad_final, tipo_precio_final = extraer_unidades_y_tipos_precio(texto_nombre_candidato, pyme_rubro_nombre)
    if unidad_final: texto_nombre_candidato = limpiar_texto_base(re.sub(r'(?i)\b' + re.escape(unidad_final) + r'\b', '', texto_nombre_candidato, 1))
    match_codigo = codigo_pattern.search(texto_nombre_candidato); codigo_articulo_final = ""
    if match_codigo:
        codigo_raw = match_codigo.group(1) if match_codigo.group(1) else match_codigo.group(2); codigo_limpio = limpiar_texto_base(codigo_raw); es_codigo_valido = True
        if codigo_limpio.isdigit():
            if len(codigo_limpio) == 4 and (1900 < int(codigo_limpio) < 2100): es_codigo_valido = False
            elif len(codigo_limpio) > 7 and not any(pref in codigo_raw.upper() for pref in ["ART","COD","REF","SKU","ID"]): es_codigo_valido = False
        if es_codigo_valido: codigo_articulo_final = codigo_limpio; texto_nombre_candidato = limpiar_texto_base(texto_nombre_candidato.replace(codigo_raw, "", 1))
    nombre_final_producto = limpiar_texto_base(texto_nombre_candidato); descripcion_final_producto = ""
    if len(texto_principal_para_nombre) > len(nombre_final_producto) + 5 and limpiar_texto_base(texto_principal_para_nombre) != nombre_final_producto :
        if texto_principal_para_nombre.startswith(nombre_final_producto): descripcion_final_producto = texto_principal_para_nombre[len(nombre_final_producto):].strip(" -,:").strip()
        elif texto_principal_para_nombre.endswith(nombre_final_producto): descripcion_final_producto = texto_principal_para_nombre[:-len(nombre_final_producto)].strip(" -,:").strip()
    if not nombre_final_producto or len(nombre_final_producto) < 3: return None
    if nombre_final_producto.isdigit() and precio_f is None: return None
    palabras_a_evitar_en_nombre = ["total","subtotal","iva","fecha","pagina","página","cliente","pedido","factura","contacto","teléfono","email","observaciones","referencia","lista de precios","descripción","articulo","precio","código", "rubro", "marca", "sub rubro", "cantidad", "unidad", "importe", "vencimiento"]
    if len(nombre_final_producto.split()) <= 4 and any(palabra_evitar in nombre_final_producto.lower() for palabra_evitar in palabras_a_evitar_en_nombre) and precio_f is None:
        logger.debug(f"[DOCAI-LINE] Descartando por palabra irrelevante: '{linea_limpia_original}' -> Nombre: '{nombre_final_producto}'"); return None
    nombre_parseado_como_precio = parse_precio_flexible(nombre_final_producto)
    if nombre_parseado_como_precio[1] is not None and precio_f is None: logger.debug(f"[DOCAI-LINE] Descartando (nombre parece precio): '{linea_limpia_original}' -> Nombre: '{nombre_final_producto}'"); return None
    return {"nombre": nombre_final_producto[:250], "descripcion": limpiar_texto_base(descripcion_final_producto)[:500] if descripcion_final_producto else "", "precio_str": precio_s if precio_s else "", "precio_float": precio_f, "moneda": moneda_final if moneda_final else "ARS", "unidad": unidad_final[:50] if unidad_final else "", "tipo_precio": tipo_precio_final[:50] if tipo_precio_final else "", "sku": codigo_articulo_final[:100], "categoria_qdrant": pyme_rubro_nombre, "marca": ""}

# --- FUNCIÓN procesar_catalogo_pdf_google (COMPLETA Y PROBADA) ---
def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    logger.info(f"[DOCAI-MAIN] Iniciando procesamiento PDF: {os.path.basename(pdf_path)} para User ID: {user_id}, Rubro: {pyme_rubro_nombre}")
    document = _obtener_documento_ai(pdf_path)
    if not document: 
        logger.error(f"[DOCAI-MAIN] No se pudo obtener el objeto Document para {os.path.basename(pdf_path)}")
        return [] 
    
    productos_extraidos_final: List[Dict[str, Any]] = []
    
    # 1. Procesar tablas
    productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
    if productos_de_tablas: 
        productos_extraidos_final.extend(productos_de_tablas)
    
    # 2. Procesar texto línea por línea (solo si las tablas no dieron suficientes resultados o para complementar)
    #    Podrías ajustar el umbral (ej. len(productos_extraidos_final) < 5) o procesar siempre
    #    y luego tener una lógica de de-duplicación más robusta si es necesario.
    if (not productos_extraidos_final or len(productos_extraidos_final) < 5) and document.text: 
        logger.info(f"[DOCAI-MAIN] {'Pocos o ningún producto de tablas' if productos_extraidos_final else 'No productos de tablas'}, procesando líneas del PDF...")
        lineas = document.text.split('\n'); count_line_prods = 0
        for linea_idx, linea_cruda in enumerate(lineas):
            linea_strip = linea_cruda.strip()
            if len(linea_strip) < 5: continue 
            
            prod_de_linea = extraer_info_producto_de_linea_pdf(linea_strip, pyme_rubro_nombre)
            if prod_de_linea: 
                prod_de_linea["user_id"] = user_id 
                # Chequeo simple para evitar duplicados por nombre si ya se extrajo de tablas
                ya_existe = any(p.get("nombre","").lower() == prod_de_linea.get("nombre","").lower() for p in productos_de_tablas if p.get("nombre") and prod_de_linea.get("nombre"))
                if not ya_existe:
                    productos_extraidos_final.append(prod_de_linea)
                    count_line_prods += 1
                else:
                    logger.debug(f"[DOCAI-MAIN] Producto de línea '{prod_de_linea.get('nombre')}' omitido, posible duplicado de tabla.")
        logger.info(f"[DOCAI-MAIN] Procesamiento línea por línea añadió {count_line_prods} productos.")
    elif not document.text:
         logger.warning(f"[DOCAI-MAIN] Document AI no extrajo texto del PDF '{os.path.basename(pdf_path)}'. No se puede procesar línea por línea.")
            
    if not productos_extraidos_final: 
        logger.warning(f"⚠️[DOCAI-MAIN] No se pudo extraer ningún producto estructurado del PDF: {os.path.basename(pdf_path)}.")
    else: 
        logger.info(f"[DOCAI-MAIN] Total productos finales del PDF '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
        
    return productos_extraidos_final