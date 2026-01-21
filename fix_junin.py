import os
import sys

# Ensure the current directory is in the path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Monkey patch Config before importing app
from config import Config
Config.SESSION_TYPE = 'filesystem'

from app import create_app, db
from models import User, TenantProfile, FeatureToggle
from werkzeug.security import generate_password_hash

def fix_junin_tenant():
    app = create_app()
    with app.app_context():
        print("\n" + "="*50)
        print("FIX JUNIN TENANT STARTED")
        print("="*50 + "\n")

        # 1. Create or Get User
        email = "mauricio@junin.com"
        user = User.query.filter_by(email=email).first()
        if not user:
            print(f"Creating user '{email}'...")
            user = User(
                email=email,
                name="Mauricio Junin",
                rol="admin",
                tipo_chat="municipio",
                plan="full"
            )
            user.set_password("123456") # Password provided by user
            db.session.add(user)
            db.session.commit()
            print(f"   [CREATED] User ID: {user.id}")
        else:
            print(f"   [EXISTS] User ID: {user.id}")
            # Ensure they are admin and have correct password
            user.rol = "admin"
            user.set_password("123456") # Password provided by user
            db.session.add(user)
            db.session.commit()
            print("   [UPDATED] User role set to 'admin' and password updated.")

        # 2. Create or Get Tenant
        # We use slug 'junin' to ensure it is distinct and identifiable
        slug = "junin"
        tenant = TenantProfile.query.filter_by(slug=slug).first()

        if not tenant:
            print(f"Creating tenant '{slug}'...")
            tenant = TenantProfile(
                slug=slug,
                nombre="Municipalidad de Junin",
                tipo="municipio",
                plan="full",
                municipio_id=user.id, # Link owner
                is_active=True
            )
            db.session.add(tenant)
            db.session.commit()
            print(f"   [CREATED] Tenant ID: {tenant.id}, Slug: {tenant.slug}")
        else:
            print(f"   [EXISTS] Tenant ID: {tenant.id}, Name: {tenant.nombre}")
            # Update attributes just in case
            tenant.nombre = "Municipalidad de Junin"
            tenant.tipo = "municipio"
            tenant.plan = "full"
            tenant.municipio_id = user.id
            tenant.is_active = True
            db.session.add(tenant)
            db.session.commit()
            print("   [UPDATED] Tenant attributes refreshed.")

        # 3. Link User to Tenant (Two-way linking)
        user.tenant_id = tenant.id
        # Also ensure municipio_id on user matches if they are the "municipio" entity user
        # (Though usually tenant_id is sufficient for admin access)

        db.session.add(user)
        db.session.commit()
        print(f"   [LINKED] User {user.id} -> Tenant {tenant.id}")

        print("\n" + "="*50)
        print("FIX FINISHED")
        print("="*50 + "\n")

if __name__ == "__main__":
    fix_junin_tenant()
