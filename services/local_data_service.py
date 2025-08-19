import os
import json
import logging
from difflib import get_close_matches

logger = logging.getLogger(__name__)

_DATA_CACHE = {}
_DATA_MTIME = {}

def cargar_datos_locales(nombre_archivo: str, municipio_id: str = "default") -> dict:
    """
    Carga un archivo de datos JSON desde el directorio de datos del municipio.
    Utiliza un caché en memoria para evitar lecturas de disco innecesarias.
    """
    global _DATA_CACHE, _DATA_MTIME

    ruta = os.path.join(
        os.path.dirname(__file__), "..", "data", "municipios", municipio_id, nombre_archivo
    )

    try:
        mtime = os.path.getmtime(ruta)
    except OSError:
        logger.warning(f"[LocalData] Archivo no encontrado en '{ruta}', se usará dict vacío.")
        return {}

    cache_key = f"{municipio_id}_{nombre_archivo}"

    if cache_key not in _DATA_CACHE or _DATA_MTIME.get(cache_key) != mtime:
        logger.info(f"Cargando o recargando archivo de datos locales: '{ruta}'")
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                _DATA_CACHE[cache_key] = json.load(f)
            _DATA_MTIME[cache_key] = mtime
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error al cargar o parsear el archivo JSON '{ruta}': {e}")
            _DATA_CACHE[cache_key] = {} # Devolver vacío en caso de error para no romper el flujo

    return _DATA_CACHE.get(cache_key, {})

class LocalDataHandler:
    def __init__(self, municipio_id: str = "default"):
        self.municipio_id = municipio_id

    def handle(self, query: str, file_name: str) -> dict | None:
        """
        Busca una consulta en un archivo de datos locales y devuelve una respuesta formateada si encuentra una coincidencia.
        """
        query_norm = query.lower().strip().replace(" ", "_")

        data = cargar_datos_locales(file_name, self.municipio_id)
        if not data:
            return None

        # Búsqueda de coincidencia
        # 1. Coincidencia exacta (normalizada)
        if query_norm in data:
            match_key = query_norm
        else:
            # 2. Búsqueda por "fuzzy matching" si no hay coincidencia exacta
            possible_matches = get_close_matches(query_norm, data.keys(), n=1, cutoff=0.8)
            if possible_matches:
                match_key = possible_matches[0]
            else:
                return None

        logger.info(f"Coincidencia encontrada en '{file_name}' para la consulta '{query}'. Clave: '{match_key}'")

        # Construir la respuesta
        match_data = data[match_key]
        message_body = match_data.get("descripcion", "No se encontró una descripción para este tema.")
        botones = match_data.get("botones", [])

        return {
            "message_body": message_body,
            "options_list": botones,
            "fuente": f"local_data_handler:{file_name}:{match_key}"
        }
