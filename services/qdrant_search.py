# En services/qdrant_search.py
def armar_respuesta_legible(resultados_qdrant: list) -> str:
    if not resultados_qdrant:
        return ""

    contexto_items = []
    for hit in resultados_qdrant:
        payload = hit.payload
        # Prioriza campos estructurados si existen
        nombre = payload.get('nombre_producto', payload.get('nombre', payload.get('texto', 'Producto'))) 
        precio = payload.get('precio_str', payload.get('precio', 'Consultar precio'))
        descripcion = payload.get('descripcion_corta', payload.get('descripcion', '').splitlines()[0]) # Tomar primera línea de desc si es larga

        item_info = f"Producto: {nombre} | Precio: {precio}"
        # Podrías añadir más campos si los tienes:
        # categoria = payload.get('categoria', '')
        # if categoria: item_info += f" | Categoría: {categoria}"
        # if descripcion: item_info += f"\n  Breve Desc: {descripcion[:100]}" # Limitar descripción
        contexto_items.append(item_info)

    if not contexto_items:
        return ""

    # Presentar la información de forma más clara al LLM
    return "Del catálogo encontré lo siguiente que podría ser relevante:\n" + "\n".join(contexto_items)