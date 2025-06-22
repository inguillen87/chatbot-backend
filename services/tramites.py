from typing import List, Dict

from .municipios import TRAMITES_INFO


def buscar_tramites(query: str | None = None) -> List[Dict]:
    """Devuelve los trámites ordenados y opcionalmente filtrados."""
    q = (query or "").strip().lower()
    resultados = []
    for nombre in sorted(TRAMITES_INFO.keys()):
        if q and q not in nombre.lower():
            continue
        info = TRAMITES_INFO[nombre]
        resultados.append({
            "nombre": nombre,
            "descripcion": info.get("descripcion"),
            "botones": info.get("botones", []),
        })
    return resultados
