import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from flask import current_app

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.getcwd()))

from app import create_app, db
from models import TenantProfile, MunicipioPost, EncEncuesta, EncPregunta, EncOpcion, User, CatalogoItem, TenantTicket, MarketOrder, MarketOrderItem

def get_or_create_post(tenant, title, content, type_post="noticia", days_offset=0, image_url=None):
    owner = tenant.municipio or tenant.pyme
    if not owner:
        print(f"⚠️ Tenant {tenant.slug} has no owner user. Skipping post '{title}'.")
        return

    existing = MunicipioPost.query.filter_by(municipio_id=owner.id, titulo=title).first()
    if existing:
        print(f"  - Post '{title}' already exists.")
        return

    post = MunicipioPost(
        municipio_id=owner.id,
        tipo_post=type_post,
        titulo=title,
        descripcion=content,
        fecha_publicacion=datetime.now(timezone.utc) - timedelta(days=days_offset),
        imagen_url=image_url
    )

    if type_post == "evento":
        post.fecha_evento_inicio = datetime.now(timezone.utc) + timedelta(days=days_offset + 5)
        post.fecha_evento_fin = datetime.now(timezone.utc) + timedelta(days=days_offset + 5, hours=3)
        post.ubicacion = "Sede Central"

    db.session.add(post)
    print(f"  + Created Post: {title}")

def get_or_create_survey(tenant, title, slug, questions_data):
    # Surveys are linked via tenant_id directly in EncEncuesta
    existing = EncEncuesta.query.filter_by(tenant_id=tenant.id, slug=slug).first()
    if existing:
        if existing.estado != "publicada":
            existing.estado = "publicada"
            db.session.add(existing)
            print(f"  - Survey '{title}' updated to published.")
        else:
            print(f"  - Survey '{title}' already exists.")
        return

    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug=slug,
        titulo=title,
        descripcion=f"Encuesta de demostración para {tenant.nombre}",
        estado="publicada",
        tipo="opinion",
        inicio_at=datetime.now(timezone.utc) - timedelta(days=1),
        fin_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db.session.add(survey)
    db.session.flush()

    for idx, q_data in enumerate(questions_data):
        q = EncPregunta(
            encuesta_id=survey.id,
            orden=idx,
            texto=q_data["text"],
            tipo=q_data["type"],
            obligatoria=True
        )
        db.session.add(q)
        db.session.flush()

        if "options" in q_data:
            for opt_idx, opt_text in enumerate(q_data["options"]):
                opt = EncOpcion(
                    pregunta_id=q.id,
                    orden=opt_idx,
                    texto=opt_text,
                    valor=opt_text
                )
                db.session.add(opt)

    print(f"  + Created Survey: {title}")

def get_or_create_catalog_item(tenant, name, price, category, image_url=None, description=""):
    owner = tenant.municipio or tenant.pyme
    if not owner:
        print(f"⚠️ Tenant {tenant.slug} has no owner. Skipping product '{name}'.")
        return

    existing = CatalogoItem.query.filter_by(tenant_id=tenant.id, nombre=name).first()
    if existing:
        existing.disponible = True  # Ensure it's available
        existing.precio_monetario = Decimal(str(price))
        existing.precio = str(price)
        existing.imagen_url = image_url
        existing.categoria = category
        db.session.add(existing)
        print(f"  - Product '{name}' updated.")
        return

    item = CatalogoItem(
        tenant_id=tenant.id,
        user_id=owner.id,
        nombre=name,
        precio=str(price),
        precio_monetario=Decimal(str(price)),
        moneda="ARS",
        unidad="unidad",
        categoria=category,
        descripcion=description,
        imagen_url=image_url,
        disponible=True,
        cantidad=100  # Default stock
    )
    db.session.add(item)
    print(f"  + Created Product: {name}")

def create_sample_tickets(tenant):
    """Creates sample tickets for the demo users to populate 'My Claims'."""
    owner = tenant.municipio or tenant.pyme
    if not owner: return

    # Find a user to assign tickets to (owner or create dummy)
    # Ideally we attach to the demo user that logs in, but here we just seed for owner
    # so admins see something.

    # We create tickets for the 'admin' user of the tenant so they see them in their portal view
    user = owner

    existing_tickets = TenantTicket.query.filter_by(tenant_id=tenant.id, user_id=user.id).count()
    if existing_tickets > 0:
        return

    print(f"  + Creating sample tickets for {tenant.slug}...")

    if tenant.slug == "municipio":
        t1 = TenantTicket(tenant_id=tenant.id, user_id=user.id, categoria="Alumbrado", descripcion="Luminaria rota en Plaza San Martín", estado="en_proceso", created_at=datetime.now(timezone.utc)-timedelta(days=2))
        t2 = TenantTicket(tenant_id=tenant.id, user_id=user.id, categoria="Limpieza", descripcion="Solicitud de poda en calle Rivadavia", estado="pendiente", created_at=datetime.now(timezone.utc)-timedelta(days=5))
        db.session.add_all([t1, t2])
    elif tenant.slug == "ferreteria":
        t1 = TenantTicket(tenant_id=tenant.id, user_id=user.id, categoria="Devolución", descripcion="Cambio de taladro por falla", estado="resuelto", created_at=datetime.now(timezone.utc)-timedelta(days=10))
        db.session.add(t1)

    db.session.commit()

def create_sample_orders(tenant):
    """Creates sample orders for demo."""
    owner = tenant.municipio or tenant.pyme
    if not owner: return
    user = owner

    existing = MarketOrder.legacy_safe_query().filter_by(tenant_id=tenant.id, user_id=user.id).count()
    if existing > 0: return

    print(f"  + Creating sample orders for {tenant.slug}...")

    order = MarketOrder(
        tenant_id=tenant.id,
        user_id=user.id,
        status="completed",
        total_monetary=15000,
        contact_name=user.name,
        created_at=datetime.now(timezone.utc) - timedelta(days=3)
    )
    db.session.add(order)
    db.session.commit() # Get ID

    # Add items if products exist
    item = CatalogoItem.query.filter_by(tenant_id=tenant.id).first()
    if item:
        oi = MarketOrderItem(order_id=order.id, product_id=item.id, quantity=2, price_monetary=7500)
        db.session.add(oi)

    db.session.commit()


def _run_seed_logic():
    print("\n🚀 Seeding Demo Content...")

    # 1. Municipio Demo
    tenant = TenantProfile.query.filter_by(slug="municipio").first()
    if tenant:
        print(f"\n🏛️  Seeding 'municipio'...")
        get_or_create_post(tenant, "Campaña de Vacunación 2025", "Acércate a tu centro de salud más cercano. Vacunación gratuita para mayores de 65 años.", "noticia", 2, "https://images.unsplash.com/photo-1606206591513-0a98aa6c7889?auto=format&fit=crop&w=800")
        get_or_create_post(tenant, "Festival de Música Local", "Este fin de semana disfrutá de las mejores bandas en la plaza principal.", "evento", 0, "https://images.unsplash.com/photo-1533174072545-e8d4aa97edf9?auto=format&fit=crop&w=800")
        get_or_create_post(tenant, "Nuevas Obras de Pavimentación", "Comenzamos la repavimentación de la Avenida Principal.", "noticia", 5, "https://images.unsplash.com/photo-1590409459367-154744474705?auto=format&fit=crop&w=800")

        get_or_create_survey(tenant, "Satisfacción Recolección de Residuos", "municipio-residuos", [
            {"text": "¿Cómo califica el servicio de recolección?", "type": "single", "options": ["Excelente", "Bueno", "Regular", "Malo"]},
            {"text": "¿En qué horario prefiere que pase el recolector?", "type": "single", "options": ["Mañana", "Tarde", "Noche"]},
            {"text": "Comentarios adicionales", "type": "text"}
        ])

        get_or_create_survey(tenant, "Participación Ciudadana: Presupuesto 2025", "presupuesto-participativo", [
            {"text": "¿Qué área debería tener prioridad?", "type": "single", "options": ["Salud", "Seguridad", "Espacios Verdes", "Cultura"]},
            {"text": "Proponga un proyecto para su barrio", "type": "text"}
        ])

        # Municipio Services/Products
        get_or_create_catalog_item(tenant, "Entrada Teatro Municipal", 5000, "Cultura", "https://images.unsplash.com/photo-1503095392237-fc74af0aaa08?auto=format&fit=crop&w=800", "Entrada general para la función del sábado.")
        get_or_create_catalog_item(tenant, "Bono Contribución Hospital", 2000, "Salud", "https://images.unsplash.com/photo-1538108149393-fbbd81895907?auto=format&fit=crop&w=800", "Ayuda a comprar insumos médicos.")
        get_or_create_catalog_item(tenant, "Licencia de Conducir (Renovación)", 8500, "Trámites", "https://images.unsplash.com/photo-1555881400-74d7acaacd25?auto=format&fit=crop&w=800", "Pago online de tasa administrativa.")

        create_sample_tickets(tenant)
        create_sample_orders(tenant)

    else:
        print("⚠️ Tenant 'municipio' not found.")

    # 2. Bodega Demo
    tenant = TenantProfile.query.filter_by(slug="bodega").first()
    if tenant:
        print(f"\n🍷 Seeding 'bodega'...")
        get_or_create_post(tenant, "Lanzamiento Malbec Reserva 2024", "Ya está disponible nuestra nueva etiqueta premiada. Conseguila con descuento lanzamiento.", "noticia", 1, "https://images.unsplash.com/photo-1510812431401-41d2bd2722f3?auto=format&fit=crop&w=800")
        get_or_create_post(tenant, "Cata Exclusiva al Atardecer", "Vení a disfrutar de una experiencia única en nuestros viñedos.", "evento", -2, "https://images.unsplash.com/photo-1560505183-b9375e2eb27e?auto=format&fit=crop&w=800")

        get_or_create_survey(tenant, "Experiencia de Visita", "bodega-visita", [
            {"text": "¿Qué le pareció la visita guiada?", "type": "single", "options": ["Increíble", "Muy buena", "Buena", "Podría mejorar"]},
            {"text": "¿Cuál fue su vino favorito?", "type": "text"}
        ])

        # Bodega Products
        get_or_create_catalog_item(tenant, "Malbec Reserva 2021", 12500, "Vinos Tintos", "https://images.unsplash.com/photo-1584916201218-f4242ceb4809?auto=format&fit=crop&w=800", "Notas de frutos rojos y vainilla. Crianza de 12 meses en roble.")
        get_or_create_catalog_item(tenant, "Cabernet Sauvignon", 9800, "Vinos Tintos", "https://images.unsplash.com/photo-1559563362-c667ba5f5480?auto=format&fit=crop&w=800", "Cuerpo robusto y especiado. Ideal para carnes rojas.")
        get_or_create_catalog_item(tenant, "Caja Degustación (6 u.)", 55000, "Promociones", "https://images.unsplash.com/photo-1506377247377-2a5b3b417ebb?auto=format&fit=crop&w=800", "Mix de nuestras mejores etiquetas.")

        create_sample_orders(tenant)

    # 3. Ferretería Demo
    tenant = TenantProfile.query.filter_by(slug="ferreteria").first()
    if tenant:
        print(f"\n🛠️  Seeding 'ferreteria'...")
        get_or_create_post(tenant, "Semana de las Herramientas Eléctricas", "Hasta 30% OFF en taladros y amoladoras. Solo por esta semana.", "promocion", 0, "https://images.unsplash.com/photo-1581783342308-f792db027f2a?auto=format&fit=crop&w=800")

        get_or_create_survey(tenant, "Encuesta de Clientes", "ferreteria-clientes", [
            {"text": "¿Encontró lo que buscaba?", "type": "boolean"},
            {"text": "¿Cómo califica la atención?", "type": "stars"}
        ])

        # Ferreteria Products
        get_or_create_catalog_item(tenant, "Taladro Percutor 700W", 85000, "Herramientas Eléctricas", "https://images.unsplash.com/photo-1504148455328-c376907d081c?auto=format&fit=crop&w=800", "Mandril de 13mm, velocidad variable y reversible.")
        get_or_create_catalog_item(tenant, "Set de Destornilladores (10 pz)", 15000, "Herramientas Manuales", "https://images.unsplash.com/photo-1530124566582-a618bc2615dc?auto=format&fit=crop&w=800", "Puntas magnéticas, mango ergonómico.")
        get_or_create_catalog_item(tenant, "Martillo Galponero", 12500, "Herramientas Manuales", "https://images.unsplash.com/photo-1586864387967-d02ef85d93e8?auto=format&fit=crop&w=800", "Mango de fibra de vidrio.")

        create_sample_tickets(tenant)
        create_sample_orders(tenant)

    db.session.commit()
    print("\n✅ Seeding complete.")

def seed_content():
    if current_app:
        # Use existing context (e.g., from init_tenants.py or flask shell)
        _run_seed_logic()
    else:
        # Create new app (e.g., running this script directly)
        app = create_app()
        with app.app_context():
            _run_seed_logic()

if __name__ == "__main__":
    seed_content()
