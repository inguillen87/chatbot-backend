from services.categorias_municipio import CATEGORIAS_RECLAMO

def handle(msg: str, meta: dict):
    """
    Handles the initial step of the Reclamos (complaints) flow by showing the
    available claim categories.
    """

    items = []
    # CATEGORIAS_RECLAMO is a list of strings, not dicts. Iterate correctly.
    for i, categoria_str in enumerate(CATEGORIAS_RECLAMO, 1):
        items.append({
            "n": str(i),
            # Capitalize for better display
            "label": categoria_str.capitalize(),
            # Create a safe key for the action_id
            "key": f"cat_reclamo_{categoria_str.replace(' ', '_').lower()}"
        })

    return {
        "type": "menu",
        "title": "Elegí una categoría para tu reclamo:",
        "summary": "Estos son los tipos de reclamo que podés iniciar:",
        "data": {
            "items": items
        },
        "tags": ["menu", "reclamos"],
        "source": "reclamos_flow"
    }
