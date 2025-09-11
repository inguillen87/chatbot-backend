import json
import os
from functools import lru_cache


@lru_cache(maxsize=32)
def get_geo_context(tenant_id: str) -> dict:
    """Load geographical context for a given tenant.

    Looks for ``data/municipios/<tenant_id>/geo.json`` which should include
    fields like ``city`` (or ``ciudad``), ``state``/``provincia``, ``country``/``pais``,
    and optional ``bounds`` and ``region_hint`` for API biasing.
    """
    path = os.path.join("data", "municipios", tenant_id, "geo.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Geo context file not found for tenant '{tenant_id}'")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
