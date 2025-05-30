# En tu archivo: services/google_docai.py

import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai # type: ignore
from google.oauth2 import service_account # type: ignore

# --- Carga de Credenciales ---
# (Tu código de carga de credenciales se mantiene igual, asegúrate que logging esté importado al inicio del archivo)
if 'logging' not in globals(): # Solo para asegurar que logging esté disponible
    import logging
if 'google.oauth2' not in globals() or 'service_account' not in globals()['google.oauth2'].__dict__:
    logging.warning("google.oauth2.service_account no parece estar disponible globalmente como se esperaba.")
try:
    ruta_render = "/etc/secrets/google_service_key.json"
    ruta_local = "instance/google-credentials.json" # Para desarrollo local
    ruta_cred = ruta_render if os.path.exists(ruta_render) else ruta_local
    with open(ruta_cred, "r") as f:
        credentials_info = json.load(f)
    credentials = service_account.Credentials.from_service_account_info(credentials_info)
    logging.info(f"Credenciales de Google cargadas desde: {ruta_cred}")
except FileNotFoundError:
    logging.error(f"❌ Archivo de credenciales de Google no encontrado en {ruta_render} ni en {ruta_local}.")
    raise RuntimeError(f"Archivo de credenciales no encontrado. Verifica las rutas.")
except Exception as e:
    logging.error(f"❌ Error crítico al cargar credenciales de Google: {e}")
    raise RuntimeError(f"❌ Error al cargar credenciales: {e}")
# --- Fin Carga de Credenciales ---

def limpiar_texto_pdf(texto: str) -> str:
    if not texto: return ""
    # Eliminar múltiples espacios y tabulaciones, pero conservar saltos de línea intencionales (párrafos)
    # Esta limpieza es genérica, puede necesitar ajustes.
    texto = re.sub(r'[ \t]+', ' ', texto) # Reemplaza múltiples espacios/tabs con uno solo
    texto = texto.strip() # Quita espacios al inicio/final
    return texto

def normalizar_y_extraer_precio(texto_precio: str) -> tuple[str | None, float | None, str | None]:
    """
    Intenta extraer y normalizar un precio de un string.
    Devuelve: (precio_str_formateado, precio_float, moneda_detectada)
    Ej: "$ 1.250,50 ARS" -> ("1250.50", 1250.50, "ARS")
    Ej: "USD 50.99" -> ("50.99", 50.99, "USD")
    Ej: "1200" -> ("1200.00", 1200.00, None)
    """
    if not texto_precio:
        return None, None, None

    texto_precio_limpio = texto_precio.strip()
    moneda_detectada = None

    # Detectar moneda
    if "ARS" in texto_precio_limpio.upper(): moneda_detectada = "ARS"
    elif "USD" in texto_precio_limpio.upper() or "U$S" in texto_precio_limpio.upper(): moneda_detectada = "USD"
    
    # Quitar símbolos de moneda y letras para aislar el número
    numero_str = texto_precio_limpio.replace("$", "").replace("ARS", "").replace("USD", "").replace("U$S", "").strip()

    # Normalizar separadores: asumir que la última coma o punto es el decimal
    if ',' in numero_str and '.' in numero_str:
        if numero_str.rfind(',') > numero_str.rfind('.'): # Formato 1.234,56
            numero_str = numero_str.replace('.', '') # Quitar miles
            numero_str = numero_str.replace(',', '.') # Coma a punto decimal
        else: # Formato 1,234.56
            numero_str = numero_str.replace(',', '') # Quitar miles
    elif ',' in numero_str: # Solo comas, la última es decimal
        numero_str = numero_str.replace(',', '.')
    
    # Validar que sea un número flotante válido
    if re.fullmatch(r"\d+(\.\d{1,2})?", numero_str):
        try:
            precio_flt = float(numero_str)
            # Formatear a dos decimales para precio_str
            precio_str_fmt = f"{precio_flt:.2f}" 
            return precio_str_fmt, precio_flt, moneda_detectada
        except ValueError:
            logging.warning(f"No se pudo convertir a float: '{numero_str}' (original: '{texto_precio}')")
            return str(texto_precio_limpio), None, moneda_detectada # Devolver original si falla conversión
    else:
        logging.warning(f"Formato de precio no reconocido: '{numero_str}' (original: '{texto_precio}')")
        # Devolver el texto original del precio si no se pudo normalizar
        return str(texto_precio_limpio), None, moneda_detectada


def procesar_catalogo_pdf_google(pdf_path: str, tipo_catalogo: str = "generico", pyme_user_id: int | None = None) -> list[dict]:
    try:
        project_id = credentials.project_id # Usar project_id de las credenciales
        location = "us"  # Asegúrate que esta sea la región de tu procesador
        processor_id = "55c57b09a179531a" # Tu Processor ID

        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(client_options=client_options, credentials=credentials)
        
        name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)
        
        logging.info(f"Enviando solicitud a Document AI para: {os.path.basename(pdf_path)}")
        result = client.process_document(request=request)
        document = result.document
        
        logging.info(f"📝 Documento '{os.path.basename(pdf_path)}' (PYME ID {pyme_user_id}) procesado: {len(document.pages)} pág. Analizando texto...")
        productos_extraidos = []

        # --- ANÁLISIS DE TEXTO LÍNEA POR LÍNEA (MEJORADO) ---
        # Regex para encontrar posibles precios. Más permisiva para capturar candidatos.
        # Captura números con o sin $, con . o , como separadores, y opcionalmente con 2 decimales.
        # También captura números enteros que podrían ser precios.
        precio_regex_pattern = r"(?:\$|\bARS\b|\bUSD\b)?\s*([\d.,]+[\d])" # Captura '1.250,00', '1250,50', '50.00', '1200'

        for page_num, page in enumerate(document.pages):
            logging.debug(f"Procesando página {page_num + 1}/{len(document.pages)}")
            
            # Iterar sobre párrafos o bloques de texto puede ser más robusto que líneas individuales
            # si Document AI los agrupa bien. Por ahora, usaremos líneas.
            for line_idx, line_obj in enumerate(page.lines):
                line_text_raw = ""
                if line_obj.layout.text_anchor and line_obj.layout.text_anchor.text_segments:
                    for segment in line_obj.layout.text_anchor.text_segments:
                        line_text_raw += document.text[segment.start_index:segment.end_index]
                
                linea_procesada = limpiar_texto_pdf(line_text_raw)

                if not linea_procesada or len(linea_procesada) < 5: # Ignorar líneas muy cortas
                    continue
                
                logging.debug(f"  Línea {line_idx+1}: '{linea_procesada}'")

                nombre_producto = linea_procesada # Default inicial, se intentará refinar
                descripcion_producto = "" # Intentar encontrarla separada
                precio_str_final = ""
                precio_float_final = None
                moneda_final = "ARS" # Default
                unidad_presentacion = ""

                # Buscar todos los candidatos a precio en la línea
                candidatos_precio = list(re.finditer(precio_regex_pattern, linea_procesada))

                mejor_precio_info = (None, None, None) # (precio_str, precio_float, moneda)
                pos_inicio_mejor_precio = -1
                pos_fin_mejor_precio = -1

                if candidatos_precio:
                    # Heurística: tomar el último candidato como el más probable de ser EL precio
                    # Podrías tener lógica más compleja aquí para PDFs muy densos.
                    for match_p in reversed(candidatos_precio):
                        precio_texto_capturado = match_p.group(1)
                        p_str, p_float, p_moneda = normalizar_y_extraer_precio(precio_texto_capturado)
                        
                        if p_float is not None: # Si es un número válido
                            mejor_precio_info = (p_str, p_float, p_moneda if p_moneda else moneda_final)
                            pos_inicio_mejor_precio = match_p.start()
                            pos_fin_mejor_precio = match_p.end()
                            break # Encontramos un buen candidato

                precio_str_final, precio_float_final, moneda_final_detectada = mejor_precio_info
                if moneda_final_detectada: moneda_final = moneda_final_detectada


                # Intentar aislar el nombre del producto y la descripción
                if pos_inicio_mejor_precio != -1:
                    texto_antes_precio = limpiar_texto_pdf(linea_procesada[:pos_inicio_mejor_precio])
                    texto_despues_precio = limpiar_texto_pdf(linea_procesada[pos_fin_mejor_precio:])

                    if texto_antes_precio:
                        nombre_producto = texto_antes_precio
                        # Si hay algo después del precio, podría ser la unidad o parte de la descripción
                        if texto_despues_precio: 
                            descripcion_producto = texto_despues_precio 
                            # Intentar detectar unidades aquí si el formato lo permite
                            # ej. si texto_despues_precio es "caja x6" o "750ml"
                            match_unidad = re.search(r"^(caja x\d+|\d+ml|\d+L|unidad|par|docena|pack x\d+)\b", texto_despues_precio, re.IGNORECASE)
                            if match_unidad:
                                unidad_presentacion = match_unidad.group(0)
                                # Podrías querer remover la unidad de la descripción si es muy obvia
                                # descripcion_producto = limpiar_texto_pdf(descripcion_producto.replace(unidad_presentacion, ""))
                    else: # El precio estaba al inicio, el resto es nombre/desc
                        nombre_producto = texto_despues_precio
                        descripcion_producto = texto_despues_precio
                else: # No se encontró precio, la línea completa es nombre/descripción
                    nombre_producto = linea_procesada
                    descripcion_producto = linea_procesada
                
                # Limpieza final de nombre y descripción (quitar códigos, etc.)
                # Esto es muy dependiente del tipo de catálogo.
                if tipo_catalogo == "indumentaria_sox":
                    # Quitar "ART. XXXX" del inicio del nombre si está
                    nombre_producto = re.sub(r"^(ART\.\s*[A-Z0-9]+\s*-?\s*)", "", nombre_producto, flags=re.IGNORECASE).strip()
                elif tipo_catalogo == "bodega_salvador_patti":
                    # Podrías intentar separar MARCA y VARIETAL aquí si es posible
                    pass # Añadir lógica específica para vinos si es necesario

                # Descartar líneas que son claramente encabezados o basura
                if not nombre_producto or len(nombre_producto) < 3 : continue # Nombre muy corto
                if nombre_producto.isupper() and len(nombre_producto.split()) < 4 and not precio_float_final:
                    # Probablemente un encabezado si es todo mayúsculas, corto, y sin precio
                    logging.debug(f"Línea descartada (posible encabezado MAYUS): '{linea_procesada}'")
                    continue
                if nombre_producto.lower() in ["producto", "descripción", "precio", "cantidad", "total", "marca", "código"]:
                    logging.debug(f"Línea descartada (palabra clave de encabezado): '{linea_procesada}'")
                    continue

                productos_extraidos.append({
                    "user_id": pyme_user_id,
                    "nombre": limpiar_texto_pdf(nombre_producto)[:250],
                    "descripcion": limpiar_texto_pdf(descripcion_producto)[:500],
                    "precio_str": precio_str_final if precio_str_final else "",
                    "precio_float": precio_float_final,
                    "moneda": moneda_final,
                    "unidad": limpiar_texto_pdf(unidad_presentacion)[:50],
                    "texto_para_embedding": f"Nombre: {nombre_producto}. Descripción: {descripcion_producto}. Precio: {precio_str_final if precio_str_final else 'Consultar'}. Unidad: {unidad_presentacion if unidad_presentacion else ''}"
                })
                logging.debug(f"  -> Producto: {productos_extraidos[-1]}")

        logging.info(f"Total de productos estructurados extraídos del PDF ({tipo_catalogo}): {len(productos_extraidos)}")
        return productos_extraidos

    except Exception as e:
        logging.error(f"❌ Error fatal procesando catálogo PDF '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return []