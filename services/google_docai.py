# En tu archivo: services/google_docai.py

import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account
# from price_parser import Price # Opcional: si decides usar una librería externa para precios

# --- Carga de Credenciales ---
# (Tu código de carga de credenciales aquí, asegúrate que 'credentials' esté disponible)
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

def limpiar_texto_base(texto: str) -> str:
    if not texto: return ""
    return re.sub(r'\s+', ' ', texto).strip()

def normalizar_y_convertir_precio(precio_str: str) -> tuple[str | None, float | None, str | None]:
    """
    Intenta normalizar un string de precio, convertirlo a float e identificar la moneda.
    Devuelve: (precio_normalizado_str, precio_float, moneda_detectada)
    """
    if not precio_str:
        return None, None, None

    precio_limpio = limpiar_texto_base(precio_str)
    moneda_detectada = "ARS" # Default ARS

    # Detectar y quitar símbolos de moneda comunes, guardando la moneda
    if "USD" in precio_limpio.upper():
        moneda_detectada = "USD"
        precio_limpio = re.sub(r'(?i)USD', '', precio_limpio).strip()
    elif "ARS" in precio_limpio.upper():
        moneda_detectada = "ARS"
        precio_limpio = re.sub(r'(?i)ARS', '', precio_limpio).strip()
    elif "$" in precio_limpio:
        # Podríamos asumir ARS si solo hay $, o necesitar más contexto si se manejan múltiples monedas con $
        precio_limpio = precio_limpio.replace("$", "").strip()
        # Si el $ está al final (ej. 100$), quitarlo
        if precio_limpio.endswith('$'):
            precio_limpio = precio_limpio[:-1].strip()


    # Normalizar separadores numéricos (asumiendo patrón argentino como común, pero intentando ser flexible)
    # 1.234,56 -> 1234.56
    # 1,234.56 -> 1234.56
    # 1234.56 -> 1234.56
    # 1234,56 -> 1234.56
    # 1234    -> 1234.00 (o dejar como 1234)
    
    numero_str_normalizado = precio_limpio
    if ',' in numero_str_normalizado and '.' in numero_str_normalizado:
        if numero_str_normalizado.rfind(',') > numero_str_normalizado.rfind('.'): # Formato 1.234,56
            numero_str_normalizado = numero_str_normalizado.replace('.', '') 
            numero_str_normalizado = numero_str_normalizado.replace(',', '.')
        else: # Formato 1,234.56
            numero_str_normalizado = numero_str_normalizado.replace(',', '')
    elif ',' in numero_str_normalizado: # Formato 1234,56
        numero_str_normalizado = numero_str_normalizado.replace(',', '.')
    
    # Validar que sea un número flotante o entero válido
    if re.fullmatch(r"^\d+(\.\d+)?$", numero_str_normalizado):
        try:
            precio_flt = float(numero_str_normalizado)
            # Devolver el string original limpio (antes de convertir a float y perder formato)
            # y el float.
            return precio_limpio, precio_flt, moneda_detectada
        except ValueError:
            logging.warning(f"No se pudo convertir a float tras normalizar: '{precio_str}' -> '{numero_str_normalizado}'")
            return precio_limpio, None, moneda_detectada # Devolver el string limpio aunque no sea float
    else:
        logging.warning(f"Formato de precio no reconocido tras normalizar: '{precio_str}' -> '{numero_str_normalizado}'")
        return precio_limpio, None, moneda_detectada # Devolver el string limpio aunque no sea float reconocido


def extraer_unidades_y_tipos_precio(texto_linea: str, rubro: str = "generico") -> tuple[str | None, str | None]:
    """
    Intenta extraer unidades (ej. "caja x 6", "750ml") o tipos de precio (ej. "por mayor") de una línea.
    Devuelve: (unidad_extraida, tipo_precio_extraido)
    """
    texto_linea_lower = texto_linea.lower()
    unidad = None
    tipo_precio = None

    # Patrones comunes de unidades
    unidades_regex = {
        "caja": r"\b(caja(?:s)?\s*(?:x\s*\d{1,2})?)\b",
        "botella": r"\b(\d{3,4}ml|botella(?:s)?)\b",
        "pack": r"\b(pack\s*(?:x\s*\d{1,2})?)\b",
        "docena": r"\b(docena(?:s)?)\b",
        "par": r"\b(par(?:es)?)\b",
        "unidad_simple": r"\b(u\.?|unid(?:ad|ades)?)\b",
        "litro": r"\b(\d{1,2}\s*lts?)\b",
        "kilo": r"\b(kg|kilo(?:s)?)\b"
    }
    # Palabras clave para tipos de precio
    tipos_precio_keywords = {
        "mayorista": ["mayorista", "por mayor", "distribuidor"],
        "minorista": ["minorista", "al detalle", "público"],
        "promocion": ["promo", "oferta", "descuento"]
    }

    for tipo, patron in unidades_regex.items():
        match = re.search(patron, texto_linea_lower)
        if match:
            unidad = limpiar_texto_base(match.group(1))
            break # Tomar la primera unidad encontrada

    for tipo, keywords in tipos_precio_keywords.items():
        if any(kw in texto_linea_lower for kw in keywords):
            tipo_precio = tipo
            break
            
    return unidad, tipo_precio


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
        request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document)
        result = client.process_document(request=request)
        document = result.document
        
        logging.info(f"📝 Documento '{os.path.basename(pdf_path)}' (PYME ID {pyme_user_id}) procesado: {len(document.pages)} pág. Rubro: {pyme_rubro_nombre}. Analizando texto...")
        productos_extraidos = []

        # --- PROCESAMIENTO DE TEXTO LÍNEA POR LÍNEA ---
        # (El análisis de tablas sería una mejora adicional aquí)
        
        # Regex general para precios, un poco más permisiva y captura el texto completo del precio.
        # Intenta capturar "$ 1.234,56" o "1234.56" o "ARS 500" etc.
        # El grupo 1 es el importante (el número y sus símbolos).
        precio_pattern = re.compile(r"((?:\$|\bARS\b|\bUSD\b)?\s*[\d.,]+(?:[.,]\d{2})?)")
        
        # Regex para códigos de artículo comunes (ej. ART. XXX, COD 123) - para ayudar a separar nombres
        codigo_pattern = re.compile(r"(\b(?:ART|COD|REF|SKU)\.?\s*[\w\d/-]+)", re.IGNORECASE)

        texto_completo_doc = document.text # Usar el texto completo para contexto si es necesario

        for page_num, page in enumerate(document.pages):
            # Document AI puede dar 'lines' que a veces son fragmentos.
            # Puede ser mejor reconstruir "párrafos" o bloques de texto lógicos.
            # Por ahora, seguimos con la idea de líneas, pero usando page.text.
            # Si page.lines es más fiable en tu procesador, ajústalo.
            # Esta es una simplificación, el layout real es más complejo.
            
            # Obtener texto de la página si page.lines no es lo ideal
            page_text_anchor = page.layout.text_anchor
            page_text = document.text[page_text_anchor.text_segments[0].start_index : page_text_anchor.text_segments[0].end_index]
            lineas_de_pagina = page_text.split('\n')

            for linea_original in lineas_de_pagina:
                linea = limpiar_texto_base(linea_original)
                if not linea or len(linea) < 5 : # Ignorar líneas muy cortas o basura
                    continue

                nombre_prod = linea
                desc_prod = "" # Empezar con descripción vacía
                precio_str_norm = None
                precio_flt = None
                moneda = "ARS" # Default
                unidad_ext = None
                tipo_precio_ext = None

                # 1. Extraer todos los precios candidatos de la línea
                precios_encontrados_match = list(precio_pattern.finditer(linea))
                
                texto_sin_precios = linea # Texto restante después de quitar precios
                precios_info = [] # Guardar (texto_precio, pos_inicio)

                if precios_encontrados_match:
                    for match_p in precios_encontrados_match:
                        precio_texto_crudo = match_p.group(1)
                        precios_info.append((precio_texto_crudo, match_p.start()))
                        # Eliminar el precio del texto para ayudar a aislar el nombre
                        texto_sin_precios = texto_sin_precios.replace(match_p.group(0), " [PRECIO] ", 1) 
                    
                    # Heurística: tomar el último precio como el principal, o el más a la derecha
                    # (Asumimos que los precios están al final de la descripción del producto)
                    if precios_info:
                        precio_texto_crudo_principal, _, moneda_detectada = normalizar_y_convertir_precio(precios_info[-1][0])
                        if precio_texto_crudo_principal: # Si la normalización tuvo éxito
                           precio_str_norm = precio_texto_crudo_principal
                           precio_flt = extraer_precio_float_desde_normalizado(precio_str_norm) # Usa el string ya normalizado
                           if moneda_detectada: moneda = moneda_detectada
                
                texto_sin_precios = limpiar_texto_base(texto_sin_precios.replace("[PRECIO]", ""))

                # 2. Intentar extraer códigos de artículo
                codigo_match = codigo_pattern.search(texto_sin_precios)
                codigo_articulo = ""
                if codigo_match:
                    codigo_articulo = limpiar_texto_base(codigo_match.group(1))
                    # Quitar el código del texto para aislar mejor el nombre/descripción
                    texto_sin_precios_ni_codigo = limpiar_texto_base(texto_sin_precios.replace(codigo_articulo, ""))
                    if texto_sin_precios_ni_codigo : # Si queda algo, ese es el nombre/desc
                        nombre_prod = texto_sin_precios_ni_codigo
                    else: # Si solo había código y precios, el código puede ser parte del nombre
                        nombre_prod = codigo_articulo
                else:
                    nombre_prod = texto_sin_precios # Lo que queda después de quitar precios

                # 3. Extraer unidades y tipos de precio del texto restante o de la línea original
                unidad_ext, tipo_precio_ext = extraer_unidades_y_tipos_precio(linea, pyme_rubro_nombre)
                if unidad_ext and unidad_ext in nombre_prod: # Evitar duplicar unidad en nombre
                    nombre_prod = limpiar_texto_base(nombre_prod.replace(unidad_ext, ""))
                
                # Si el nombre es muy genérico y hay un código, usar el código
                if not nombre_prod and codigo_articulo:
                    nombre_prod = codigo_articulo
                
                # Asignar descripción
                if nombre_prod != linea_limpia : # Si logramos aislar un nombre
                    desc_prod = nombre_prod # Por ahora, o podrías intentar buscar más contexto
                else: # Si el nombre sigue siendo la línea completa (menos precios), no hay desc separada
                    desc_prod = "" 
                    # Si no hay precio y el nombre es la línea, podría ser un encabezado.
                    if not precio_flt and len(nombre_prod.split()) < 6: # Heurística para descartar posibles encabezados
                        palabras_encabezado_comunes = ["ARTICULO", "CODIGO", "DESCRIPCION", "TALLE", "PRECIO", "MARCA", "LINEA", "TOTAL", "SUBTOTAL", "CANTIDAD"]
                        if any(palabra.upper() in nombre_prod.upper() for palabra in palabras_encabezado_comunes):
                            logging.debug(f"Línea descartada como encabezado: '{linea_limpia}'")
                            continue
                
                # Chequeo final de calidad
                if not nombre_prod or len(nombre_prod) < 3: # Nombre muy corto
                    logging.debug(f"Producto descartado (nombre corto o inválido): '{linea_limpia}' -> Nombre procesado: '{nombre_prod}'")
                    continue
                if not precio_flt and len(nombre_prod) < 10: # Si no hay precio, el nombre debe ser más descriptivo
                    logging.debug(f"Producto descartado (nombre corto sin precio): '{nombre_prod}'")
                    continue


                productos_extraidos.append({
                    "user_id": pyme_user_id if pyme_user_id is not None else 0,
                    "nombre": nombre_prod[:250],
                    "descripcion": desc_prod[:500] if desc_prod else nombre_prod[:500], # Usar nombre si no hay desc
                    "precio_str": precio_str_norm if precio_str_norm else "",
                    "precio_float": precio_flt, # Puede ser None
                    "moneda": moneda,
                    "unidad": unidad_ext if unidad_ext else "",
                    "tipo_precio": tipo_precio_ext if tipo_precio_ext else "", # ej. mayorista
                    "texto_para_embedding": f"{nombre_prod} {desc_prod} {unidad_ext if unidad_ext else ''} {precio_str_norm if precio_str_norm else ''}".strip()
                })
                logging.debug(f"Extracción Línea: Orig='{linea_limpia}' -> Prod='{nombre_prod}', Precio='{precio_str_norm}', Unidad='{unidad_ext}'")

        logging.info(f"Total de productos potencialmente extraídos del PDF ({os.path.basename(pdf_path)}): {len(productos_extraidos)}")
        return productos_extraidos

    except Exception as e:
        logging.error(f"❌ Error fatal procesando catálogo PDF ({os.path.basename(pdf_path)}): {e}", exc_info=True)
        return []