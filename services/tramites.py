from typing import List, Dict

def buscar_tramites(query: str | None = None) -> List[Dict]:
    """Devuelve los trámites ordenados y opcionalmente filtrados."""
    from .municipio_responder import get_tramites_info

    q = (query or "").strip().lower()
    tramites = get_tramites_info()
    resultados = []
    for nombre in sorted(tramites.keys()):
        if q and q not in nombre.lower():
            continue
        info = tramites[nombre]
        resultados.append({
            "nombre": nombre,
            "descripcion": info.get("descripcion"),
            "botones": info.get("botones", []),
        })
    return resultados
