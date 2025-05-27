import pandas as pd
import logging

def procesar_catalogo_excel(path: str) -> list[dict]:
    try:
        ext = path.lower().split(".")[-1]
        df = pd.read_csv(path) if ext == "csv" else pd.read_excel(path)

        if df.empty:
            logging.warning("⚠️ El archivo Excel/CSV está vacío.")
            return []

        productos = []
        for _, row in df.iterrows():
            nombre = str(row.get("nombre", "")).strip()
            descripcion = str(row.get("descripcion", nombre)).strip()
            precio = str(row.get("precio", "")).replace("$", "").replace(",", ".").strip()
            cantidad = str(row.get("cantidad", "1")).strip()

            if not nombre and not descripcion:
                continue

            productos.append({
                "nombre": nombre[:50],
                "descripcion": descripcion[:500],
                "precio": precio or "-",
                "cantidad": cantidad or "1"
            })

        return productos
    except Exception as e:
        logging.error(f"❌ Error procesando archivo Excel: {e}")
        return []
