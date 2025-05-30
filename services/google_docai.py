# En tu archivo: services/google_docai.py

import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account

# --- Carga de Credenciales (tu código existente) ---
# ... (asegúrate que tus credenciales se carguen correctamente y logging esté importado)
# Ejemplo de importación y carga básica de credenciales:
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


def limpiar_texto_simple(texto: str) -> str:
    if not texto: return ""
    return re.sub(r'\s+', ' ', texto).strip()

def normalizar_precio_str(precio_str: str) -> str:
    """Limpia y normaliza un string de precio a 'DDDD.CC' o devuelve vacío."""
    if not precio_str: return ""
    # Quitar símbolo de moneda y espacios iniciales/finales
    precio = precio_str.replace("$", "").strip()
    # Quitar separadores de miles (puntos en formato ARS como 1.234,56)
    # Esta lógica asume que la coma es el separador decimal si está presente.
    if ',' in precio and '.' in precio: # Ej: 1.234,56 o 1,234.56
        if precio.rfind(',') > precio.rfind('.'): # Coma es decimal (1.234,56)
            precio = precio.replace('.', '') # Queda 1234,56
            precio = precio.replace(',', '.') # Queda 1234.56
        else: # Punto es decimal (1,234.56)
            precio = precio.replace(',', '') # Queda 1234.56
    elif ',' in precio: # Solo comas, asumir que la última es decimal (1234,56)
        precio = precio.replace(',', '.')
    # Si solo hay puntos, o ninguno, se asume que el punto es decimal o es entero.
    # Validar que sea un formato numérico válido antes de devolver
    if re.fullmatch(r"\d+(\.\d{1,2})?", precio):
        return precio
    return "" # Devolver vacío si no se pudo normalizar a un formato esperado

def extraer_precio_float_desde_normalizado(precio_normalizado_str: str) -> float | None:
    if not precio_normalizado_str: return None
    try:
        return float(precio_normalizado_str)
    except ValueError:
        return None

def procesar_catalogo_pdf_google(pdf_path, tipo_catalogo="generico", pyme_user_id=None):
    try:
        project_id = "ambient-stack-461118-k7"
        location = "us"
        processor_id = "55c57b09a179531a"

        client = documentai.DocumentProcessorServiceClient(credentials=credentials)
        name = f"projects/{project_id}/locations/{location}/processors/{processor_id}"

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)
        result = client.process_document(request=request)
        document = result.document
        
        logging.info(f"📝 Documento '{os.path.basename(pdf_path)}' (PYME ID {pyme_user_id}) procesado: {len(document.pages)} pág. Tipo: {tipo_catalogo}. Analizando texto...")
        productos_extraidos = []

        # --- INICIO DEL PROCESAMIENTO LÍNEA POR LÍNEA MEJORADO ---
        # Esta es una aproximación genérica. Para mayor precisión, especialmente con tablas,
        # necesitarías procesar document.tables o crear heurísticas más específicas por 'tipo_catalogo'.

        # Regex para precios: busca patrones como $1.234,56 o 1234.56 o $1234 etc.
        # Captura el grupo numérico principal.
        precio_regex = r"(?:\$|\bARS\b)?\s*([\d.,]+(?:\.\d{2}|,\d{2})?|\d+)"

        for page_num, page in enumerate(document.pages): # Iterar por página puede ayudar con el contexto
            logging.debug(f"Procesando página {page_num + 1}")
            # El texto de la página ya viene ordenado por Document AI
            # (o puedes usar document.text para todo el documento a la vez)
            lineas_pagina = page.lines 
            # Si page.lines no está disponible o es problemático, usa:
            # texto_pagina = document.text[page.layout.text_anchor.text_segments[0].start_index:page.layout.text_anchor.text_segments[0].end_index]
            # lineas_pagina_texto = texto_pagina.split('\n')

            for line_obj in lineas_pagina: # Asumiendo que line_obj tiene una forma de obtener su texto
                # Extraer el texto de la línea usando el text_anchor
                line_text = ""
                if line_obj.layout.text_anchor and line_obj.layout.text_anchor.text_segments:
                    for segment in line_obj.layout.text_anchor.text_segments:
                        line_text += document.text[segment.start_index:segment.end_index]
                
                linea_limpia = limpiar_texto_simple(line_text)

                if not linea_limpia or len(linea_limpia) < 4: # Ignorar líneas muy cortas
                    continue

                nombre_producto = linea_limpia # Default inicial
                descripcion_producto = linea_limpia # Default inicial
                precio_str_final = ""
                precio_float_final = None

                # Encontrar todos los candidatos a precio en la línea
                candidatos_precio_match = list(re.finditer(precio_regex, linea_limpia))

                if candidatos_precio_match:
                    # Heurística: el precio "principal" suele ser el último en la línea,
                    # o el que está más a la derecha en listas de precios.
                    match_precio_elegido = candidatos_precio_match[-1] # Tomar el último
                    precio_texto_crudo = match_precio_elegido.group(1) # El grupo que captura el número
                    
                    precio_str_final = normalizar_precio_str(precio_texto_crudo)
                    if precio_str_final:
                        precio_float_final = extraer_precio_float_desde_normalizado(precio_str_final)

                        # Intentar aislar el nombre del producto quitando el precio y lo que le sigue
                        # Esto es delicado y depende de la estructura.
                        inicio_span_precio, fin_span_precio = match_precio_elegido.span(1) # Posición del número del precio
                        
                        # Considerar el texto a la izquierda del precio como el nombre/descripción
                        texto_antes_de_precio = linea_limpia[:match_precio_elegido.start()].strip()
                        
                        if texto_antes_de_precio:
                            nombre_producto = texto_antes_de_precio
                            descripcion_producto = texto_antes_de_precio
                        else: # Si no hay nada antes, el precio estaba al inicio (raro para nombre)
                              # o la línea solo contenía el precio.
                              # Intentar tomar la línea completa sin el precio.
                            nombre_producto = limpiar_texto_simple(linea_limpia.replace(match_precio_elegido.group(0), ""))
                            if not nombre_producto: # Si al quitar el precio no queda nada, es una línea de solo precio
                                logging.debug(f"Línea parece ser solo un precio, se descarta como producto: '{linea_limpia}'")
                                continue
                else: # No se encontró precio, la línea podría ser un nombre de producto o encabezado
                    nombre_producto = linea_limpia
                    descripcion_producto = linea_limpia
                    logging.debug(f"No se encontró precio con regex en: '{linea_limpia}'")


                # --- Filtros y Heurísticas Adicionales ---
                # 1. Descartar si parece un encabezado muy obvio y no tiene precio
                palabras_encabezado_comunes = ["ART.", "CODIGO", "DESCRIPCION", "TALLE", "PRECIO", "MARCA", "LINEA", "TOTAL", "SUBTOTAL", "CANTIDAD"]
                # Convertir nombre_producto a mayúsculas para la comparación
                nombre_producto_upper = nombre_producto.upper()
                es_posible_encabezado = any(palabra.upper() in nombre_producto_upper for palabra in palabras_encabezado_comunes)
                
                if es_posible_encabezado and not precio_float_final and len(nombre_producto.split()) < 5:
                    logging.debug(f"Línea descartada (posible encabezado sin precio): '{linea_limpia}'")
                    continue
                
                # 2. Si después de todo el nombre es muy corto o genérico, y no hay precio, descartar.
                if not precio_float_final and (len(nombre_producto) < 5 or nombre_producto.isdigit()):
                    logging.debug(f"Línea descartada (nombre corto/numérico sin precio): '{linea_limpia}'")
                    continue
                
                # 3. Validar que el nombre no sea solo un precio (si la regex de precio falló en aislarlo antes)
                if normalizar_precio_str(nombre_producto) == nombre_producto and not precio_float_final : # Si el nombre es en sí un precio
                     logging.debug(f"Línea descartada (nombre es solo un precio): '{linea_limpia}'")
                     continue


                # --- Añadir producto ---
                # Solo añadir si tenemos un nombre y preferiblemente un precio, o si el nombre es suficientemente descriptivo
                if nombre_producto and (precio_float_final is not None or len(nombre_producto) > 10): # Umbral de longitud para nombres sin precio
                    productos_extraidos.append({
                        "user_id": pyme_user_id if pyme_user_id is not None else 0,
                        "nombre": nombre_producto[:250],
                        "descripcion": descripcion_producto[:500],
                        "precio_str": precio_str_final if precio_str_final else "", # Asegurar string vacío si no hay precio
                        "precio_float": precio_float_final, # Puede ser None
                        "moneda": "ARS", # Asumir ARS, o intentar detectarlo si es posible
                        "texto_para_embedding": f"{nombre_producto} {descripcion_producto} {precio_str_final if precio_str_final else ''}"
                    })
                    logging.debug(f"Producto candidato: Nombre='{nombre_producto}', PrecioStr='{precio_str_final}', PrecioFloat={precio_float_final}")
                else:
                    logging.debug(f"Producto descartado por falta de nombre/precio o nombre corto: '{linea_limpia}'")


        logging.info(f"Total de productos potencialmente extraídos del PDF ({tipo_catalogo}): {len(productos_extraidos)}")
        return productos_extraidos

    except Exception as e:
        logging.error(f"❌ Error fatal procesando catálogo con Google Doc AI ({os.path.basename(pdf_path)}): {e}", exc_info=True)
        return []