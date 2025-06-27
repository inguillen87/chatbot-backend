# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
from typing import List, Dict, Any

from .utils import limpiar_texto_base

logger = logging.getLogger(__name__)


def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """Lee un archivo Excel y devuelve las filas normalizadas."""
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

    df.columns = [limpiar_texto_base(c).replace(" ", "_") for c in df.columns]

    registros: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        registro = {col: str(row.get(col, "")).strip() for col in df.columns}
        if not registro.get("nombre"):
            continue
        registros.append(registro)

    logger.info(f"✅ {len(registros)} filas obtenidas de '{base_filename}'")
    return registros
