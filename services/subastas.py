from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "subastas.json"


def _parse_datetime(value: str | None) -> Optional[datetime]:
    if not value:
        return None

    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _normalize_subasta(entry: dict) -> dict:
    return {
        "id": str(entry.get("id") or entry.get("slug") or ""),
        "titulo": entry.get("titulo") or entry.get("title") or "",
        "descripcion": entry.get("descripcion") or entry.get("description") or "",
        "fecha_publicacion": entry.get("fecha_publicacion") or entry.get("fecha_inicio") or "",
        "fecha_cierre": entry.get("fecha_cierre") or entry.get("fecha_fin") or "",
        "precio_base": entry.get("precio_base"),
        "moneda": entry.get("moneda") or "ARS",
        "estado": entry.get("estado") or entry.get("status") or "activa",
        "imagen_url": entry.get("imagen_url") or entry.get("image_url"),
        "etiquetas": entry.get("etiquetas") or entry.get("tags") or [],
    }


def _load_subastas() -> List[dict]:
    if not _DATA_PATH.exists():
        return []

    try:
        content = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        if isinstance(content, list):
            return [_normalize_subasta(entry) for entry in content]
    except Exception:
        return []

    return []


def _is_active(subasta: dict, *, now: datetime) -> bool:
    estado = (subasta.get("estado") or "").lower()
    if estado in {"finalizada", "cerrada", "cancelada"}:
        return False

    fecha_publicacion = _parse_datetime(subasta.get("fecha_publicacion"))
    if fecha_publicacion and fecha_publicacion > now:
        return False

    fecha_cierre = _parse_datetime(subasta.get("fecha_cierre"))
    if fecha_cierre and fecha_cierre < now:
        return False

    return True


def listar_subastas_activas(*, now: Optional[datetime] = None) -> List[dict]:
    current_time = now or datetime.now(timezone.utc)
    subastas = _load_subastas()
    activas = [s for s in subastas if _is_active(s, now=current_time)]
    return sorted(
        activas,
        key=lambda s: _parse_datetime(s.get("fecha_cierre")) or current_time,
    )


def obtener_subasta_por_id(subasta_id: str) -> Optional[dict]:
    if not subasta_id:
        return None

    for subasta in _load_subastas():
        if subasta.get("id") == subasta_id or subasta.get("titulo") == subasta_id:
            return subasta
    return None
