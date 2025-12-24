import sys
import os

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import current_app
from extensions import db
from models import TenantProfile, User, Rubro, WhatsappNumero
from services.tenant_management.folder_manager import ensure_tenant_folder_structure
import uuid
from datetime import datetime

def seed_servill():
    # Helper to run logic with or without existing context
    def _run_seeding():
        print("Starting SERVILL seeding...")

        # 1. Ensure Admin User Exists (First, to satisfy Tenant foreign key if needed)
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
                rol="admin_pyme",
                tipo_chat="pyme",
                rubro_id=rubro.id,
                token=str(uuid.uuid4()),
                email_verified=True,
                acepto_terminos=True,
                fecha_aceptacion_terminos=datetime.utcnow()
            )
            admin_user.set_password("Servill2030!")
            db.session.add(admin_user)
            db.session.commit()
        else:
            print(f"Found admin user '{admin_email}'.")
            if not admin_user.rubro_id:
                admin_user.rubro_id = rubro.id
                db.session.add(admin_user)
                db.session.commit()

        # 2. Ensure Tenant Exists
        tenant_slug = "servill"
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()

        if not tenant:
            print(f"Creating tenant '{tenant_slug}'...")
            tenant = TenantProfile(
                slug=tenant_slug,
                nombre="SERVILL Indumentaria",
                tipo="pyme",
                whatsapp_sender_id="whatsapp:+5492634947679",
                plan="pyme",
                pyme_id=admin_user.id # Satisfies CK constraint
            )
            db.session.add(tenant)
            db.session.commit()
        else:
            print(f"Updating tenant '{tenant_slug}'...")
            tenant.whatsapp_sender_id = "whatsapp:+5492634947679"
            tenant.nombre = "SERVILL Indumentaria"
            if not tenant.pyme_id:
                tenant.pyme_id = admin_user.id
            db.session.add(tenant)
            db.session.commit()

        # 3. Link User back to Tenant
        if admin_user.tenant_id != tenant.id:
            print(f"Linking user '{admin_email}' to tenant '{tenant_slug}'...")
            admin_user.tenant_id = tenant.id
            db.session.add(admin_user)
            db.session.commit()

        # 4. Ensure WhatsappNumero mapping exists
        target_number = "+5492634947679"
        mapping = WhatsappNumero.query.filter_by(numero_whatsapp=target_number).first()
        if not mapping:
            print(f"Creating WhatsApp mapping for {target_number}...")
            mapping = WhatsappNumero(
                numero_whatsapp=target_number,
                user_id=admin_user.id,
                is_active=True
            )
            db.session.add(mapping)
            db.session.commit()
        else:
            print(f"WhatsApp mapping for {target_number} already exists.")
            if mapping.user_id != admin_user.id:
                print(f"Updating mapping user to {admin_user.id}...")
                mapping.user_id = admin_user.id
                db.session.add(mapping)
                db.session.commit()

        # 5. Ensure Professional Folder Structure
        try:
            folder_path = ensure_tenant_folder_structure(tenant_slug, "SERVILL Indumentaria", "pyme")
            print(f"✅ Verified folder structure at {folder_path}")
        except Exception as e:
            print(f"⚠️ Failed to ensure folder structure: {e}")

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
