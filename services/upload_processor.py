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

        # Leer CSV / Excel
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

                texto = f"{nombre}. {descripcion}. Precio: {precio}."
                textos.append(texto)
                registros.append({
                    "nombre": nombre,
                    "descripcion": descripcion,
                    "precio": precio
                })

        # Leer PDF con soporte de tablas
        elif ext == ".pdf":
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    # Intentar extraer tabla estructurada
                    table = page.extract_table()
                    if table and len(table[0]) >= 2:
                        headers = [h.lower() for h in table[0]]
                        for row in table[1:]:
                            row_dict = dict(zip(headers, row))
                            nombre = row_dict.get("nombre", row[0]) or ""
                            descripcion = row_dict.get("descripcion", "") or ""
                            precio = row_dict.get("precio", "") or ""
                            texto = f"{nombre}. {descripcion}. Precio: {precio}."
                            textos.append(texto)
                            registros.append({
                                "nombre": nombre.strip()[:50],
                                "descripcion": descripcion.strip(),
                                "precio": precio.strip()
                            })
                    else:
                        # Fallback a texto plano por línea
                        text = page.extract_text()
                        if text:
                            for line in text.split("\n"):
                                texto = line.strip()
                                if texto:
                                    textos.append(texto)
                                    registros.append({
                                        "nombre": texto[:50],
                                        "descripcion": texto,
                                        "precio": "-"
                                    })

        if not textos:
            raise ValueError("No se extrajo contenido útil del archivo")

        vectores = embed_textos(textos)
        if not vectores:
            raise ValueError("❌ No se pudieron generar vectores con Cohere")

        items = [
            CatalogoEmbedding(
                user_id=user_id,
                nombre=r["nombre"],
                descripcion=r["descripcion"],
                precio=r["precio"],
                embedding_vector=vec
            )
            for r, vec in zip(registros, vectores)
        ]

        db.session.bulk_save_objects(items)
        db.session.commit()
        logging.info(f"✅ {len(items)} ítems embebidos correctamente (user_id={user_id})")
        return len(items)

    except Exception as e:
        logging.error(f"❌ Error en procesamiento de catálogo: {e}")
        return 0
