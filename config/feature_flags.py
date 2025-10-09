import os
from typing import Optional, Set


def _parse_int_set(raw_value: str) -> Set[int]:
    """Parse a comma separated list of integers from an environment variable."""

    resultados: Set[int] = set()
    if not raw_value:
        return resultados

    for token in raw_value.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        try:
            resultados.add(int(cleaned))
        except ValueError:
            continue
    return resultados


FEATURE_ENCUESTAS = os.getenv("FEATURE_ENCUESTAS", "false").lower() == "true"
FEATURE_ENCUESTAS_TENANTS = _parse_int_set(os.getenv("FEATURE_ENCUESTAS_TENANTS", ""))


def is_feature_encuestas_enabled_for_tenant(tenant_id: Optional[int]) -> bool:
    """Return whether the surveys module is enabled for a given tenant."""

    if FEATURE_ENCUESTAS:
        return True
    if tenant_id is None:
        return False
    return tenant_id in FEATURE_ENCUESTAS_TENANTS
