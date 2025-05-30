# En tu archivo: services/google_docai.py

import os
import json
import logging  # <--- AÑADIDO: Importar logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account

# --- Carga de Credenciales (tu código existente, asegúrate que esté correcto) ---
ruta_render = "/etc/secrets/google_service_key.json"
ruta_local = "instance/google-credentials.json" # Asegúrate que esta ruta sea correcta para tu desarrollo local si aplica
try:
    ruta_cred = ruta_render if os.path.exists(ruta_render) else ruta_local
    with open(ruta_cred, "r") as f:
        credentials_info = json.load(f)
    credentials = service_account.Credentials.from_service_account_info(credentials_info)
    logging.info(f"Credenciales de Google cargadas desde: {ruta_cred}")
except FileNotFoundError:
    logging.error(f"❌ Archivo de credenciales de Google no encontrado en {ruta_render} ni en {ruta_local}.")
    # Considera si quieres que la aplicación falle aquí o continúe con Document AI deshabilitado.
    # Por ahora, la siguiente llamada a Document AI fallará si 'credentials' no está definido.
    # Es mejor que falle pronto si las credenciales son esenciales.
    raise RuntimeError(f"Archivo de credenciales no encontrado. La aplicación no puede iniciar sin ellas si Document AI es esencial.")
except Exception as e:
    logging.error(f"❌ Error crítico al cargar credenciales de Google desde {ruta_cred}: {e}")
    raise RuntimeError(f"❌ Error al cargar credenciales desde {ruta_cred}: {e}")
# --- Fin Carga de Credenciales ---

def limpiar_texto(texto: str) -> str:
    if not texto: return ""
    return re.sub(r'\s+', ' ', texto).strip()

def extraer_precio_float(precio_str: str) -> float | None:
    if not precio_str: return None
    try:
        precio_limpio = precio_str.replace("$", "").strip()
        if ',' in precio_limpio and '.' in precio_limpio:
            if precio_limpio.rfind(',') > precio_limpio.rfind('.'):
                precio_limpio = precio_limpio.replace('.', '').replace(',', '.')
            else:
                precio_limpio = precio_limpio.replace(',', '')
        elif ',' in precio_limpio:
            partes = precio_limpio.split(',')
            if len(partes) > 1:
                parte_entera = "".join(partes[:-1])
                parte_decimal = partes[-1]
                precio_limpio = f"{parte_entera}.{parte_decimal}"
            else:
                precio_limpio = precio_limpio.replace(',', '.')
        
        precio_final_str = "".join(c for c in precio_limpio if c.isdigit() or c == '.')
        if precio_final_str.count('.') > 1:
            partes = precio_final_str.split('.')
            precio_final_str = partes[0] + '.' + "".join(partes[1:])
        
        return float(precio_final_str)
    except ValueError:
        logging.warning(f"No se pudo convertir el precio '{precio_str}' a float.")
        return None
    except Exception as e:
        logging.error(f"Error inesperado al convertir precio '{precio_str}': {e}")
        return None

def procesar_catalogo_pdf_google(pdf_path, tipo_catalogo="generico", pyme_user_id=None): # Añadido pyme_user_id
    try:
        # Estas son las configuraciones de tu procesador de Document AI
        project_id = "ambient-stack-461118-k7"  # De tus logs y google_service_key.json
        location = "us"  # O la región correcta de tu procesador
        processor_id = "55c57b09a179531a" # De tu código anterior

        # Inicializar el cliente de Document AI
        client = documentai.DocumentProcessorServiceClient(credentials=credentials)
        name = f"projects/{project_id}/locations/{location}/processors/{processor_id}"

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)

        # --- ASEGÚRATE QUE ESTA LÍNEA ESTÉ DESCOMENTADA Y ACTIVA ---
        result = client.process_document(request=request)
        # ----------------------------------------------------------
        
        document = result.document # Ahora 'result' está definido
        
        logging.info(f"📝 Documento '{os.path.basename(pdf_path)}' para PYME ID {pyme_user_id} procesado. {len(document.pages)} páginas. Tipo catálogo: {tipo_catalogo}. Procesando texto...")
        productos_extraidos = []

        # --- Lógica de Procesamiento de Texto Plano Línea por Línea (MEJORADO) ---
        # (Aquí va la lógica de extracción mejorada que te proporcioné en el mensaje anterior,
        # la que incluye limpiar_texto, regex_precio, y el bucle sobre lineas_documento.
        # Por brevedad, no la repito toda aquí, pero es la sección que comienza con:
        # logging.info("Procesando texto plano línea por línea con heurísticas mejoradas...")
        # y termina con:
        # logging.info(f"Total de productos potencialmente extraídos del texto plano: {len(productos_extraidos)}")
        # Asegúrate de que esa lógica esté aquí.)

        # --- INICIO DE LA LÓGICA DE EXTRACCIÓN QUE ESTABA EN EL MENSAJE ANTERIOR ---
        logging.info("Procesando texto plano línea por línea con heurísticas mejoradas...")
        lineas_documento = document.text.split('\n')
        
        regex_precio = r"\$?\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{2}))|\$?\s*(\d+)"

        for i, linea_original in enumerate(lineas_documento):
            linea = limpiar_texto(linea_original)
            if not linea or len(linea) < 5: 
                continue

            posibles_precios_match = list(re.finditer(regex_precio, linea))
            
            nombre_producto = linea 
            descripcion_producto = linea 
            precio_str_final = ""
            precio_float_final = None

            if posibles_precios_match:
                mejor_candidato_precio_str = ""
                for match_p in reversed(posibles_precios_match): 
                    precio_capturado_grupo1 = match_p.group(1) 
                    precio_capturado_grupo2 = match_p.group(2) 
                    if precio_capturado_grupo1:
                        mejor_candidato_precio_str = precio_capturado_grupo1
                        break 
                    elif precio_capturado_grupo2 and not mejor_candidato_precio_str:
                        mejor_candidato_precio_str = precio_capturado_grupo2
                
                if mejor_candidato_precio_str:
                    precio_str_final = limpiar_texto(mejor_candidato_precio_str)
                    precio_float_final = extraer_precio_float(precio_str_final)
                    pos_precio_en_linea = linea.rfind(mejor_candidato_precio_str.strip())
                    if pos_precio_en_linea != -1:
                        nombre_producto = limpiar_texto(linea[:pos_precio_en_linea])
                    else: 
                        nombre_producto = limpiar_texto(linea.replace(mejor_candidato_precio_str, ""))
                    if len(nombre_producto) < 4 and len(linea) > len(mejor_candidato_precio_str) + 5:
                         nombre_producto = limpiar_texto(linea)
                    descripcion_producto = nombre_producto
            else:
                nombre_producto = linea
                descripcion_producto = linea
            
            palabras_clave_descarte = ["MARCA", "VARIETAL", "UN/CAJA", "CAJA/PALLET", "PRECIO", "TALLE", "EMPAQUETADO", "LINEA", "COLECCIÓN", "LISTA DE PRECIOS"]
            # Convertir nombre_producto a mayúsculas para la comparación de descarte
            nombre_producto_upper = nombre_producto.upper()
            if any(palabra_clave.upper() in nombre_producto_upper for palabra_clave in palabras_clave_descarte) and not precio_float_final :
                # Si la línea es muy corta Y contiene una palabra clave de encabezado, es más probable que sea un encabezado
                if len(nombre_producto) < 30 : 
                    logging.debug(f"Línea descartada como posible encabezado (corta y con palabra clave): '{linea}'")
                    continue
                # Si la línea es más larga, pero NO tiene un precio y SÍ una palabra clave, también podría ser un encabezado de sección
                # Esta heurística puede necesitar más ajustes.
                # Podríamos también verificar si la línea es completamente en mayúsculas, etc.
            
            if len(nombre_producto) < 3:
                continue

            productos_extraidos.append({
                "user_id": pyme_user_id if pyme_user_id is not None else 0, # Asegurar que user_id se guarde
                "nombre": nombre_producto[:250], 
                "descripcion": descripcion_producto[:500],
                "precio_str": precio_str_final, 
                "precio_float": precio_float_final, 
                "moneda": "ARS", 
                "texto_para_embedding": f"{nombre_producto} {descripcion_producto} {precio_str_final if precio_str_final else ''}"
            })
            logging.debug(f"Producto extraído: Nombre='{nombre_producto}', PrecioStr='{precio_str_final}', PrecioFloat={precio_float_final}")
        # --- FIN DE LA LÓGICA DE EXTRACCIÓN ---

        logging.info(f"Total de productos potencialmente extraídos: {len(productos_extraidos)}")
        return productos_extraidos

    except Exception as e:
        # Asegurarse que logging esté importado para que este error se registre
        logging.error(f"❌ Error procesando catálogo con Google Doc AI ({pdf_path}): {e}", exc_info=True)
        return [] # Devolver lista vacía en caso de error