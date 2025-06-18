# services/sinonimos.py
from difflib import SequenceMatcher
from typing import Dict, List, Optional
from .herramientas_municipio import normalizar_texto

# Mapeo simple de sinónimos para trámites municipales
TRAMITE_SYNONYMS: Dict[str, List[str]] = {
    "licencia de conducir": ["carnet", "carnet de conducir", "licencia", "registro", "registro de conducir", "carnet de manejo"],
}

# Mapeo de sinónimos para productos comunes en PyME
PRODUCT_SYNONYMS: Dict[str, List[str]] = {
    "malbec": ["vino malbec", "malbek"],
    "caja": ["cajon", "pack"],
}


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
