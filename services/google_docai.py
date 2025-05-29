# En google_docai.py
# ... (tus importaciones y carga de credenciales igual) ...

def procesar_catalogo_pdf_google(pdf_path, tipo_catalogo="generico"): # Añadido tipo_catalogo
    try:
        # ... (tu inicialización de cliente Document AI igual) ...
        # result = client.process_document(request=request)
        # texto_extraido = result.document.text
        # logging.info("📝 Texto extraído con Google Document AI:")
        # logging.info(texto_extraido) # Útil para depurar la extracción

        # --- INICIO DE LÓGICA DE EXTRACCIÓN MEJORADA (CONCEPTUAL) ---
        # La clave es usar las capacidades de análisis de entidades y tablas de Document AI
        # en lugar de solo el texto plano. Esto es solo un esquema conceptual.
        # La implementación real dependerá de la estructura de la respuesta de Document AI.

        productos_extraidos = []
        document = result.document

        # Intento 1: Usar entidades si el procesador las extrae (necesitarías un procesador configurado para ello)
        # for entity in document.entities:
        #     if entity.type_ == "product_item": # O el tipo de entidad que defina tu procesador
        #         nombre = entity.mention_text
        #         precio = "" # Lógica para encontrar el precio asociado a esta entidad
        #         # ... extraer otros campos ...
        #         productos_extraidos.append({"nombre": nombre, "descripcion": nombre, "precio": precio, "cantidad": "1"})

        # Intento 2: Usar tablas (más prometedor para tus listas de precios)
        if document.tables:
            logging.info(f"✅ Se encontraron {len(document.tables)} tablas en el PDF.")
            for table_idx, table in enumerate(document.tables):
                logging.info(f"Procesando tabla {table_idx + 1}")
                # Aquí necesitarías una lógica para interpretar las filas y columnas de la tabla.
                # Por ejemplo, identificar las columnas de "Nombre", "Descripción", "Precio".
                # Esto es complejo y específico para cada formato de tabla.
                # EJEMPLO MUY SIMPLIFICADO (necesitarás adaptarlo muchísimo):
                header_row = table.header_rows[0].cells if table.header_rows else None
                # col_nombre_idx, col_precio_idx = -1, -1
                # if header_row:
                # try: col_nombre_idx = [cell.layout.text_anchor.text_segments[0] for cell in header_row].index("PRODUCTO") except ValueError: pass
                # try: col_precio_idx = [cell.layout.text_anchor.text_segments[0] for cell in header_row].index("PRECIO") except ValueError: pass
                
                for body_row in table.body_rows:
                    # Extraer texto de celdas específicas si identificas las columnas
                    # nombre_celda = body_row.cells[col_nombre_idx].layout.text_anchor.text_segments[0] if col_nombre_idx != -1 else "Nombre no extraído"
                    # precio_celda = body_row.cells[col_precio_idx].layout.text_anchor.text_segments[0] if col_precio_idx != -1 else "Precio no extraído"
                    
                    # Por ahora, como fallback, usaremos tu lógica original línea por línea si las tablas no se procesan
                    # O si no hay tablas, procesar el texto plano
                    pass # Aquí iría la lógica de procesamiento de tablas


        # Fallback a tu lógica actual si el procesamiento de tablas/entidades no se implementa o falla:
        # Esta lógica es la que probablemente necesite más ajustes por tipo de catálogo.
        if not productos_extraidos: # Si no se llenó con tablas/entidades
             logging.info("No se usaron tablas/entidades, procesando texto plano línea por línea (lógica original con ajustes)...")
             lineas = [line.strip() for line in document.text.split("\n") if line.strip()]
             for linea in lineas:
                 nombre_final = linea # Default, intentar mejorar esto
                 precio_final = ""
                 descripcion_final = linea # Default

                 # Ejemplo de heurística MUY BÁSICA para vinos (Salvador Patti)
                 if tipo_catalogo == "bodega_salvador_patti":
                     # Intentar extraer MARCA, VARIETAL, y luego el precio de la caja.
                     # Esto necesitaría regex complejas y conocimiento de la estructura.
                     # Ejemplo: si la línea es "VINCENT MALBEC 6 140 2732 5 16.390,00"
                     # nombre_final podría ser "VINCENT MALBEC"
                     # precio_final podría ser "16390.00" (precio caja)
                     # Esto es solo un ejemplo, la implementación real es compleja.
                     match_vino = re.search(r"([A-Z\s]+)\s+([A-Z\s\/]+)\s+.*?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2}))\s*$", linea)
                     if match_vino:
                         marca_intento = match_vino.group(1).strip()
                         varietal_intento = match_vino.group(2).strip()
                         precio_intento = match_vino.group(3).replace(".","").replace(",",".") # Asumiendo que el último es el precio de caja
                         if not marca_intento.isupper(): # Heurística simple, si no es todo mayúsculas, quizás no es la marca
                             nombre_final = linea
                         else:
                             nombre_final = f"{marca_intento} {varietal_intento}"
                             precio_final = precio_intento
                             descripcion_final = nombre_final
                     else: # Si no hay match complejo, usar regex de precio general
                         precio_match = re.search(r"\$?\s*([\d.,]+)", linea)
                         if precio_match:
                             precio_candidato = precio_match.group(1).replace(".","").replace(",",".")
                             # Intentar limpiar el nombre si el precio está al final
                             nombre_sin_precio = linea.replace(precio_match.group(0), "").strip()
                             if len(nombre_sin_precio) > 5 : # Evitar nombres muy cortos después de quitar precio
                                 nombre_final = nombre_sin_precio
                             precio_final = precio_candidato

                 # Ejemplo de heurística MUY BÁSICA para SOX
                 elif tipo_catalogo == "indumentaria_sox":
                     match_art = re.search(r"^(ART\.\s*[A-Z0-9]+)\s*(.*?)\s*(\$?\s*[\d.,]+)\s*$", linea, re.IGNORECASE)
                     if match_art:
                         codigo_articulo = match_art.group(1).strip()
                         descripcion_articulo = match_art.group(2).strip()
                         precio_articulo = match_art.group(3).replace("$","").replace(".","").replace(",",".").strip()
                         nombre_final = f"{codigo_articulo} - {descripcion_articulo}" if descripcion_articulo else codigo_articulo
                         precio_final = precio_articulo
                         descripcion_final = nombre_final
                     else: # Fallback si el formato de ART no coincide
                         precio_match = re.search(r"\$?\s*([\d.,]+)", linea) # Tu regex de precio
                         if precio_match:
                             precio_final = precio_match.group(1).replace(".","").replace(",",".")
                             nombre_final = linea.replace(precio_match.group(0), "").strip()


                 else: # Lógica genérica (tu regex original de precio)
                     precio_match = re.search(r"\$?\s?(\d{1,7}(?:[.,]\d{2})?)", linea) # Aumentado a 7 dígitos antes de la coma
                     if precio_match:
                         precio_final = precio_match.group(1).replace(",", ".")
                         # Intentar limpiar un poco el nombre
                         nombre_candidato = linea.replace(precio_match.group(0), "").strip()
                         if len(nombre_candidato) > 3: # Si queda algo razonable como nombre
                             nombre_final = nombre_candidato
                         else: # Si no, mantener la línea original como nombre para no perder info
                             nombre_final = linea # Y el precio se extrajo
                 
                 if nombre_final: # Solo añadir si tenemos un nombre (aunque sea la línea completa)
                     productos_extraidos.append({
                         "nombre": nombre_final.strip()[:250], # Limitar longitud
                         "descripcion": descripcion_final.strip()[:500], # Usar descripción separada
                         "precio": precio_final, # Ya debería estar formateado como string numérico con "."
                         "cantidad": "1" # Default, o intentar extraer si es posible
                     })

        if not productos_extraidos:
            logging.warning("No se pudieron extraer productos estructurados, volviendo a la lógica de split por línea más simple.")
            # Este sería tu bucle original como último recurso si todo lo demás falla.
            # (Lo omito aquí por brevedad, pero es el for linea in lineas: productos.append(...) que tenías)
            # PERO, idealmente, la lógica anterior ya debería haber procesado todas las líneas.

        logging.info(f"Productos extraídos del PDF ({tipo_catalogo}): {len(productos_extraidos)}")
        return productos_extraidos

    except Exception as e:
        logging.error(f"❌ Error procesando catálogo con Google Doc AI: {e}", exc_info=True)
        return []