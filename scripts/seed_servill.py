import sys
import os

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import current_app
from extensions import db
from models import TenantProfile, User, Rubro, WhatsappNumero
import uuid
from datetime import datetime
import re


def normalize_whatsapp_sender(sender: str | None) -> str | None:
    if not sender:
        return None
    candidate = str(sender).strip()
    if not candidate:
        return None
    if candidate.lower().startswith("whatsapp:"):
        candidate = candidate.split(":", 1)[1].strip()
    digits = re.sub(r"\D", "", candidate)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    return f"whatsapp:+{digits}"

def seed_servill():
    # Helper to run logic with or without existing context
    def _run_seeding():
        print("Starting SERVILL seeding...")

        # 1. Ensure Rubro exists (Prerequisite for User)
        rubro = Rubro.query.filter(Rubro.nombre.ilike("indumentaria")).first()
        if not rubro:
             rubro = Rubro(nombre="Indumentaria", clave="indumentaria", es_publico=False)
             db.session.add(rubro)
             db.session.commit()

        # 2. Create User First (Required for Tenant Owner Constraint)
        admin_email = "info@servill.ar"
        admin_user = User.query.filter_by(email=admin_email).first()

        if not admin_user:
            print(f"Creating admin user '{admin_email}'...")
            admin_user = User(
                name="Admin Servill",
                email=admin_email,
                rol="admin_pyme",
                tipo_chat="pyme",
                rubro_id=rubro.id,
                tenant_id=None, # Will update later
                token=str(uuid.uuid4()),
                email_verified=True,
                acepto_terminos=True,
                fecha_aceptacion_terminos=datetime.utcnow()
            )
            admin_user.set_password("Servill2030!")
            db.session.add(admin_user)
            db.session.commit() # Commit to get ID
            print(f"Created Admin User ID: {admin_user.id}")
        else:
            print(f"Admin user '{admin_email}' already exists (ID: {admin_user.id}).")

        # 3. Create Tenant (Now we have the owner ID)
        tenant_slug = "servill"
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        whatsapp_sender_raw = "whatsapp:+5492634947679"
        whatsapp_sender = normalize_whatsapp_sender(whatsapp_sender_raw)

        if not tenant:
            print(f"Creating tenant '{tenant_slug}'...")
            tenant = TenantProfile(
                slug=tenant_slug,
                nombre="SERVILL Indumentaria",
                tipo="pyme",
                pyme_id=admin_user.id, # Satisfy Check Constraint
                whatsapp_sender_id=whatsapp_sender,
                whatsapp_sender=whatsapp_sender,
                plan="pyme"
            )
            db.session.add(tenant)
            db.session.commit() # Commit to get ID
            print(f"Created Tenant ID: {tenant.id}")
        else:
            print(f"Updating tenant '{tenant_slug}'...")
            tenant.whatsapp_sender_id = whatsapp_sender
            tenant.whatsapp_sender = whatsapp_sender
            tenant.nombre = "SERVILL Indumentaria"
            if not tenant.pyme_id:
                tenant.pyme_id = admin_user.id
            db.session.add(tenant)
            db.session.commit()

        # 4. Link User to Tenant (Backref)
        if admin_user.tenant_id != tenant.id:
            print(f"Linking User {admin_user.id} to Tenant {tenant.id}...")
            admin_user.tenant_id = tenant.id
            db.session.add(admin_user)
            db.session.commit()

        # 5. Create WhatsappNumero Mapping
        # The logs show incoming: To=+5492634947679
        target_number = "+5492634947679"

        wn = WhatsappNumero.query.filter_by(numero_whatsapp=target_number).first()
        if not wn:
            print(f"Creating WhatsappNumero mapping for {target_number} -> User {admin_user.id}...")
            wn = WhatsappNumero(
                numero_whatsapp=target_number,
                user_id=admin_user.id,
                is_active=True
            )
            db.session.add(wn)
            db.session.commit()
        else:
            if wn.user_id != admin_user.id:
                print(f"Updating WhatsappNumero {target_number} to point to User {admin_user.id} (was {wn.user_id})...")
                wn.user_id = admin_user.id
                db.session.add(wn)
                db.session.commit()
            else:
                print(f"WhatsappNumero mapping for {target_number} is correct.")

        print("✅ SERVILL seeding completed successfully.")

    if current_app:
        _run_seeding()
    else:
        print("⚠️ seed_servill called without active app context. Skipping.")

if __name__ == "__main__":
    from app import app
    with app.app_context():
        seed_servill()
