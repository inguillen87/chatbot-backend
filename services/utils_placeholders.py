# services/utils_placeholders.py
import re
import os
import json
import logging

logger = logging.getLogger(__name__)

def reemplazar_placeholders(texto, datos):
    if not isinstance(texto, str) or not texto:
        return texto

    def get_valor(clave):
        if isinstance(datos, dict):
            return datos.get(clave, "")
        try:
            return getattr(datos, clave, "")
        except Exception:
            return ""

    encontrados = re.findall(r"\[([a-zA-Z0-9_]+)\]", texto)
    for clave in set(encontrados):
        valor = get_valor(clave)
        texto = texto.replace(f"[{clave}]", str(valor) if valor is not None else "")
    return texto

# La ruta correcta a /data/sugerencias.json (dos niveles arriba desde /services)
SUGERENCIAS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sugerencias.json")
_cache_sugerencias = None

def sugerencias_por_rubro(rubro_nombre):
    global _cache_sugerencias
    if _cache_sugerencias is None:
        try:
            with open(SUGERENCIAS_PATH, "r", encoding="utf-8") as f:
                _cache_sugerencias = json.load(f)
        except Exception as e:
            logger.error(f"[SUGERENCIAS] No se pudo cargar sugerencias.json en {SUGERENCIAS_PATH}: {e}")
            _cache_sugerencias = {}
    rubro_key = str(rubro_nombre).strip().lower()
    return _cache_sugerencias.get(rubro_key, [])
