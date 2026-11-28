"""Ready-made catalogs an organization can load in one step.

«Beneficios ciudadanos» turns a municipality's marketplace into a social policy
tool: people redeem the points they earn by participating (surveys, votes,
public-works feedback) for school kits, scholarships or vouchers, sign up for
free talks and services, buy a raffle number or donate. Every item is generic
(no real brands, programs or people) and the organization edits it afterwards.

Loading is idempotent: items are matched by SKU, so loading again updates the
template's items and never duplicates or touches the organization's own ones.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CONTRACT = "chatboc.marketplace_template.v1"


@dataclass(frozen=True)
class TemplateItem:
    sku: str
    nombre: str
    categoria: str
    modalidad: str  # venta | canje | donacion | inscripcion
    descripcion: str
    precio: str | None = None
    puntos: int | None = None
    cupos: int | None = None


BENEFICIOS_CIUDADANOS: tuple[TemplateItem, ...] = (
    # Education
    TemplateItem("tpl-benef-kit-escolar", "Kit escolar completo", "Educación", "canje",
                 "Útiles para un año de primaria: cuadernos, lápices, regla y mochila.", puntos=1500, cupos=300),
    TemplateItem("tpl-benef-beca-estudio", "Beca de apoyo al estudio", "Educación", "canje",
                 "Ayuda mensual por un cuatrimestre para estudios secundarios, terciarios o de oficio.",
                 puntos=6000, cupos=40),
    TemplateItem("tpl-benef-taller-oficios", "Taller de oficios", "Educación", "inscripcion",
                 "Electricidad, plomería o gastronomía. Clases gratuitas con certificado.", precio="Gratis", cupos=60),
    # Health
    TemplateItem("tpl-benef-desc-remedios", "Descuento en medicamentos", "Salud", "canje",
                 "Voucher de descuento en farmacias adheridas, válido por 30 días.", puntos=800, cupos=500),
    TemplateItem("tpl-benef-control-visual", "Control visual y anteojos", "Salud", "inscripcion",
                 "Control gratuito en el centro de salud y anteojos sin cargo si los necesitás.",
                 precio="Gratis", cupos=120),
    # Inclusion and disability
    TemplateItem("tpl-benef-traslado-accesible", "Traslado accesible a turnos", "Inclusión", "inscripcion",
                 "Vehículo adaptado para ir y volver de turnos médicos o de rehabilitación.", precio="Gratis", cupos=80),
    TemplateItem("tpl-benef-ayudas-tecnicas", "Préstamo de ayudas técnicas", "Inclusión", "inscripcion",
                 "Silla de ruedas, andador o muletas en préstamo mientras los necesites.", precio="Gratis", cupos=50),
    TemplateItem("tpl-benef-lengua-senas", "Curso de lengua de señas", "Inclusión", "inscripcion",
                 "Curso inicial para familias, docentes y vecinos.", precio="Gratis", cupos=40),
    TemplateItem("tpl-benef-kit-accesibilidad", "Kit de accesibilidad en casa", "Inclusión", "canje",
                 "Barral de baño, antideslizantes y luz de noche, con instalación.", puntos=3000, cupos=60),
    # Families
    TemplateItem("tpl-benef-charla-crianza", "Charla: primeros años de crianza", "Familias", "inscripcion",
                 "Encuentro para madres, padres y cuidadores con especialistas en salud infantil.",
                 precio="Gratis", cupos=100),
    TemplateItem("tpl-benef-madres-emprendedoras", "Ciclo para madres emprendedoras", "Familias", "inscripcion",
                 "Cuatro encuentros sobre ventas, costos y redes para emprendimientos.", precio="Gratis", cupos=60),
    TemplateItem("tpl-benef-kit-bebe", "Kit de bienvenida al bebé", "Familias", "canje",
                 "Pañales, ropa de recién nacido y guía de controles.", puntos=2500, cupos=100),
    # Culture and events
    TemplateItem("tpl-benef-recital", "Entrada a recital al aire libre", "Cultura y eventos", "canje",
                 "Entrada general para el próximo recital municipal.", puntos=600, cupos=400),
    TemplateItem("tpl-benef-feria-gastronomica", "Voucher feria gastronómica", "Cultura y eventos", "canje",
                 "Degustación para dos personas en la feria de productores locales.", puntos=400, cupos=300),
    TemplateItem("tpl-benef-conferencia", "Conferencia de innovación", "Cultura y eventos", "inscripcion",
                 "Charlas sobre tecnología, empleo y ciudad. Cupo limitado.", precio="Gratis", cupos=250),
    # Local commerce and sport
    TemplateItem("tpl-benef-voucher-comercio", "Voucher para comercios locales", "Comercio local", "canje",
                 "Voucher para gastar en comercios adheridos del barrio.", puntos=2000, cupos=500),
    TemplateItem("tpl-benef-comedor-puntos", "Sumá puntos al comedor", "Solidaridad", "donacion",
                 "Tus puntos se convierten en viandas para el comedor comunitario del barrio.", puntos=200, cupos=5000),
    # Solidarity
    TemplateItem("tpl-benef-rifa-hospital", "Rifa solidaria del hospital", "Solidaridad", "venta",
                 "Número para la rifa a beneficio del hospital público. Se sortea en vivo.", precio="2000", cupos=1000),
    TemplateItem("tpl-benef-donar-utiles", "Donar útiles escolares", "Solidaridad", "donacion",
                 "Sumate a la colecta: dejá tus útiles en cualquier centro vecinal.", cupos=1000),
    TemplateItem("tpl-benef-arbol-nativo", "Árbol nativo para tu vereda", "Ambiente", "inscripcion",
                 "Lo plantamos con vos. Elegís la especie según el ancho de la vereda.", precio="Gratis", cupos=150),
)

TEMPLATES: dict[str, dict[str, Any]] = {
    "beneficios_ciudadanos": {
        "title": "Beneficios ciudadanos",
        "summary": "Canjes por participación, becas, salud, inclusión, familias, eventos, rifa y donaciones.",
        "items": BENEFICIOS_CIUDADANOS,
    },
}


def catalog() -> list[dict[str, Any]]:
    return [{"key": key, "title": value["title"], "summary": value["summary"], "items": len(value["items"])}
            for key, value in TEMPLATES.items()]


class TemplateError(ValueError):
    pass


def apply_template(tenant: Any, key: str, *, fallback_owner_id: int | None = None) -> dict[str, Any]:
    """Create or update the template's items in this tenant's catalog (by SKU)."""
    from models import CatalogoItem, User, db

    template = TEMPLATES.get(key)
    if template is None:
        raise TemplateError("template_not_found")
    owner_id = getattr(tenant, "municipio_id", None) or fallback_owner_id
    if not owner_id or db.session.get(User, int(owner_id)) is None:
        raise TemplateError("tenant_owner_missing")
    existing = {item.sku: item for item in CatalogoItem.query.filter(
        CatalogoItem.tenant_id == tenant.id,
        CatalogoItem.sku.in_([entry.sku for entry in template["items"]])).all()}
    created = updated = 0
    for entry in template["items"]:
        row = existing.get(entry.sku)
        if row is None:
            row = CatalogoItem(user_id=int(owner_id), tenant_id=tenant.id, sku=entry.sku)
            db.session.add(row)
            created += 1
        else:
            updated += 1
        row.nombre = entry.nombre
        row.categoria = entry.categoria
        # The catalog stores sale/donation/redemption; a free sign-up is a sale «Gratis».
        row.modalidad = "venta" if entry.modalidad == "inscripcion" else entry.modalidad
        row.descripcion = entry.descripcion
        row.descripcion_corta = entry.descripcion[:200]
        row.precio = entry.precio
        row.precio_puntos = entry.puntos
        row.cantidad = str(entry.cupos) if entry.cupos is not None else None
        row.unidad = "cupo" if entry.modalidad == "inscripcion" else row.unidad
        row.disponible = True
        row.checkout_type = "chatboc"
        metadata = dict(row.extra_metadata or {}) if isinstance(row.extra_metadata, dict) else {}
        metadata["template"] = key
        metadata["kind"] = entry.modalidad
        row.extra_metadata = metadata
    # Items this template loaded before and no longer offers are hidden, never deleted.
    current = {entry.sku for entry in template["items"]}
    retired = 0
    for row in CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant.id,
                                         CatalogoItem.sku.like("tpl-%")).all():
        metadata = row.extra_metadata if isinstance(row.extra_metadata, dict) else {}
        if metadata.get("template") == key and row.sku not in current and row.disponible is not False:
            row.disponible = False
            retired += 1
    db.session.commit()
    return {"contract_version": CONTRACT, "template": key, "created": created, "updated": updated,
            "retired": retired, "items": len(template["items"])}
