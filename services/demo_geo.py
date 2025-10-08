"""Utilities to generate demo geographic datasets around Junín, Mendoza.

These helpers are used as fallbacks when the real analytics endpoints do not
have data available (for example on fresh installations or in demo mode).
The generated data is pseudo-random but deterministic per day so dashboards
look alive while keeping a consistent experience within the same session.
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from typing import List, MutableMapping

JUNIN_CENTER = (-33.008818, -68.485079)
JUNIN_RADIUS_KM = 4.2

DEMO_CATEGORIES: tuple[str, ...] = (
    "Iluminación",
    "Residuos",
    "Bacheo",
    "Seguridad",
    "Arbolado",
    "Pluvial",
)

DEMO_ESTADOS: tuple[str, ...] = (
    "nuevo",
    "en_progreso",
    "derivado",
    "resuelto",
)

DEMO_BARRIOS: tuple[str, ...] = (
    "Ciudad",
    "La Colonia",
    "Philipps",
    "Medrano",
    "Los Barriales",
    "Rodríguez Peña",
)


def _rng(seed: int | None = None) -> random.Random:
    if seed is None:
        seed = int(datetime.utcnow().strftime("%Y%m%d"))
    return random.Random(seed)


def _random_point(rng: random.Random) -> tuple[float, float]:
    angle = rng.uniform(0, 2 * math.pi)
    radius = rng.uniform(0.25, JUNIN_RADIUS_KM)
    delta_lat = (radius * math.cos(angle)) / 111  # approx km to degrees
    denominator = 111 * math.cos(math.radians(JUNIN_CENTER[0])) or 1
    delta_lon = (radius * math.sin(angle)) / denominator
    return (JUNIN_CENTER[0] + delta_lat, JUNIN_CENTER[1] + delta_lon)


def _category_mix(rng: random.Random, total: int) -> MutableMapping[str, int]:
    selected: List[str] = []
    desired = rng.randint(1, min(3, len(DEMO_CATEGORIES)))
    while len(selected) < desired:
        candidate = rng.choice(DEMO_CATEGORIES)
        if candidate not in selected:
            selected.append(candidate)
    remaining = total
    mix: MutableMapping[str, int] = {}
    for index, category in enumerate(selected):
        if index == len(selected) - 1:
            mix[category] = max(1, remaining)
        else:
            share = max(1, int(rng.uniform(0.2, 0.6) * remaining))
            mix[category] = share
            remaining -= share
    return mix


def generate_demo_heatmap_cells(
    *, scope: str = "municipio", cells: int = 28, seed: int | None = None
) -> list[dict]:
    rng = _rng(seed)
    base = 6 if scope == "municipio" else 3
    cells_data: list[dict] = []
    for index in range(cells):
        lat, lon = _random_point(rng)
        count = rng.randint(base, base + 28)
        cells_data.append(
            {
                "cell_id": f"demo-{scope}-{index}",
                "count": count,
                "centroid_lat": round(lat, 6),
                "centroid_lon": round(lon, 6),
                "categories": dict(_category_mix(rng, count)),
                "fuente": "demo",
            }
        )
    return cells_data


def generate_demo_points(
    *, scope: str = "municipio", count: int = 72, seed: int | None = None
) -> list[dict]:
    rng = _rng(seed + 101 if seed is not None else None)
    points: list[dict] = []
    for _ in range(count):
        lat, lon = _random_point(rng)
        categoria = rng.choice(DEMO_CATEGORIES)
        estado = rng.choice(DEMO_ESTADOS)
        barrio = rng.choice(DEMO_BARRIOS)
        payload = {
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "categoria": categoria,
            "estado": estado,
            "barrio": barrio,
            "fuente": "demo",
        }
        if scope == "pyme":
            payload["total"] = round(rng.uniform(4500, 95000), 2)
        else:
            payload["count"] = rng.randint(1, 4)
        points.append(payload)
    return points


__all__ = [
    "generate_demo_heatmap_cells",
    "generate_demo_points",
    "DEMO_CATEGORIES",
    "DEMO_ESTADOS",
    "DEMO_BARRIOS",
    "JUNIN_CENTER",
]

