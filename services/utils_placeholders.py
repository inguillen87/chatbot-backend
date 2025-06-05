# services/utils_placeholders.py

import re

def reemplazar_placeholders(texto, datos):
    """
    Reemplaza placeholders tipo [nombreEmpresa], [telefono], etc. en el texto,
    usando los valores que estén en el dict o en el objeto datos.
    - Si datos es un objeto, busca con getattr.
    - Si datos es un dict, usa get.
    - Si no encuentra el valor, deja el placeholder vacío.
    """
    if not isinstance(texto, str) or not texto:
        return texto

    def get_valor(clave):
        if isinstance(datos, dict):
            return datos.get(clave, "")
        try:
            return getattr(datos, clave, "")
        except Exception:
            return ""

    # Encuentra todos los [placeholders]
    encontrados = re.findall(r"\[([a-zA-Z0-9_]+)\]", texto)
    for clave in set(encontrados):
        valor = get_valor(clave)
        texto = texto.replace(f"[{clave}]", str(valor) if valor is not None else "")
    return texto
