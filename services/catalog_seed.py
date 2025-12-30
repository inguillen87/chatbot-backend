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
    talles: Optional[str] = None
    colores: Optional[str] = None

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

        extra_metadata = {}
        if self.talles:
            extra_metadata["talles"] = self.talles
        if self.colores:
            extra_metadata["colores"] = self.colores

        if extra_metadata:
            payload["extra_metadata"] = extra_metadata

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

_FARMACIA_ITEMS: Sequence[SeedItem] = (
    SeedItem(
        nombre="Paracetamol 500mg",
        categoria="Medicamentos",
        descripcion="Analgésico y antipirético.",
        precio="$1200",
        unidad="caja",
        sku="farma-paracetamol",
        cantidad="Stock disponible",
        imagen_url="https://images.unsplash.com/photo-1584308666744-24d5c474f2ae?auto=format&fit=crop&w=900&q=80",
    ),
    SeedItem(
        nombre="Crema Hidratante Facial",
        categoria="Cuidado Personal",
        descripcion="Hidratación profunda para pieles sensibles.",
        precio="$8500",
        unidad="pote",
        sku="farma-crema-facial",
        cantidad="Stock disponible",
        imagen_url="https://images.unsplash.com/photo-1611080541599-8c6dbde6edb8?auto=format&fit=crop&w=900&q=80",
    ),
)

_LOGISTICA_ITEMS: Sequence[SeedItem] = (
    SeedItem(
        nombre="Envío Estándar AMBA",
        categoria="Envíos",
        descripcion="Entrega en 48hs hábiles en CABA y GBA.",
        precio="$3500",
        unidad="envio",
        sku="log-envio-amba",
        cantidad="Servicio disponible",
    ),
    SeedItem(
        nombre="Pack Embalaje Frágil",
        categoria="Insumos",
        descripcion="Caja reforzada y plástico burbuja.",
        precio="$1200",
        unidad="pack",
        sku="log-pack-fragil",
        cantidad="Stock disponible",
    ),
)

_GENERIC_B2B_ITEMS: Sequence[SeedItem] = (
    SeedItem(
        nombre="Consultoría IT - Hora",
        categoria="Servicios",
        descripcion="Asesoramiento técnico especializado.",
        precio="$25000",
        unidad="hora",
        sku="b2b-consultoria",
        cantidad="Agenda disponible",
    ),
    SeedItem(
        nombre="Licencia Software Enterprise",
        categoria="Software",
        descripcion="Licencia anual para empresas.",
        precio="$150000",
        unidad="licencia",
        sku="b2b-licencia",
        cantidad="Disponible",
    ),
)

_BODEGA_ITEMS: Sequence[SeedItem] = (
    SeedItem(
        nombre="Malbec Reserva 2020",
        categoria="Vinos Tintos",
        descripcion="Crianza de 12 meses en barrica de roble francés. Notas de ciruela y vainilla.",
        descripcion_corta="Tinto con cuerpo y estructura",
        precio="$8500",
        unidad="botella",
        sku="bod-malbec-reserva",
        cantidad="Cajas disponibles",
        imagen_url="https://images.unsplash.com/photo-1559563362-c667ba5f5480?auto=format&fit=crop&w=900&q=80",
    ),
    SeedItem(
        nombre="Caja Degustación (6 botellas)",
        categoria="Promociones",
        descripcion="2 Malbec, 2 Cabernet, 2 Chardonnay. Ideal para regalar.",
        descripcion_corta="Mix de varietales seleccionados",
        precio="$45000",
        unidad="caja",
        sku="bod-caja-degustacion",
        cantidad="Disponible",
        imagen_url="https://images.unsplash.com/photo-1510812431401-41d2bd2722f3?auto=format&fit=crop&w=900&q=80",
    ),
    SeedItem(
        nombre="Visita Guiada y Degustación",
        categoria="Enoturismo",
        descripcion="Recorrido por viñedos y bodega con degustación de 4 etiquetas.",
        descripcion_corta="Experiencia en bodega",
        precio="$15000",
        unidad="entrada",
        sku="bod-visita-guiada",
        cantidad="Reserva previa",
        modalidad="reserva",
    ),
)

_FERRETERIA_ITEMS: Sequence[SeedItem] = (
    SeedItem(
        nombre="Taladro Percutor 700W",
        categoria="Herramientas Eléctricas",
        descripcion="Mandril de 13mm, velocidad variable y reversible.",
        precio="$85000",
        unidad="unidad",
        sku="fer-taladro-700w",
        cantidad="3 en stock",
        imagen_url="https://images.unsplash.com/photo-1504148455328-c376907d081c?auto=format&fit=crop&w=900&q=80",
    ),
    SeedItem(
        nombre="Set de Destornilladores (6 piezas)",
        categoria="Herramientas Manuales",
        descripcion="Puntas imantadas, mango ergonómico. Plano y Phillips.",
        precio="$12500",
        unidad="set",
        sku="fer-set-destornilladores",
        cantidad="10 en stock",
        imagen_url="https://images.unsplash.com/photo-1530124566582-a618bc2615dc?auto=format&fit=crop&w=900&q=80",
    ),
    SeedItem(
        nombre="Lata de Pintura Látex Interior 20L",
        categoria="Pinturas",
        descripcion="Blanco mate, alto poder cubritivo.",
        precio="$65000",
        unidad="lata",
        sku="fer-pintura-latex-20l",
        cantidad="Disponible",
        imagen_url="https://images.unsplash.com/photo-1562259949-e8e7689d7828?auto=format&fit=crop&w=900&q=80",
    ),
)

_INDUMENTARIA_ITEMS: Sequence[SeedItem] = (
    SeedItem(
        nombre="Remera Básica Algodón Premium",
        categoria="Remeras",
        descripcion="100% algodón peinado, corte regular fit. Colores varios.",
        precio="$18000",
        unidad="unidad",
        sku="ind-remera-basica",
        cantidad="Stock en todos los talles",
        imagen_url="https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?auto=format&fit=crop&w=900&q=80",
        talles="S, M, L, XL, XXL",
        colores="Blanco, Negro, Azul, Gris",
    ),
    SeedItem(
        nombre="Jean Clásico Corte Recto",
        categoria="Pantalones",
        descripcion="Denim rígido 12oz, lavado stone wash. Durabilidad y estilo clásico.",
        precio="$45000",
        unidad="unidad",
        sku="ind-jean-clasico",
        cantidad="Stock disponible",
        imagen_url="https://images.unsplash.com/photo-1542272454315-4c01d7abdf4a?auto=format&fit=crop&w=900&q=80",
        talles="38 al 48",
    ),
    SeedItem(
        nombre="Buzo Hoodie con Capucha",
        categoria="Abrigos",
        descripcion="Frisa invisible de alta calidad, bolsillo canguro y capucha forrada.",
        precio="$38000",
        unidad="unidad",
        sku="ind-buzo-hoodie",
        cantidad="Últimas unidades",
        imagen_url="https://images.unsplash.com/photo-1556821840-3a63f95609a7?auto=format&fit=crop&w=900&q=80",
        colores="Negro, Gris Melange, Rojo",
    ),
)

_SEED_BY_KEY: Mapping[str, Sequence[SeedItem]] = {
    "municipalidad-de-junin": _JUNIN_ITEMS,
    "junin": _JUNIN_ITEMS,
    "junin-mendoza": _JUNIN_ITEMS,
    "farmacia": _FARMACIA_ITEMS,
    "logistica": _LOGISTICA_ITEMS,
    "empresa": _GENERIC_B2B_ITEMS,
    "soluciones": _GENERIC_B2B_ITEMS,
    "seguros": _GENERIC_B2B_ITEMS,
    "bodega": _BODEGA_ITEMS,
    "vino": _BODEGA_ITEMS,
    "vinos": _BODEGA_ITEMS,
    "ferreteria": _FERRETERIA_ITEMS,
    "construccion": _FERRETERIA_ITEMS,
    "herramientas": _FERRETERIA_ITEMS,
    "indumentaria": _INDUMENTARIA_ITEMS,
    "ropa": _INDUMENTARIA_ITEMS,
    "textil": _INDUMENTARIA_ITEMS,
    "tienda": _INDUMENTARIA_ITEMS,
    "servill": _INDUMENTARIA_ITEMS,
    "servill-ventas": _INDUMENTARIA_ITEMS,
    "moda": _INDUMENTARIA_ITEMS,
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
        kwargs = seed.to_catalog_kwargs()
        if tenant and tenant.id:
            kwargs["tenant_id"] = tenant.id
        records.append(CatalogoItem(user_id=owner.id, **kwargs))

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

