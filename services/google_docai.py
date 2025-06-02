# services/google_docai.py
import os
import json
import logging
import re
from google.cloud import documentai # Usar el import general
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional 

# Asegúrate de que utils.py esté en el mismo directorio 'services' o ajusta la ruta
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio 

logger = logging.getLogger(__name__)

# --- Carga de Credenciales (Tu código actual está bien) ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
try:
    ruta_render_path = "/etc/secrets/google_service_key.json" 
    ruta_local_path = os.path.join(os.getcwd(), "instance", "google-credentials.json")
    ruta_cred_final = None
    if os.path.exists(ruta_render_path): ruta_cred_final = ruta_render_path
    elif os.path.exists(ruta_local_path): ruta_cred_final = ruta_local_path
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f: credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True; logger.info(f"✅ [DOCAI] Credenciales Google cargadas desde: {ruta_cred_final}")
    else: logger.error(f"❌ [DOCAI] Archivo de credenciales Google NO encontrado. Rutas: '{ruta_render_path}', '{ruta_local_path}'.")
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
        
        # Llamada simplificada a ProcessRequest
        request_doc_ai = documentai.ProcessRequest(
            name=resource_name, 
            raw_document=raw_document_proto, 
            skip_human_review=True
        )
        logger.info(f"[DOCAI-GET] Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            # --- ACCESO SEGURO A TABLAS y LOGGING MEJORADO ---
            num_tablas_en_doc = 0
            num_tablas_en_paginas = 0

            # Verificar si el atributo 'tables' existe directamente en el objeto Document
            if hasattr(result.document, 'tables') and result.document.tables is not None:
                num_tablas_en_doc = len(result.document.tables)
            
            # Adicionalmente, verificar tablas anidadas en páginas (más común)
            if result.document.pages:
                for page in result.document.pages:
                    if hasattr(page, 'tables') and page.tables is not None:
                         num_tablas_en_paginas += len(page.tables)
            
            logger.info(f"[DOCAI-GET] Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. "
                        f"Tablas (atributo directo 'document.tables'): {num_tablas_en_doc}. "
                        f"Tablas (en 'document.pages'): {num_tablas_en_paginas}.")
            
            if not result.document.text and num_tablas_en_doc == 0 and num_tablas_en_paginas == 0:
                 logger.warning(f"[DOCAI-GET] DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas con la configuración actual.")
            return result.document
        else:
            logger.error(f"[DOCAI-GET] Document AI no devolvió un resultado de documento válido para '{os.path.basename(pdf_path)}'.")
            return None
    except Exception as e:
        logger.error(f"❌ [DOCAI-GET] Error en llamada a API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

# --- TU FUNCIÓN procesar_tablas_document_ai (DEBES PERSONALIZAR HEADER_MAP_PDF) ---
def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    productos_de_tablas: List[Dict[str, Any]] = []
    
    # Las tablas suelen estar anidadas en las páginas del documento
    doc_tables_to_process = []
    if hasattr(document, 'tables') and document.tables: # Algunas APIs/procesadores las ponen aquí
         doc_tables_to_process.extend(document.tables)
    if document.pages:
        for page in document.pages:
            if hasattr(page, 'tables') and page.tables:
                doc_tables_to_process.extend(page.tables)
    
    if not doc_tables_to_process:
        logger.info("[DOCAI-TABLES] No se encontraron tablas en el documento (ni en document.tables ni en document.pages[N].tables).")
        return productos_de_tablas

    logger.info(f"[DOCAI-TABLES] Procesando {len(doc_tables_to_process)} tablas encontradas en el PDF...")
    full_doc_text = document.text if document.text else ""

    # ¡¡¡ESTE HEADER_MAP_PDF ES UN EJEMPLO!!! DEBES AJUSTARLO A TUS PDFs
    HEADER_MAP_PDF: Dict[str, List[str]] = {
        "sku": ["código", "cod.", "codigo", "art.", "articulo", "ref.", "referencia", "item", "id", "art"],
        "nombre": ["producto", "descripción", "descripcion", "detalle", "nombre del producto", "articulo", "item name", "designacion", "varietal"],
        "marca": ["marca", "bodega", "fabricante", "brand"],
        "categoria": ["categoría", "categoria", "linea", "línea", "rubro", "tipo"],
        "precio_str": ["precio", "valor", "importe", "pvp", "p.v.p", "contado", "lista", "unitario", "distribuidor", "mayorista", "minorista", "final", "$ botella", "$ caja", "$ unitario"],
        "unidad": ["unidad", "presentación", "presentacion", "empaque", "formato", "u/m", "un/caja", "caja x", "pack x", "unid.", "unidades"],
        "stock": ["stock", "disponible", "cantidad", "cant.", "disponibilidad", "existencia"],
        # Añade más campos que necesites mapear
    }
        
    for table_idx, table_obj in enumerate(doc_tables_to_process):
        logger.info(f"[DOCAI-TABLES] Procesando Tabla {table_idx + 1}...")
        if not table_obj.header_rows or not table_obj.body_rows:
            logger.warning(f"[DOCAI-TABLES] Tabla {table_idx + 1} no tiene filas de encabezado o cuerpo. Se omite.")
            continue

        header_row_texts_list: List[List[str]] = []
        for hr in table_obj.header_rows:
             header_cells = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in hr.cells]
             if any(c.strip() for c in header_cells): # Solo añadir si la fila de header tiene algún texto
                 header_row_texts_list.append(header_cells)
        
        if not header_row_texts_list or not header_row_texts_list[0]:
            logger.warning(f"[DOCAI-TABLES] No se pudieron extraer encabezados con texto para Tabla {table_idx + 1}. Se omite.")
            continue
        
        # Usar la primera fila de encabezados con contenido para el mapeo
        actual_headers = header_row_texts_list[0]
        logger.info(f"[DOCAI-TABLES] Encabezados crudos Tabla {table_idx + 1} (usando fila 1 de headers): {actual_headers}")

        column_to_field_map: Dict[int, str] = {}
        for col_idx, header_text_raw in enumerate(actual_headers):
            header_text = limpiar_texto_base(header_text_raw)
            for field_name, possible_headers in HEADER_MAP_PDF.items():
                if any(ph.lower() in header_text.lower() for ph in possible_headers if ph): # Asegurar que ph no sea None/vacío
                    if col_idx not in column_to_field_map: 
                        column_to_field_map[col_idx] = field_name
                        logger.info(f"[DOCAI-TABLES] Mapeo Tabla {table_idx+1}: Col Idx {col_idx} ('{header_text_raw}') -> Campo '{field_name}'")
                        break 
        
        if not column_to_field_map or not any(f in ["nombre", "sku", "descripcion"] for f in column_to_field_map.values()):
            logger.warning(f"[DOCAI-TABLES] Tabla {table_idx + 1}: Mapeo de columnas insuficiente (falta 'nombre'/'sku'/'descripcion'). Se omite. Mapa: {column_to_field_map}")
            continue

        for row_idx, body_row_obj in enumerate(table_obj.body_rows):
            producto_candidato: Dict[str, Any] = {"user_id": pyme_user_id, "categoria": pyme_rubro_nombre}
            celdas_fila_actual = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in body_row_obj.cells]
            
            celdas_con_texto_significativo = [c for c in celdas_fila_actual if c and len(c) > 1] # Texto más que un solo caracter
            if len(celdas_con_texto_significativo) < 1: # Si la fila está prácticamente vacía
                continue

            for cell_idx, cell_text_raw in enumerate(celdas_fila_actual):
                field_name_mapped = column_to_field_map.get(cell_idx)
                if field_name_mapped:
                    cell_text_limpio = limpiar_texto_base(cell_text_raw)
                    if field_name_mapped in producto_candidato and producto_candidato[field_name_mapped] and cell_text_limpio:
                        producto_candidato[field_name_mapped] += " " + cell_text_limpio 
                    elif cell_text_limpio:
                        producto_candidato[field_name_mapped] = cell_text_limpio
            
            nombre_tabla = limpiar_texto_base(str(producto_candidato.get("nombre", producto_candidato.get("descripcion", ""))))[:250]
            sku_tabla = limpiar_texto_base(str(producto_candidato.get("sku", "")))[:100]

            if not nombre_tabla and not sku_tabla: continue 
            if not nombre_tabla and sku_tabla: nombre_tabla = sku_tabla 

            producto_final_tabla = {"user_id": pyme_user_id, "nombre": nombre_tabla, "sku": sku_tabla}
            desc_candidata = str(producto_candidato.get("descripcion", ""))
            if limpiar_texto_base(desc_candidata) != nombre_tabla : producto_final_tabla["descripcion"] = limpiar_texto_base(desc_candidata)[:1000]
            else: producto_final_tabla["descripcion"] = ""

            precio_s, precio_f, moneda = parse_precio_flexible(producto_candidato.get("precio_str"))
            producto_final_tabla["precio_str"] = precio_s if precio_s else ""
            producto_final_tabla["precio_float"] = precio_f
            producto_final_tabla["moneda"] = moneda if moneda else "ARS"
            
            producto_final_tabla["unidad"] = limpiar_texto_base(str(producto_candidato.get("unidad", "")))[:50]
            # Usar categoría del mapeo si existe, sino el rubro_pyme_nombre
            categoria_mapeada = limpiar_texto_base(str(producto_candidato.get("categoria", "")))
            producto_final_tabla["categoria"] = categoria_mapeada[:100] if categoria_mapeada else pyme_rubro_nombre[:100]
            producto_final_tabla["marca"] = limpiar_texto_base(str(producto_candidato.get("marca", "")))[:100]
            producto_final_tabla["cantidad_disponible"] = "1" # Default, DocAI no suele dar stock directo

            productos_de_tablas.append(producto_final_tabla)
            logger.info(f"[DOCAI-TABLES] Producto de tabla agregado: {nombre_tabla[:50]}")
            
    if productos_de_tablas:
         logger.info(f"[DOCAI-TABLES] Se extrajeron {len(productos_de_tablas)} productos del análisis de tablas del PDF.")
    return productos_de_tablas

# --- Tu función extraer_info_producto_de_linea_pdf ---
def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    # (Pega aquí tu función extraer_info_producto_de_linea_pdf completa y ya mejorada
    #  de la respuesta @‶gANVneHZLGZv... que incluye el logging y las mejoras en regex)
    linea_limpia = limpiar_texto_base(linea_procesar)
    if not linea_limpia or len(linea_limpia) < 4: return None
    precio_regex = r"((?:[\$\€\£]?\s*\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?)|(?:\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?\s*[\$\€\£]?))\b(?:\s*(ARS|USD|EUR|GBP|CLP))?"
    codigo_pattern = re.compile(r"(\b(?:ART|COD|REF|SKU|ID|ITEM|NRO)\.?\s*[:\-]?\s*[\w\d\/-]+)\b|(\b\d{4,}[\w\d-]*\b)", re.IGNORECASE)
    texto_principal = linea_limpia; texto_precio_crudo = None; moneda_explicita_en_linea = None
    best_price_match = None
    for match_p in re.finditer(precio_regex, linea_limpia, re.IGNORECASE):
        if re.search(r"\d", match_p.group(1)): best_price_match = match_p # Tomar el último que tenga dígitos
    if best_price_match:
        texto_precio_crudo = limpiar_texto_base(best_price_match.group(1)); moneda_explicita_en_linea = limpiar_texto_base(best_price_match.group(2).upper()) if best_price_match.group(2) else None
        texto_principal = linea_limpia.replace(best_price_match.group(0), "").strip()
        if not texto_principal.strip() and texto_precio_crudo: logger.debug(f"[DOCAI-LINE] Descartando (solo precio): '{linea_limpia}'"); return None
    if not texto_principal.strip(): return None # Si después de quitar precio no queda nada
    
    precio_s, precio_f, mon_p = parse_precio_flexible(texto_precio_crudo); mon_f = moneda_explicita_en_linea if moneda_explicita_en_linea else (mon_p if mon_p else "ARS")
    
    texto_nombre_cand = texto_principal; unidad_f, tipo_p_f = extraer_unidades_y_tipos_precio(texto_nombre_cand, pyme_rubro_nombre)
    if unidad_f: texto_nombre_cand = limpiar_texto_base(re.sub(r'(?i)\b' + re.escape(unidad_f) + r'\b', '', texto_nombre_cand, 1))
    # if tipo_p_f: texto_nombre_cand = limpiar_texto_base(re.sub(r'(?i)\b' + re.escape(tipo_p_f) + r'\b', '', texto_nombre_cand, 1)) # No quitar tipo de precio del nombre

    match_cod = codigo_pattern.search(texto_nombre_cand); cod_art_ext = ""
    if match_cod:
        cod_raw = match_cod.group(1) if match_cod.group(1) else match_cod.group(2); cod_limpio = limpiar_texto_base(cod_raw); es_cod = True
        if cod_limpio.isdigit():
            if len(cod_limpio) == 4 and (1900 < int(cod_limpio) < 2100): es_cod = False # Evitar años
            # Podríamos añadir más heurísticas para evitar que números de teléfono o CUITs se tomen como SKU
        if es_cod: cod_art_ext = cod_limpio; texto_nombre_cand = limpiar_texto_base(texto_nombre_cand.replace(cod_raw, "", 1))
    
    nombre_final_prod = limpiar_texto_base(texto_nombre_cand); desc_final = ""
    # Si el texto_principal original (sin precio) es significativamente más largo que el nombre candidato, asumir que el resto es descripción
    if len(texto_principal) > len(nombre_final_prod) + 5 and limpiar_texto_base(texto_principal) != nombre_final_prod :
        # Extraer descripción de la parte que no es nombre_final_prod
        # Esto es complejo, por ahora una heurística simple:
        if texto_principal.startswith(nombre_final_prod): desc_final = texto_principal[len(nombre_final_prod):].strip()
        elif texto_principal.endswith(nombre_final_prod): desc_final = texto_principal[:-len(nombre_final_prod)].strip()
        else: desc_final = texto_principal # O dejarlo vacío si no es claro

    if not nombre_final_prod or len(nombre_final_prod) < 3: return None
    if nombre_final_prod.isdigit() and precio_f is None: return None # Nombre es solo un número y no hay precio
    
    pal_evitar_si_cortas = ["total","subtotal","iva","fecha","pagina","página","cliente","pedido","factura","contacto","teléfono","email","observaciones","referencia","lista de precios","descripción","articulo","precio","código", "rubro", "marca"]
    if len(nombre_final_prod.split()) <= 3 and any(p_ev in nombre_final_prod.lower() for p_ev in pal_evitar_si_cortas) and precio_f is None:
        logger.debug(f"[DOCAI-LINE] Descarte por palabra irrelevante: '{linea_limpia}' -> '{nombre_final_prod}'")
        return None

    return {"nombre": nombre_final_prod[:250], "descripcion": desc_final[:500] if desc_final else "", 
            "precio_str": precio_s if precio_s else "", "precio_float": precio_f, 
            "moneda": mon_f if mon_f else "ARS", "unidad": unidad_f[:50] if unidad_f else "", 
            "tipo_precio": tipo_p_f[:50] if tipo_p_f else "", "sku": cod_art_ext[:100], # Renombrado de codigo_articulo a sku
            "categoria": ""} # Categoría no se extrae fácilmente de una línea

# --- Tu función procesar_catalogo_pdf_google ---
def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    logger.info(f"[DOCAI-MAIN] Iniciando procesamiento PDF: {os.path.basename(pdf_path)} para User ID: {user_id}, Rubro: {pyme_rubro_nombre}")
    document = _obtener_documento_ai(pdf_path)
    if not document: 
        logger.error(f"[DOCAI-MAIN] No se pudo obtener el objeto Document para {os.path.basename(pdf_path)}")
        return [] 
    
    productos_extraidos_final: List[Dict[str, Any]] = []
    
    # Procesar tablas
    productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
    if productos_de_tablas: 
        productos_extraidos_final.extend(productos_de_tablas)
        logger.info(f"[DOCAI-MAIN] Extracción de tablas aportó {len(productos_de_tablas)} productos.")
    
    # Procesar texto línea por línea solo si no se encontraron productos en tablas
    # Opcional: podrías procesar líneas siempre y luego de-duplicar.
    if not productos_extraidos_final and document.text: 
        logger.info(f"[DOCAI-MAIN] No se extrajeron productos de tablas (o no habían). Procesando líneas del PDF...")
        lineas = document.text.split('\n'); count_line_prods = 0
        for linea_idx, linea_cruda in enumerate(lineas):
            linea_strip = linea_cruda.strip()
            if len(linea_strip) < 5: continue # Ignorar líneas muy cortas
            
            # logger.debug(f"[DOCAI-MAIN] Procesando línea {linea_idx + 1}: '{linea_strip[:100]}'")
            prod = extraer_info_producto_de_linea_pdf(linea_strip, pyme_rubro_nombre)
            if prod: 
                prod["user_id"] = user_id # Asegurar que user_id se asigne aquí también
                productos_extraidos_final.append(prod) 
                count_line_prods += 1
        logger.info(f"[DOCAI-MAIN] Procesamiento línea por línea añadió {count_line_prods} productos.")
    elif not document.text:
         logger.warning(f"[DOCAI-MAIN] Document AI no extrajo texto del PDF '{os.path.basename(pdf_path)}'. No se puede procesar línea por línea.")
            
    if not productos_extraidos_final: 
        logger.warning(f"⚠️[DOCAI-MAIN] No se pudo extraer ningún producto estructurado del PDF: {os.path.basename(pdf_path)}.")
    else: 
        logger.info(f"[DOCAI-MAIN] Total productos finales del PDF '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
        # logger.debug(f"Ejemplos PDF: {productos_extraidos_final[:2]}")
        
    return productos_extraidos_final