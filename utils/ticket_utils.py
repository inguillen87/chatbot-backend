from typing import Optional
from services.categorias_municipio import (
    CATEGORIAS_SINONIMOS,
    normalizar_texto,
)
from services.herramientas_municipio import (
    KEYWORD_TO_CATEGORY_MAP,
    ensure_keyword_cache,
)


# Precompute a mapping from normalized synonyms to their canonical category
_synonym_map: dict[str, str] = {}
for canon, synonyms in CATEGORIAS_SINONIMOS.items():
    canon_norm = normalizar_texto(canon)
    # Default output is title-cased canonical name
    canon_output = "Luminarias" if canon_norm == "luminaria" else canon.title()
    _synonym_map[canon_norm] = canon_output
    for s in synonyms:
        _synonym_map[normalizar_texto(s)] = canon_output

# Include dynamic keywords loaded from previous tickets
ensure_keyword_cache()
for keyword, category in KEYWORD_TO_CATEGORY_MAP.items():
    _synonym_map.setdefault(normalizar_texto(keyword), category)


def normalize_category(category: Optional[str]) -> Optional[str]:
    """Return a unified category name for ticket listings.

    Categories are normalized using the synonym map defined in
    ``services.categorias_municipio``. Any input that matches (even
    partially) a known synonym is mapped to a canonical name. If no match is
    found the original category is returned unchanged.
    """

    if not category:
        return category

    ensure_keyword_cache()
    # Incorporar keywords dinámicas que puedan haberse cargado
    for keyword, cat in KEYWORD_TO_CATEGORY_MAP.items():
        _synonym_map.setdefault(normalizar_texto(keyword), cat)

    texto = normalizar_texto(category)
    for key, canon in _synonym_map.items():
        if key in texto:
            return canon
    return category
