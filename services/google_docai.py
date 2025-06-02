# services/google_docai.py
import os
import json
import logging
import re
from google.cloud import documentai
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional 
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio

logger = logging.getLogger(__name__)

# --- Carga de Credenciales ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
try:
    ruta_render = "/etc/secrets/google_service_key.json"; ruta_cred_local = os.path.join(os.getcwd(), "instance", "google-credentials.json"); ruta_cred_final = None
    if os.path.exists(ruta_render): ruta_cred_final = ruta_cred_render
    elif os.path.exists(ruta_cred_local): ruta_cred_final = ruta_cred_local
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f: credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True; logger.info(f"✅ [DOCAI] Credenciales Google cargadas: {ruta_cred_final}")
    else: logger.error(f"❌ [DOCAI] Credenciales Google NO encontradas. Rutas: '{ruta_cred_render}', '{ruta_cred_local}'.")
except Exception as e: logger.error(f"❌ [DOCAI] Error crítico cargando credenciales Google: {e}", exc_info=True)
# --- Fin Carga ---

def _get_text_from_layout_segments(text_anchor: Optional[documentai.Document.TextAnchor], full_doc_text: str) -> str:
    response = "";
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            start = int(segment.start_index); end = int(segment.end_index)
            if 0 <= start <= end <= len(full_doc_text): response += full_doc_text[start:end]
            else: logger.warning(f"[DOCAI] Segmento de texto inválido: start={start}, end={end}, len_text={len(full_doc_text)}")
    return limpiar_texto_base(response)

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("[DOCAI] Imposible procesar PDF: Credenciales Google no cargadas/inválidas.")
        return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")
        if not all([project_id, location, processor_id]):
            missing = [v_name for v_name,val in [("GOOGLE_PROJECT_ID",project_id),("GOOGLE_DOCAI_LOCATION",location),("GOOGLE_DOCAI_PROCESSOR_ID",processor_id)] if not val]
            logger.error(f"❌ [DOCAI] Faltan variables de entorno Google DocAI: {', '.join(missing)}.")
            return None 
            
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file: pdf_content = file.read()
        raw_document_proto = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        
        # --- LLAMADA SIMPLIFICADA ---
        # Confiar en la configuración del procesador en Google Cloud para la extracción de tablas.
        # La mayoría de los procesadores genéricos o específicos para formularios/facturas
        # intentarán extraer tablas si están presentes y configuradas.
        request_doc_ai = documentai.ProcessRequest(
            name=resource_name, 
            raw_document=raw_document_proto, 
            skip_human_review=True # Importante para procesamiento automático
        )
        # --- FIN LLAMADA SIMPLIFICADA ---

        logger.info(f"[DOCAI] Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            num_tablas = 0
            # Acceso seguro al atributo 'tables'
            if hasattr(result.document, 'tables') and result.document.tables is not None:
                num_tablas = len(result.document.tables)
            
            logger.info(f"[DOCAI] Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. Tablas (atributo directo): {num_tablas}")
            
            if not result.document.text and num_tablas == 0:
                 logger.warning(f"[DOCAI] DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas con la configuración actual.")
            return result.document
        else:
            logger.error(f"[DOCAI] Document AI no devolvió un resultado de documento válido para '{os.path.basename(pdf_path)}'.")
            return None
    except Exception as e:
        logger.error(f"❌ [DOCAI] Error en llamada a API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

# --- Tu función procesar_tablas_document_ai ---
# (RECUERDA QUE NECESITAS PERSONALIZAR EL HEADER_MAP_PDF Y LA LÓGICA INTERNA)
def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    productos_de_tablas: List[Dict[str, Any]] = []
    if not hasattr(document, 'tables') or not document.tables:
        logger.info("[DOCAI-TABLES] No se encontraron tablas en el documento (via document.tables).")
        return productos_de_tablas

    logger.info(f"[DOCAI-TABLES] Procesando {len(document.tables)} tablas encontradas en el PDF...")
    full_doc_text = document.text if document.text else ""

    # ESTE HEADER_MAP ES UN EJEMPLO AMPLIO. DEBES AJUSTARLO A LOS NOMBRES DE COLUMNA DE TUS PDFS.
    HEADER_MAP_PDF: Dict[str, List[str]] = {
        "sku": ["código", "cod.", "codigo", "art.", "articulo", "ref.", "referencia", "item", "id", "art"],
        "nombre": ["producto", "descripción", "descripcion", "detalle", "nombre del producto", "articulo", "item name", "designacion", "varietal"],
        "marca": ["marca", "bodega", "fabricante", "brand"],
        "categoria": ["categoría", "categoria", "linea", "línea", "rubro", "tipo"],
        "precio_str": ["precio", "valor", "importe", "pvp", "p.v.p", "contado", "lista", "unitario", "distribuidor", "mayorista", "minorista", "final", "$ botella", "$ caja"],
        "unidad": ["unidad", "presentación", "presentacion", "empaque", "formato", "u/m", "un/caja", "caja x", "pack x", "unid."],
        "stock": ["stock", "disponible", "cantidad", "cant.", "disponibilidad", "existencia"],
    }
        
    for table_idx, table_obj in enumerate(document.tables):
        logger.info(f"[DOCAI-TABLES] Procesando Tabla {table_idx + 1}...")
        if not table_obj.header_rows or not table_obj.body_rows:
            logger.warning(f"[DOCAI-TABLES] Tabla {table_idx + 1} no tiene filas de encabezado o cuerpo. Se omite.")
            continue

        header_rows_texts: List[List[str]] = [[_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in hr.cells] for hr in table_obj.header_rows]
        if not header_rows_texts or not header_rows_texts[0]:
            logger.warning(f"[DOCAI-TABLES] No se pudieron extraer encabezados para Tabla {table_idx + 1}. Se omite.")
            continue
        
        logger.info(f"[DOCAI-TABLES] Encabezados crudos Tabla {table_idx + 1} (fila 1): {header_rows_texts[0]}")

        column_to_field_map: Dict[int, str] = {}
        for col_idx, header_text_raw in enumerate(header_rows_texts[0]): # Usar primera fila de headers para mapeo
            header_text = limpiar_texto_base(header_text_raw)
            for field_name, possible_headers in HEADER_MAP_PDF.items():
                if any(ph.lower() in header_text.lower() for ph in possible_headers):
                    if col_idx not in column_to_field_map: 
                        column_to_field_map[col_idx] = field_name
                        logger.info(f"[DOCAI-TABLES] Mapeo Tabla {table_idx+1}: Col Idx {col_idx} ('{header_text_raw}') -> Campo '{field_name}'")
                        break 
        
        if not column_to_field_map or not any(f in ["nombre", "sku", "descripcion"] for f in column_to_field_map.values()):
            logger.warning(f"[DOCAI-TABLES] Tabla {table_idx + 1}: Mapeo de columnas insuficiente o falta 'nombre'/'sku'/'descripcion'. Se omite.")
            continue

        for row_idx, body_row_obj in enumerate(table_obj.body_rows):
            producto_candidato: Dict[str, Any] = {"user_id": pyme_user_id, "categoria": pyme_rubro_nombre} # Categoria default al rubro
            celdas_fila_actual = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in body_row_obj.cells]
            
            # Si la fila está casi vacía, saltarla (ej. solo tiene 1 o 2 celdas con texto corto)
            celdas_con_texto_significativo = [c for c in celdas_fila_actual if c and len(c) > 2]
            if len(celdas_con_texto_significativo) < 2: # Requiere al menos 2 celdas con algo de info
                # logger.debug(f"[DOCAI-TABLES] Fila {row_idx+1} de Tabla {table_idx+1} omitida por pocas celdas con texto: {celdas_fila_actual}")
                continue

            for cell_idx, cell_text_raw in enumerate(celdas_fila_actual):
                field_name_mapped = column_to_field_map.get(cell_idx)
                if field_name_mapped:
                    cell_text_limpio = limpiar_texto_base(cell_text_raw)
                    # Manejar celdas combinadas o texto multilínea en una celda
                    if field_name_mapped in producto_candidato and producto_candidato[field_name_mapped] and cell_text_limpio:
                        producto_candidato[field_name_mapped] += " " + cell_text_limpio 
                    elif cell_text_limpio:
                        producto_candidato[field_name_mapped] = cell_text_limpio
            
            nombre_tabla = limpiar_texto_base(str(producto_candidato.get("nombre", producto_candidato.get("descripcion", ""))))[:250]
            sku_tabla = limpiar_texto_base(str(producto_candidato.get("sku", "")))[:100]

            if not nombre_tabla and not sku_tabla: continue 
            if not nombre_tabla and sku_tabla: nombre_tabla = sku_tabla 

            producto_final_tabla = {"user_id": pyme_user_id, "nombre": nombre_tabla, "sku": sku_tabla}
            producto_final_tabla["descripcion"] = limpiar_texto_base(str(producto_candidato.get("descripcion", "")))[:1000]
            if producto_final_tabla["descripcion"] == nombre_tabla: producto_final_tabla["descripcion"] = ""

            precio_s, precio_f, moneda = parse_precio_flexible(producto_candidato.get("precio_str"))
            producto_final_tabla["precio_str"] = precio_s if precio_s else ""
            producto_final_tabla["precio_float"] = precio_f
            producto_final_tabla["moneda"] = moneda if moneda else "ARS"
            
            producto_final_tabla["unidad"] = limpiar_texto_base(str(producto_candidato.get("unidad", "")))[:50]
            producto_final_tabla["categoria"] = limpiar_texto_base(str(producto_candidato.get("categoria", pyme_rubro_nombre)))[:100]
            producto_final_tabla["marca"] = limpiar_texto_base(str(producto_candidato.get("marca", "")))[:100]
            producto_final_tabla["cantidad_disponible"] = "1" # Default si no hay columna de stock

            productos_de_tablas.append(producto_final_tabla)
            logger.info(f"[DOCAI-TABLES] Producto de tabla agregado: {nombre_tabla[:50]}")
            
    if productos_de_tablas:
         logger.info(f"[DOCAI-TABLES] Se extrajeron {len(productos_de_tablas)} productos del análisis de tablas del PDF.")
    return productos_de_tablas

def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    # ... (Tu función extraer_info_producto_de_linea_pdf como te la pasé en @‶gANVneHZLGZj...)
    # Asegúrate de que esté completa y sea la versión robusta.
    # Copia aquí la versión de la respuesta @‶gANVneHZLGZj...
    linea_limpia = limpiar_texto_base(linea_procesar)
    if not linea_limpia or len(linea_limpia) < 4: return None
    precio_regex = r"((?:[\$\€\£]?\s*\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?)|(?:\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?\s*[\$\€\£]?))\b(?:\s*(ARS|USD|EUR|GBP|CLP))?"
    codigo_pattern = re.compile(r"(\b(?:ART|COD|REF|SKU|ID|ITEM|NRO)\.?\s*[:\-]?\s*[\w\d\/-]+)\b|(\b\d{4,}[\w\d-]*\b)", re.IGNORECASE)
    texto_principal = linea_limpia; texto_precio_crudo = None; moneda_explicita_en_linea = None
    best_price_match = None
    for match_p in re.finditer(precio_regex, linea_limpia, re.IGNORECASE):
        if re.search(r"\d", match_p.group(1)): best_price_match = match_p
    if best_price_match:
        texto_precio_crudo = limpiar_texto_base(best_price_match.group(1)); moneda_explicita_en_linea = limpiar_texto_base(best_price_match.group(2).upper()) if best_price_match.group(2) else None
        inicio_match, fin_match = best_price_match.span(); parte_antes = linea_limpia[:inicio_match].strip(); parte_despues = linea_limpia[fin_match:].strip()
        if len(parte_antes) > len(parte_despues) and len(parte_antes) >= 3: texto_principal = parte_antes
        elif len(parte_despues) >= 3: texto_principal = parte_despues
        elif parte_antes: texto_principal = parte_antes
        elif parte_despues: texto_principal = parte_despues
        else: texto_principal = linea_limpia.replace(best_price_match.group(0), "").strip()
        if not texto_principal.strip() and texto_precio_crudo: logger.debug(f"[DOCAI-LINE] Descartando, parece solo precio: '{linea_limpia}'"); return None
    precio_s, precio_f, mon_p = parse_precio_flexible(texto_precio_crudo); mon_f = moneda_explicita_en_linea if moneda_explicita_en_linea else (mon_p if mon_p else "ARS")
    if not texto_principal.strip(): return None
    texto_nombre_cand = texto_principal; unidad_f, tipo_p_f = extraer_unidades_y_tipos_precio(texto_nombre_cand, pyme_rubro_nombre)
    if unidad_f: texto_nombre_cand = limpiar_texto_base(re.sub(r'(?i)\b' + re.escape(unidad_f) + r'\b', '', texto_nombre_cand, 1))
    if tipo_p_f: texto_nombre_cand = limpiar_texto_base(re.sub(r'(?i)\b' + re.escape(tipo_p_f) + r'\b', '', texto_nombre_cand, 1))
    match_cod = codigo_pattern.search(texto_nombre_cand); cod_art_ext = ""
    if match_cod:
        cod_raw = match_cod.group(1) if match_cod.group(1) else match_cod.group(2); cod_limpio = limpiar_texto_base(cod_raw); es_cod = True
        if cod_limpio.isdigit():
            if len(cod_limpio) == 4 and (1900 < int(cod_limpio) < 2100): es_cod = False
            elif len(cod_limpio) > 7 and not any(p in cod_raw.upper() for p in ["ART","COD","REF","SKU"]): es_cod = False
        if es_cod: cod_art_ext = cod_limpio; texto_nombre_cand = limpiar_texto_base(texto_nombre_cand.replace(cod_raw, "", 1))
    nombre_final_prod = limpiar_texto_base(texto_nombre_cand); desc_final = ""
    if texto_principal != nombre_final_prod and len(texto_principal) > len(nombre_final_prod) + 3:
        desc_final = texto_principal; 
        if limpiar_texto_base(desc_final) == nombre_final_prod: desc_final = ""
    if not nombre_final_prod or len(nombre_final_prod) < 3: return None
    if nombre_final_prod.isdigit() and precio_f is None: return None
    pal_evitar = ["total","subtotal","iva","fecha","pagina","página","cliente","pedido","factura","contacto","teléfono","email","observaciones","referencia","lista de precios","descripción","articulo","precio","código"]
    if len(nombre_final_prod.split()) <= 4 and any(p_ev in nombre_final_prod.lower() for p_ev in pal_evitar) and precio_f is None:
        logger.debug(f"[DOCAI-LINE] Descarte por palabra irrelevante en nombre corto sin precio: '{linea_limpia}' -> '{nombre_final_prod}'")
        return None
    nombre_como_precio_info = parse_precio_flexible(nombre_final_prod)
    if nombre_como_precio_info[1] is not None and precio_f is None: logger.debug(f"[DOCAI-LINE] Descarte (nombre parece solo precio): '{linea_limpia}' -> '{nombre_final_prod}'"); return None
    return {"nombre": nombre_final_prod[:250], "descripcion": desc_final[:500] if desc_final else "", "precio_str": precio_s if precio_s else "", "precio_float": precio_f, "moneda": mon_f if mon_f else "ARS", "unidad": unidad_f[:50] if unidad_f else "", "tipo_precio": tipo_p_f[:50] if tipo_p_f else "", "codigo_articulo": cod_art_ext[:100], "categoria": ""}


def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    logger.info(f"[DOCAI-MAIN] Iniciando procesamiento PDF: {os.path.basename(pdf_path)} para User ID: {user_id}")
    document = _obtener_documento_ai(pdf_path)
    if not document: 
        logger.error(f"[DOCAI-MAIN] No se pudo obtener el objeto Document para {os.path.basename(pdf_path)}")
        return [] 
    
    productos_extraidos_final: List[Dict[str, Any]] = []
    
    # Priorizar tablas si existen y la lógica está implementada
    if hasattr(document, 'tables') and document.tables: # Chequeo más seguro
        logger.info(f"[DOCAI-MAIN] Document AI encontró {len(document.tables)} atributos 'tables'. Intentando procesarlas...")
        productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
        if productos_de_tablas: 
            productos_extraidos_final.extend(productos_de_tablas)
            logger.info(f"[DOCAI-MAIN] Extracción de tablas aportó {len(productos_de_tablas)} productos.")
    else:
        logger.info(f"[DOCAI-MAIN] PDF '{os.path.basename(pdf_path)}' no tiene el atributo 'tables' o está vacío.")

    # Procesar texto línea por línea como fallback o si no hay tablas (o para complementar)
    # Decidir si se procesa líneas solo si las tablas no dieron nada, o siempre y luego de-duplicar.
    if not productos_extraidos_final and document.text: 
        logger.info(f"[DOCAI-MAIN] No se extrajeron productos de tablas (o no habían). Procesando líneas del PDF...")
        lineas = document.text.split('\n'); count_line_prods = 0
        for linea_idx, linea_cruda in enumerate(lineas):
            linea_strip = linea_cruda.strip()
            if len(linea_strip) < 5: continue # Ignorar líneas muy cortas
            
            # logger.debug(f"[DOCAI-MAIN] Procesando línea {linea_idx + 1}: '{linea_strip[:100]}'")
            prod = extraer_info_producto_de_linea_pdf(linea_strip, pyme_rubro_nombre)
            if prod: 
                prod["user_id"] = user_id
                # Lógica anti-duplicados simple (si ya procesaste tablas)
                # ya_existe = any(p.get("nombre","").lower() == prod.get("nombre","").lower() for p in productos_extraidos_final if p.get("nombre") and prod.get("nombre"))
                # if not ya_existe:
                #     productos_extraidos_final.append(prod)
                #     count_line_prods += 1
                # else:
                #     logger.debug(f"[DOCAI-MAIN] Producto de línea duplicado (ya en tablas): {prod.get('nombre')}")
                productos_extraidos_final.append(prod) # Añadir directamente por ahora
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