import os
import logging
import pandas as pd
import pdfplumber

from models import CatalogoEmbedding
from extensions import db
from services.cohere_ai import embed_textos


def procesar_y_embedear_catalogo(path, user_id):
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext not in [".csv", ".xlsx", ".xls", ".pdf"]:
            raise ValueError("❌ Formato no soportado")

        textos = []
        registros = []

        # Procesar CSV o Excel
        if ext in [".csv", ".xlsx", ".xls"]:
            df = pd.read_csv(path) if ext == ".csv" else pd.read_excel(path)
            if df.empty:
                return 0

            for _, row in df.iterrows():
                nombre = str(row.get("nombre", "")).strip()
                descripcion = str(row.get("descripcion", "")).strip()
                precio = str(row.get("precio", "")).strip()

                if not nombre and not descripcion:
                    continue

                texto_base = f"{nombre}. {descripcion}. Precio: {precio}."
                textos.append(texto_base)

                registros.append({
                    "nombre": nombre,
                    "descripcion": descripcion,
                    "precio": precio,
                })

        # Procesar PDF (texto plano por línea)
        elif ext == ".pdf":
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    for line in page.extract_text().split("\n"):
                        texto = line.strip()
                        if texto:
                            textos.append(texto)
                            registros.append({
                                "nombre": texto[:50],
                                "descripcion": texto,
                                "precio": "-"
                            })

        # Embeddings con Cohere
        vectores = embed_textos(textos)
        if not vectores:
            raise ValueError("No se generaron vectores")

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
