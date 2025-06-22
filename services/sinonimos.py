# services/sinonimos.py
from difflib import SequenceMatcher
from typing import Dict, List, Optional
import json
import logging
import os
from pathlib import Path
from .herramientas_municipio import normalizar_texto

# Mapeo simple de sinónimos para trámites municipales
TRAMITE_SYNONYMS: Dict[str, List[str]] = {
    "licencia de conducir": [
        "carnet",
        "carnet de conducir",
        "carnet conducir",
        "licencia",
        "licencia de manejo",
        "licencia nacional",
        "registro",
        "registro de conducir",
        "registro conducir",
        "carnet de manejo",
        "permiso de conducir",
        "brevete",
    ],
}

logger = logging.getLogger(__name__)

PRODUCT_SYNONYMS_PATH = Path(__file__).resolve().parent.parent / "data" / "product_synonyms.json"

def _cargar_json(path: Path) -> Dict[str, List[str]]:
    if not path.is_file():
        logger.warning(f"[SINONIMOS] Archivo no encontrado: {path}")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            datos = json.load(f)
        if isinstance(datos, dict):
            return {str(k): list(map(str, v)) for k, v in datos.items() if isinstance(v, list)}
    except Exception as e:
        logger.error(f"[SINONIMOS] Error leyendo {path}: {e}")
    return {}


def cargar_product_synonyms() -> Dict[str, List[str]]:
    """Carga sinónimos de productos desde un archivo JSON."""
    datos = _cargar_json(PRODUCT_SYNONYMS_PATH)
    if datos:
        return datos
    # Fallback por defecto si no se pudo cargar el archivo
    return {
        "malbec": ["vino malbec", "vinos malbec", "malbek"],
        "caja": ["cajon", "pack"],
        "remera": ["camiseta", "playera", "polera"],
        "tornillo": ["perno", "rosca"],
        "ladrillo": ["bloque", "block", "tabique"],
        "paracetamol": ["acetaminofen", "tylenol"],
        "martillo": ["mazo", "martillo de uña"],
    }

PRODUCT_SYNONYMS: Dict[str, List[str]] = cargar_product_synonyms()


def aplicar_sinonimos(texto: str, mapa: Dict[str, List[str]]) -> str:
    """Reemplaza sinónimos encontrados en el texto por su forma canónica."""
    texto_norm = normalizar_texto(texto)
    for canon, sinonimos in mapa.items():
        canon_norm = normalizar_texto(canon)
        for s in sinonimos:
            s_norm = normalizar_texto(s)
            if s_norm in texto_norm:
                texto_norm = texto_norm.replace(s_norm, canon_norm)
    return texto_norm


def fuzzy_match(opciones: List[str], texto: str, threshold: float = 0.75) -> Optional[str]:
    """Devuelve la opción que tenga mayor similitud con el texto."""
    texto_norm = normalizar_texto(texto)
    mejor = None
    mejor_score = 0.0
    for opt in opciones:
        score = SequenceMatcher(None, texto_norm, normalizar_texto(opt)).ratio()
        if score > mejor_score:
            mejor_score = score
            mejor = opt
    if mejor_score >= threshold:
        return mejor
    return None
