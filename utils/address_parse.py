import re
from typing import Tuple, Optional

DISTRICT_KEYS = r"(?:barrio|distrito|departamento|localidad|partido)\s+"

def split_ubicacion_y_distrito(texto: str) -> Tuple[str, Optional[str]]:
    """Split an address into street part and district if a clear separator is present.

    Only splits when the district is provided after a comma or after a keyword
    such as "barrio" or "distrito". Otherwise the text is treated entirely as
    the street address and district is ``None``.
    """
    if not texto:
        return "", None
    t = texto.strip()
    # case with comma
    if "," in t:
        ubic, resto = t.split(",", 1)
        resto = resto.strip()
        m = re.match(DISTRICT_KEYS + r"(.+)$", resto, flags=re.I)
        return ubic.strip(), (m.group(1).strip() if m else (resto or None))
    # case with keyword without comma
    m = re.search(DISTRICT_KEYS + r"(.+)$", t, flags=re.I)
    if m:
        return t[: m.start()].strip().rstrip(", "), m.group(1).strip()
    # no separator -> no district
    return t, None
