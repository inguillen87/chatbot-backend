from __future__ import annotations

import random
from typing import Iterable, List, Optional

from models import CatalogoItem, CatalogoKit, TenantProfile


def listar_kits(tenant: TenantProfile) -> List[dict]:
    return [kit.to_dict() for kit in CatalogoKit.query.filter_by(tenant_id=tenant.id).all()]


def sugerir_kits_para_carrito(tenant: TenantProfile, cart_items: Iterable[CatalogoItem]) -> List[dict]:
    kits = listar_kits(tenant)
    if not kits:
        return []
    sample_size = min(len(kits), 3)
    return random.sample(kits, sample_size)


def guardar_kit(tenant: TenantProfile, nombre: str, descripcion: str, items: list, precio_especial: Optional[float], moneda: str = "ARS") -> CatalogoKit:
    kit = CatalogoKit(
        tenant_id=tenant.id,
        nombre=nombre,
        descripcion=descripcion,
        items=items,
        precio_especial=precio_especial,
        moneda=moneda,
    )
    CatalogoKit.query.filter_by(tenant_id=tenant.id, nombre=nombre).delete()
    from database import db

    db.session.add(kit)
    db.session.commit()
    return kit

