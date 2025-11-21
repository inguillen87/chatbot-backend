"""Utility helpers to seed showcase catalog items for public tenants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from flask import current_app

from extensions import db
from models import CatalogoItem, TenantProfile, User


@dataclass(frozen=True)
class SeedItem:
    nombre: str
    categoria: str
    descripcion: str
    precio: str
    unidad: str
    sku: str
    cantidad: str = ""
    marca: Optional[str] = None
    descripcion_corta: Optional[str] = None
    promocion_info: Optional[str] = None
    imagen_url: Optional[str] = None
    modalidad: Optional[str] = None
    precio_puntos: Optional[int] = None

    def to_catalog_kwargs(self) -> Dict[str, Optional[str]]:
        payload = {
            "nombre": self.nombre,
            "categoria": self.categoria,
            "descripcion": self.descripcion,
            "precio": self.precio,
            "unidad": self.unidad,
            "sku": self.sku,
            "cantidad": self.cantidad,
            "marca": self.marca,
            "descripcion_corta": self.descripcion_corta,
            "promocion_info": self.promocion_info,
            "imagen_url": self.imagen_url,
            "modalidad": self.modalidad,
        }

        if self.modalidad:
            payload["modalidad"] = self.modalidad
        if self.precio_puntos is not None:
            payload["precio_puntos"] = self.precio_puntos

        return payload


def _normalize_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    for ch in (" ", "/", "\\", "."):
        normalized = normalized.replace(ch, "-")
    return "-".join(part for part in normalized.split("-") if part)


_JUNIN_ITEMS: Sequence[SeedItem] = (
    SeedItem(
        nombre="Kit escolar solidario",
        categoria="Educación",
        descripcion="Mochila, útiles y abrigo para un estudiante del ciclo básico.",
        descripcion_corta="Completá el kit de un estudiante juninense",
        precio="1500 pts",
        unidad="kit",
        sku="junin-kit-escolar",
        cantidad="Stock solidario: 250 kits",
        promocion_info="Sumá 1.500 puntos participando de encuestas cívicas",
        imagen_url=(
            "https://images.unsplash.com/photo-1503676260728-1c00da094a0b?"
            "auto=format&fit=crop&w=900&q=80"
        ),
        modalidad="canje",
        precio_puntos=1500,
    ),
    SeedItem(
        nombre="Árbol nativo en tu vereda",
        categoria="Ambiente",
        descripcion="Solicitá un fresno americano o aguaribay y el equipo municipal lo planta por vos.",
        descripcion_corta="Adoptá un árbol y sumá puntos verdes",
        precio="0",
        unidad="unidad",
        sku="junin-arbol-nativo",
        cantidad="Agenda abierta: 120 turnos",
        promocion_info="Disponible para donación o puntos verdes",
        imagen_url=(
            "https://images.unsplash.com/photo-1501785888041-af3ef285b470?"
            "auto=format&fit=crop&w=900&q=80"
        ),
        modalidad="donacion",
        precio_puntos=0,
    ),
    SeedItem(
        nombre="Bono de donación Hospital Saporiti",
        categoria="Salud",
        descripcion="Aporte destinado a equipamiento hospitalario y medicamentos oncológicos.",
        descripcion_corta="Convertí tus puntos en una donación",
        precio="2500 pts",
        unidad="bono",
        sku="junin-bono-hospital",
        cantidad="Meta mensual: 80 bonos",
        promocion_info="Cada bono financia 1 kit de insumos críticos",
        imagen_url=(
            "https://images.unsplash.com/photo-1505751172876-fa1923c5c528?"
            "auto=format&fit=crop&w=900&q=80"
        ),
        modalidad="donacion",
        precio_puntos=2500,
    ),
    SeedItem(
        nombre="Bolson saludable kilómetro cero",
        categoria="Producción local",
        descripcion="Verduras y frutas agroecológicas de productores de Junín.",
        descripcion_corta="Bolson agroecológico",
        precio="$4500",
        unidad="bolson",
        sku="junin-bolson-saludable",
        cantidad="Stock semanal: 150 bolsos",
        promocion_info="Beneficio exclusivo vecinos registrados",
        imagen_url=(
            "https://images.unsplash.com/photo-1466978913421-dad2ebd01d17?"
            "auto=format&fit=crop&w=900&q=80"
        ),
        modalidad="compra",
    ),
    SeedItem(
        nombre="Canje de residuos electrónicos",
        categoria="Economía circular",
        descripcion="Entregá tu residuo electrónico y recibí puntos para trámites o beneficios culturales.",
        descripcion_corta="Traé tu e-waste, llevate premios",
        precio="800 pts",
        unidad="canje",
        sku="junin-canje-ewaste",
        cantidad="Cupón mensual: 300 canjes",
        promocion_info="Incluye retiro coordinado para adultos mayores",
        imagen_url=(
            "https://images.unsplash.com/photo-1518770660439-4636190af475?"
            "auto=format&fit=crop&w=900&q=80"
        ),
        modalidad="canje",
        precio_puntos=800,
    ),
)

_DEFAULT_MUNICIPAL_ITEMS: Sequence[SeedItem] = (
    SeedItem(
        nombre="Beca deporte social",
        categoria="Deporte",
        descripcion="Cubre la cuota del polideportivo municipal por un mes.",
        precio="1200 pts",
        unidad="beca",
        sku="muni-beca-deporte",
        cantidad="Cupos disponibles: 60",
        promocion_info="Beneficio para jóvenes que completan encuestas",
        modalidad="canje",
        precio_puntos=1200,
    ),
    SeedItem(
        nombre="Pack huerta urbana",
        categoria="Ambiente",
        descripcion="Incluye semillas, compost y guía para armar tu propia huerta.",
        precio="900 pts",
        unidad="pack",
        sku="muni-pack-huerta",
        cantidad="Stock inicial: 120",
        modalidad="canje",
        precio_puntos=900,
    ),
)

_SEED_BY_KEY: Mapping[str, Sequence[SeedItem]] = {
    "municipalidad-de-junin": _JUNIN_ITEMS,
    "junin": _JUNIN_ITEMS,
    "junin-mendoza": _JUNIN_ITEMS,
}


def _candidate_keys(owner: User, tenant: Optional[TenantProfile]) -> Iterable[str]:
    if tenant and tenant.slug:
        yield tenant.slug
    if tenant and tenant.nombre:
        yield tenant.nombre
    for attr in ("nombre_empresa", "name", "ciudad"):
        value = getattr(owner, attr, None)
        if value:
            yield value
    rubro = getattr(owner, "rubro", None)
    if rubro and getattr(rubro, "nombre", None):
        yield rubro.nombre
    tipo = getattr(owner, "tipo_chat", None)
    if tipo:
        yield tipo


def seed_items_for(owner: User, tenant: Optional[TenantProfile]) -> Sequence[SeedItem]:
    for key in _candidate_keys(owner, tenant):
        normalized = _normalize_key(key)
        if normalized and normalized in _SEED_BY_KEY:
            return _SEED_BY_KEY[normalized]
    if getattr(owner, "tipo_chat", None) == "municipio":
        return _DEFAULT_MUNICIPAL_ITEMS
    return ()


def ensure_seed_catalog(owner: User, tenant: Optional[TenantProfile] = None) -> bool:
    """Create showcase catalog rows for the owner if it still has none."""

    if not owner:
        return False

    existing = (
        CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
        .filter_by(user_id=owner.id)
        .first()
    )
    if existing:
        return False

    seed_items = seed_items_for(owner, tenant)
    if not seed_items:
        return False

    records: List[CatalogoItem] = []
    for seed in seed_items:
        records.append(CatalogoItem(user_id=owner.id, **seed.to_catalog_kwargs()))

    try:
        db.session.add_all(records)
        db.session.commit()
    except Exception:
        current_app.logger.exception(
            "[catalog_seed] No se pudo sembrar el catálogo demo para el owner %s",
            getattr(owner, "id", None),
        )
        db.session.rollback()
        return False

    current_app.logger.info(
        "[catalog_seed] Catalogo demo con %s items creado para owner %s",
        len(records),
        owner.id,
    )
    return True

