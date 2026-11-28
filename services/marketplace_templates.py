"""Ready-made catalogs an organization can load in one step.

Three packs, one per kind of organization: «Beneficios ciudadanos» for
municipalities and governments, «Beneficios para empleados» for companies and
«Comercio y clientes frecuentes» for shops and services.

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
    # How many one person may take; None keeps the default (one for benefits,
    # unlimited for sales). 0 lifts the limit.
    por_persona: int | None = None


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
                 "Entrada general para el próximo recital municipal.", puntos=600, cupos=400, por_persona=2),
    TemplateItem("tpl-benef-feria-gastronomica", "Voucher feria gastronómica", "Cultura y eventos", "canje",
                 "Degustación para dos personas en la feria de productores locales.", puntos=400, cupos=300),
    TemplateItem("tpl-benef-conferencia", "Conferencia de innovación", "Cultura y eventos", "inscripcion",
                 "Charlas sobre tecnología, empleo y ciudad. Cupo limitado.", precio="Gratis", cupos=250),
    # Local commerce and sport
    TemplateItem("tpl-benef-voucher-comercio", "Voucher para comercios locales", "Comercio local", "canje",
                 "Voucher para gastar en comercios adheridos del barrio.", puntos=2000, cupos=500),
    TemplateItem("tpl-benef-comedor-puntos", "Sumá puntos al comedor", "Solidaridad", "donacion",
                 "Tus puntos se convierten en viandas para el comedor comunitario del barrio.", puntos=200, cupos=5000,
                 por_persona=0),
    # Solidarity
    TemplateItem("tpl-benef-rifa-hospital", "Rifa solidaria del hospital", "Solidaridad", "venta",
                 "Número para la rifa a beneficio del hospital público. Se sortea en vivo.", precio="2000", cupos=1000,
                 por_persona=10),
    TemplateItem("tpl-benef-donar-utiles", "Donar útiles escolares", "Solidaridad", "donacion",
                 "Sumate a la colecta: dejá tus útiles en cualquier centro vecinal.", cupos=1000),
    TemplateItem("tpl-benef-arbol-nativo", "Árbol nativo para tu vereda", "Ambiente", "inscripcion",
                 "Lo plantamos con vos. Elegís la especie según el ancho de la vereda.", precio="Gratis", cupos=150),
)

BENEFICIOS_EMPLEADOS: tuple[TemplateItem, ...] = (
    # Wellbeing
    TemplateItem("tpl-emp-dia-libre", "Día libre de cumpleaños", "Bienestar", "canje",
                 "Un día libre para usar en la semana de tu cumpleaños.", puntos=1200, cupos=500),
    TemplateItem("tpl-emp-gimnasio", "Mes de gimnasio", "Bienestar", "canje",
                 "Un mes de pase libre en gimnasios adheridos.", puntos=2500, cupos=200),
    TemplateItem("tpl-emp-psicologia", "Espacio de escucha", "Bienestar", "inscripcion",
                 "Hasta cuatro encuentros confidenciales con profesionales. Sin costo.", precio="Gratis", cupos=150),
    TemplateItem("tpl-emp-pausa-activa", "Pausas activas en la oficina", "Bienestar", "inscripcion",
                 "Quince minutos de movilidad dos veces por semana.", precio="Gratis", cupos=300),
    # Learning
    TemplateItem("tpl-emp-curso-online", "Curso online a elección", "Aprendizaje", "canje",
                 "Un curso de la plataforma de formación, con certificado.", puntos=3000, cupos=200),
    TemplateItem("tpl-emp-ingles", "Clases de inglés", "Aprendizaje", "canje",
                 "Un trimestre de clases grupales en horario laboral.", puntos=5000, cupos=60),
    TemplateItem("tpl-emp-charla-finanzas", "Charla: finanzas personales", "Aprendizaje", "inscripcion",
                 "Presupuesto, ahorro e inversión sin vueltas.", precio="Gratis", cupos=200),
    # Family
    TemplateItem("tpl-emp-kit-escolar", "Kit escolar para tus hijos", "Familia", "canje",
                 "Útiles para el año, uno por hijo en edad escolar.", puntos=1500, cupos=400, por_persona=4),
    TemplateItem("tpl-emp-colonia", "Colonia de vacaciones", "Familia", "canje",
                 "Dos semanas de colonia para hijos de 4 a 12 años.", puntos=4000, cupos=120, por_persona=4),
    TemplateItem("tpl-emp-kit-bebe", "Kit de nacimiento", "Familia", "canje",
                 "Ajuar y pañales para la llegada de un bebé.", puntos=2000, cupos=60),
    # Leisure
    TemplateItem("tpl-emp-cine", "Entradas de cine", "Tiempo libre", "canje",
                 "Dos entradas para cualquier función 2D.", puntos=700, cupos=600, por_persona=2),
    TemplateItem("tpl-emp-cena", "Cena para dos", "Tiempo libre", "canje",
                 "Voucher en restaurantes adheridos.", puntos=3500, cupos=150),
    TemplateItem("tpl-emp-recital", "Entrada a recital", "Tiempo libre", "canje",
                 "Entrada general para el show del mes.", puntos=1800, cupos=100, por_persona=2),
    # Recognition and solidarity
    TemplateItem("tpl-emp-reconocimiento", "Reconocé a un compañero", "Reconocimiento", "donacion",
                 "Regalale puntos a alguien que te ayudó. Le llega con tu mensaje.", puntos=100, cupos=10000,
                 por_persona=0),
    TemplateItem("tpl-emp-voluntariado", "Jornada de voluntariado", "Solidaridad", "inscripcion",
                 "Un día de trabajo con una organización social de la zona.", precio="Gratis", cupos=80),
    TemplateItem("tpl-emp-donar-puntos", "Doná tus puntos a una causa", "Solidaridad", "donacion",
                 "La empresa convierte tus puntos en una donación a la causa del trimestre.", puntos=500,
                 cupos=10000, por_persona=0),
)

COMERCIO_LOCAL: tuple[TemplateItem, ...] = (
    # Sales
    TemplateItem("tpl-com-combo-semana", "Combo de la semana", "Promos", "venta",
                 "Nuestra selección con precio especial. Cambia todos los lunes.", precio="9500", cupos=50),
    TemplateItem("tpl-com-envio-gratis", "Envío sin cargo", "Promos", "canje",
                 "Tu próximo pedido llega sin costo de envío.", puntos=300, cupos=1000),
    TemplateItem("tpl-com-gift-card", "Tarjeta de regalo", "Regalos", "venta",
                 "Para regalar: el que la recibe elige qué llevarse.", precio="15000", cupos=200),
    TemplateItem("tpl-com-descuento-cliente", "10% en tu próxima compra", "Clientes frecuentes", "canje",
                 "Válido por 30 días en el local o por WhatsApp.", puntos=800, cupos=1000),
    TemplateItem("tpl-com-producto-regalo", "Producto de regalo", "Clientes frecuentes", "canje",
                 "Elegí un producto de la góndola de regalos con tu próxima compra.", puntos=1500, cupos=300),
    # Services
    TemplateItem("tpl-com-turno", "Reservá un turno", "Servicios", "inscripcion",
                 "Elegí día y horario y te confirmamos por este chat.", precio="Gratis", cupos=500, por_persona=2),
    TemplateItem("tpl-com-presupuesto", "Pedí un presupuesto", "Servicios", "venta",
                 "Contanos qué necesitás y te pasamos el precio.", cupos=500),
    TemplateItem("tpl-com-taller", "Taller abierto en el local", "Servicios", "inscripcion",
                 "Una tarde para aprender a usar lo que vendemos. Cupo limitado.", precio="Gratis", cupos=30),
    # Community
    TemplateItem("tpl-com-donar-puntos", "Doná tus puntos al comedor", "Comunidad", "donacion",
                 "Convertimos tus puntos en mercadería para el comedor del barrio.", puntos=200, cupos=10000,
                 por_persona=0),
)

TEMPLATES: dict[str, dict[str, Any]] = {
    "beneficios_ciudadanos": {
        "title": "Beneficios ciudadanos",
        "summary": "Canjes por participación, becas, salud, inclusión, familias, eventos, rifa y donaciones.",
        "audiences": ("municipio", "gobierno"),
        "items": BENEFICIOS_CIUDADANOS,
    },
    "beneficios_empleados": {
        "title": "Beneficios para empleados",
        "summary": "Bienestar, formación, familia, tiempo libre, reconocimientos y voluntariado.",
        "audiences": ("empresa",),
        "items": BENEFICIOS_EMPLEADOS,
    },
    "comercio_local": {
        "title": "Comercio y clientes frecuentes",
        "summary": "Promos, tarjetas de regalo, puntos por compras, turnos y presupuestos.",
        "audiences": ("pyme", "comercio"),
        "items": COMERCIO_LOCAL,
    },
}

_AUDIENCE_WORDS = {
    "municipio": ("municipio", "municipal", "municipalidad", "gobierno", "provincia", "comuna", "estado"),
    "empresa": ("empresa", "corporativo", "rrhh", "recursos humanos", "empleados", "industria"),
    "pyme": ("pyme", "comercio", "tienda", "almacen", "gastronomia", "restaurante", "local", "servicios"),
}


def recommended_template(tenant: Any) -> str:
    """The pack that fits this organization: government, company or business."""
    from services.marketplace_whatsapp_flow import _plain

    hints = " ".join(_plain(str(value or "")) for value in (
        getattr(tenant, "tipo", None), getattr(tenant, "vertical", None), getattr(tenant, "subvertical", None)))
    if any(word in hints for word in _AUDIENCE_WORDS["municipio"]):
        return "beneficios_ciudadanos"
    if any(word in hints for word in _AUDIENCE_WORDS["empresa"]):
        return "beneficios_empleados"
    return "comercio_local"


def catalog(tenant: Any = None) -> list[dict[str, Any]]:
    recommended = recommended_template(tenant) if tenant is not None else None
    entries = [{"key": key, "title": value["title"], "summary": value["summary"], "items": len(value["items"]),
                "recommended": key == recommended} for key, value in TEMPLATES.items()]
    return sorted(entries, key=lambda entry: not entry["recommended"])


class TemplateError(ValueError):
    pass


def apply_template(tenant: Any, key: str, *, fallback_owner_id: int | None = None) -> dict[str, Any]:
    """Create or update the template's items in this tenant's catalog (by SKU)."""
    from models import CatalogoItem, User, db

    template = TEMPLATES.get(key)
    if template is None:
        raise TemplateError("template_not_found")
    owner_id = getattr(tenant, "municipio_id", None) or getattr(tenant, "pyme_id", None) or fallback_owner_id
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
        if entry.por_persona is not None:
            metadata["max_per_person"] = entry.por_persona
        else:
            metadata.pop("max_per_person", None)
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
