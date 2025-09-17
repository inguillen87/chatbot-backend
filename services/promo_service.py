# services/promo_service.py
from typing import Dict, Any, Optional

# En el futuro, esto podría leerse desde una base de datos o un archivo de configuración
# para permitir la actualización de promociones sin necesidad de un deploy.
PROMOSIONES_ACTIVAS = [
    {
        "id": "punto_limpio_junin",
        "image_url": "https://www.juninmendoza.gov.ar/wp-content/uploads/logo-junin-punto-limpio-1024x472.png",
        "text": (
            "¿Sabías que estamos trabajando para una Junín más limpia? ♻️\n"
            "Conocé nuestra planta de recolección, reciclaje y elaboración de productos sustentables.\n"
            "Ladrillos, tejas, postes, mangueras, impresión 3D, luminarias LED y paneles solares."
        ),
        "link_text": "Más info",
        "link_url": "https://www.juninmendoza.gov.ar/punto-limpio/"
    }
    # Se podrían agregar más promociones aquí y rotarlas.
]

def get_active_promo() -> Optional[Dict[str, Any]]:
    """
    Devuelve la promoción activa.
    Por ahora, devuelve la primera de la lista estática.
    """
    if PROMOSIONES_ACTIVAS:
        return PROMOSIONES_ACTIVAS[0]
    return None
