from services.categorias_municipio import CATEGORIAS_RECLAMO

def handle(msg: str, meta: dict):
    """
    Handles the initial step of the Reclamos (complaints) flow by showing the
    available claim categories.
    """

    items = []
    for i, categoria in enumerate(CATEGORIAS_RECLAMO, 1):
        items.append({
            "n": str(i),
            "label": categoria["label"],
            "key": f"cat_reclamo_{categoria['key']}" # Add a prefix to distinguish from other commands
        })

    return {
        "type": "menu",
        "title": "Elegí una categoría para tu reclamo:",
        "summary": "Estos son los tipos de reclamo que podés iniciar.",
        "data": {
            "items": items
        },
        "tags": ["menu", "reclamos"],
        "source": "reclamos_flow"
    }
