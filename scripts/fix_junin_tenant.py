import sys
import os
import argparse

# Add the project root to sys.path to ensure modules are found
# We need to go up one level from 'scripts' to get to the root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app
from models import User, TenantProfile, Rubro
from extensions import db
from sqlalchemy import desc

def fix_junin_tenant(password=None):
    with app.app_context():
        print("--- 🔧 STARTING RECOVERY FOR JUNIN TENANT ---")

        # 1. Define Target Data
        TARGET_EMAIL = "mauricio@junin.com"
        TARGET_SLUG = "junin"
        TARGET_NAME = "Municipalidad de Junin"
        TARGET_PLAN = "full"
        # Use provided password or fallback to environment variable, else fail
        TARGET_PASS = password or os.environ.get("JUNIN_USER_PASSWORD")

        if not TARGET_PASS:
            # Check if user exists to decide if password is strictly required
            existing_user = User.query.filter_by(email=TARGET_EMAIL).first()
            if not existing_user:
                print("❌ ERROR: Password required to create new user. Provide via --password argument or JUNIN_USER_PASSWORD env var.")
                return
            else:
                print("ℹ️  User exists. Password not provided; skipping password update.")

        # 2. Check/Fix User
        user = User.query.filter_by(email=TARGET_EMAIL).first()
        if not user:
            print(f"❌ User {TARGET_EMAIL} not found. Creating...")
            user = User(
                email=TARGET_EMAIL,
                name="Mauricio Junin",
                rol="admin",
                plan=TARGET_PLAN,
                tipo_chat="municipio",
                is_active=True
            )
            user.set_password(TARGET_PASS)
            db.session.add(user)
            db.session.flush() # Get ID
            print(f"✅ Created User ID: {user.id}")
        else:
            print(f"✓ User found (ID: {user.id}). Updating plan/role...")
            user.plan = TARGET_PLAN
            user.rol = "admin"
            if TARGET_PASS:
                user.set_password(TARGET_PASS)
                print("✓ Password updated.")

        # 3. Check/Fix Tenant
        tenant = TenantProfile.query.filter_by(slug=TARGET_SLUG).first()
        if not tenant:
            # Try finding by name just in case slug was changed
            tenant = TenantProfile.query.filter(TenantProfile.nombre.ilike(f"%{TARGET_NAME}%")).first()

        if not tenant:
            print(f"❌ Tenant '{TARGET_SLUG}' not found. Creating...")
            tenant = TenantProfile(
                slug=TARGET_SLUG,
                nombre=TARGET_NAME,
                tipo="municipio",
                plan=TARGET_PLAN,
                is_active=True,
                municipio_id=user.id # Link as owner immediately
            )
            db.session.add(tenant)
            db.session.flush()
            print(f"✅ Created Tenant ID: {tenant.id}")
        else:
            print(f"✓ Tenant found (ID: {tenant.id}, Slug: {tenant.slug}). Updating...")
            tenant.slug = TARGET_SLUG # Enforce slug
            tenant.nombre = TARGET_NAME
            tenant.tipo = "municipio"
            tenant.plan = TARGET_PLAN
            tenant.is_active = True
            tenant.municipio_id = user.id # Enforce ownership
            # Clear pyme_id to avoid constraint error
            tenant.pyme_id = None

        # 4. Link User to Tenant (Bi-directional)
        user.tenant_id = tenant.id
        user.municipio_id = tenant.id # Helper field often used in legacy

        # 5. Commit
        try:
            db.session.commit()
            print("\n✅ SUCCESS: Database updated successfully.")
            print(f"User: {user.email} (ID {user.id})")
            print(f"Tenant: {tenant.nombre} (ID {tenant.id})")
            print(f"Link: User.tenant_id={user.tenant_id}, Tenant.municipio_id={tenant.municipio_id}")

            print("\n--- 📋 VERIFICATION: SUPER ADMIN LIST ---")
            query = TenantProfile.query.order_by(desc(TenantProfile.created_at)).limit(20)
            for t in query.all():
                 owner_email = "N/A"
                 if t.municipio: owner_email = t.municipio.email
                 elif t.pyme: owner_email = t.pyme.email

                 status = "ACTIVO" if t.is_active else "INACTIVO"
                 print(f"ID={t.id}\tSlug={t.slug}\tName={t.nombre}\tPlan={t.plan}\tStatus={status}\tOwner={owner_email}")

        except Exception as e:
            db.session.rollback()
            print(f"\n❌ ERROR during commit: {str(e)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix Junin Tenant Data")
    parser.add_argument("--password", help="Password for the user", default=None)
    args = parser.parse_args()

    fix_junin_tenant(password=args.password)
