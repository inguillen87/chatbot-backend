# En services/qdrant_search.py
def armar_respuesta_legible(resultados_qdrant: list) -> str:
    if not resultados_qdrant:
        return ""

    contexto_items = []
    for hit in resultados_qdrant:
        payload = hit.payload
        # Intentar obtener campos estructurados, con fallbacks
        nombre = payload.get('nombre_producto', payload.get('nombre', 'Producto')) # Prioriza 'nombre_producto'
        precio = payload.get('precio_str', payload.get('precio', 'Consultar precio'))
        descripcion = payload.get('descripcion_corta', payload.get('descripcion', '')) # Prioriza 'descripcion_corta'

        item_info = f"Producto: {nombre}\nPrecio: {precio}"
        if descripcion:
            item_info += f"\nDescripción: {descripcion}"
        contexto_items.append(item_info)

    if not contexto_items:
        return ""

    return "Información relevante del catálogo:\n" + "\n---\n".join(contexto_items)