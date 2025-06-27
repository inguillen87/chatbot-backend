# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
from typing import List, Dict, Any
from .utils import parse_precio_flexible

logger = logging.getLogger(__name__)


def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """Lee un archivo Excel y devuelve las filas sin transformar."""
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Leyendo archivo: {base_filename}")
    try:
        df = pd.read_excel(path, sheet_name=0, keep_default_na=False, dtype=str)
    except Exception as e_read:
        logger.error(
            f"❌ Error al leer el archivo Excel '{base_filename}': {e_read}",
            exc_info=True,
        )
        raise ValueError("No se pudo leer el archivo Excel.")

    if df.empty:
        logger.warning(f"⚠️ El archivo '{base_filename}' está vacío o no se pudo leer.")
        return []

    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    registros: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        registro = {
            "nombre": str(row.get("nombre", "")).strip(),
            "descripcion": str(row.get("descripcion", "")).strip(),
            "precio_str": str(row.get("precio", "")).strip(),
            "precio_float": parse_precio_flexible(row.get("precio"))[1],
            "sku": str(row.get("sku", "")).strip(),
            "categoria_qdrant": str(row.get("categoria", pyme_rubro_nombre)).strip(),
            "unidad": str(row.get("unidad", "")).strip(),
            "cantidad_disponible": str(row.get("stock", "")).strip() or None,
            "marca": str(row.get("marca", "")).strip(),
        }
        if not registro["nombre"]:
            continue
        registros.append(registro)

    logger.info(f"✅ {len(registros)} filas obtenidas de '{base_filename}'")
    return registros
