from typing import Optional

def normalize_category(category: Optional[str]) -> Optional[str]:
    """Return a unified category name for ticket listings.
    Any variant containing 'lumin' (e.g., luminaria, alumbrado) is mapped to
    the single category name 'Luminarias'. Other categories are returned as-is.
    """
    if not category:
        return category
    lower = category.lower()
    if "lumin" in lower:
        return "Luminarias"
    return category
