import sys
import os

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import current_app
from extensions import db
from models import TenantProfile, User, Rubro
import uuid
from datetime import datetime

def seed_servill():
    # Helper to run logic with or without existing context
    def _run_seeding():
        print("Starting SERVILL seeding...")

        # 1. Ensure Tenant Exists
        tenant_slug = "servill"
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()

        if not tenant:
            print(f"Creating tenant '{tenant_slug}'...")
            tenant = TenantProfile(
                slug=tenant_slug,
                nombre="SERVILL Indumentaria",
                tipo="pyme",
                whatsapp_sender_id="whatsapp:+5492634947679",
                plan="pyme"
            )
            db.session.add(tenant)
            db.session.commit()
        else:
            print(f"Updating tenant '{tenant_slug}'...")
            tenant.whatsapp_sender_id = "whatsapp:+5492634947679"
            tenant.nombre = "SERVILL Indumentaria"
            db.session.add(tenant)
            db.session.commit()

        # 2. Ensure Admin User Exists
        admin_email = "info@servill.ar"
        admin_user = User.query.filter_by(email=admin_email).first()

        # Rubro (ensure exists or generic)
        rubro = Rubro.query.filter(Rubro.nombre.ilike("indumentaria")).first()
        if not rubro:
             rubro = Rubro(nombre="Indumentaria", clave="indumentaria", es_publico=False)
             db.session.add(rubro)
             db.session.commit()

        if not admin_user:
            print(f"Creating admin user '{admin_email}'...")
            admin_user = User(
                name="Admin Servill",
                email=admin_email,
                rol="admin_pyme", # Or whatever role is appropriate
                tipo_chat="pyme",
                rubro_id=rubro.id,
                token=str(uuid.uuid4()),
                email_verified=True,
                acepto_terminos=True,
                fecha_aceptacion_terminos=datetime.utcnow()
            )
            admin_user.set_password("Servill2030!") # Placeholder password
            db.session.add(admin_user)
            db.session.commit()
        else:
            print(f"Updating admin user '{admin_email}'...")
            if not admin_user.rubro_id:
                admin_user.rubro_id = rubro.id
            db.session.add(admin_user)
            db.session.commit()

        # 2. Ensure Tenant Exists
        tenant_slug = "servill"
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        whatsapp_sender = normalize_whatsapp_sender("whatsapp:+5492634947679")

        if not tenant:
            print(f"Creating tenant '{tenant_slug}'...")
            tenant = TenantProfile(
                slug=tenant_slug,
                nombre="SERVILL Indumentaria",
                tipo="pyme",
                pyme_id=admin_user.id,
                whatsapp_sender_id=whatsapp_sender,
                whatsapp_sender=whatsapp_sender,
                plan="pyme"
            )
            db.session.add(tenant)
            db.session.commit()
        else:
            print(f"Updating tenant '{tenant_slug}'...")
            tenant.whatsapp_sender_id = whatsapp_sender
            tenant.whatsapp_sender = whatsapp_sender
            tenant.nombre = "SERVILL Indumentaria"
            if not tenant.pyme_id:
                tenant.pyme_id = admin_user.id
            db.session.add(tenant)
            db.session.commit()

        # 3. Link Tenant to User (Owner)
        # Assuming Pyme relationship
        if admin_user.tenant_id != tenant.id:
            admin_user.tenant_id = tenant.id
            db.session.add(admin_user)
            db.session.commit()

        print("✅ SERVILL seeding completed successfully.")

    if current_app:
        _run_seeding()
    else:
        # Fallback if somehow called without context (should not happen if pattern followed)
        print("⚠️ seed_servill called without active app context. Skipping.")

if __name__ == "__main__":
    from app import app
    with app.app_context():
        seed_servill()
