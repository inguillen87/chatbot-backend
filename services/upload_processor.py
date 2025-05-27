import os
import logging
import pandas as pd
from models import CatalogoEmbedding
from extensions import db
from services.cohere_ai import embed_textos  # Asegurate de tener esta función implementada

def procesar_y_embedear_catalogo(path, user_id):
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext not in [".csv", ".xlsx", ".xls"]:
            raise ValueError("Formato no soportado")

        # Leer archivo
        df = pd.read_csv(path) if ext == ".csv" else pd.read_excel(path)
        if df.empty:
            return 0

        registros = []
        textos = []

        for _, row in df.iterrows():
            nombre = str(row.get("nombre", "")).strip()
            descripcion = str(row.get("descripcion", "")).strip()
            precio = str(row.get("precio", "")).strip()

            texto_base = f"{nombre}. {descripcion}. Precio: {precio}."
            textos.append(texto_base)

            registros.append({
                "nombre": nombre,
                "descripcion": descripcion,
                "precio": precio,
            })

        vectores = embed_textos(textos)  # Devuelve una lista de listas

        items = []
        for r, vec in zip(registros, vectores):
            items.append(CatalogoEmbedding(
                user_id=user_id,
                nombre=r["nombre"],
                descripcion=r["descripcion"],
                precio=r["precio"],
                embedding_vector=vec
            ))

        db.session.bulk_save_objects(items)
        db.session.commit()
        logging.info(f"✅ {len(items)} ítems procesados y embebidos para user_id={user_id}")
        return len(items)

    except Exception as e:
        logging.error(f"❌ Error procesando catálogo: {e}")
        return 0
