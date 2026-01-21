import os
import sys

# Ensure the current directory is in the path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Monkey patch Config before importing app to avoid session conflict
from config import Config
Config.SESSION_TYPE = 'filesystem'  # Override to avoid SQLAlchemy session conflict

from app import create_app, db
from models import User, TenantProfile, FeatureToggle

def run_diagnostics():
    # Use explicit config with filesystem session
    app = create_app()
    with app.app_context():
        print("\n" + "="*50)
        print("DIAGNOSTICS STARTED")
        print("="*50 + "\n")

        # 1. Check for user 'mauricio@junin.com'
        print("1. Checking for user 'mauricio@junin.com'...")
        user = User.query.filter_by(email='mauricio@junin.com').first()
        if user:
            print(f"   [FOUND] User ID: {user.id}, Email: {user.email}, Tenant ID: {user.tenant_id}")
        else:
            print("   [NOT FOUND] User 'mauricio@junin.com' does not exist.")

        # 2. Check for TenantProfile ID 4
        print("\n2. Checking TenantProfile ID 4...")
        tenant = db.session.get(TenantProfile, 4)
        if tenant:
            print(f"   [FOUND] ID: {tenant.id}, Slug: {tenant.slug}, Name: {tenant.nombre}, Type: {tenant.tipo}")
        else:
            print("   [NOT FOUND] TenantProfile ID 4 does not exist.")

        # 3. Search for any tenant with 'junin' in the name or slug
        print("\n3. Searching for tenants with 'junin' in name or slug...")
        tenants = TenantProfile.query.filter(
            (TenantProfile.slug.ilike('%junin%')) |
            (TenantProfile.nombre.ilike('%junin%'))
        ).all()

        if tenants:
            for t in tenants:
                print(f"   [FOUND] ID: {t.id}, Slug: {t.slug}, Name: {t.nombre}, Type: {t.tipo}")
        else:
            print("   [NONE FOUND] No tenants found with 'junin' in name or slug.")

        # 4. List all tenants just in case
        print("\n4. Listing all tenants (ID, Slug, Name):")
        all_tenants = TenantProfile.query.order_by(TenantProfile.id).all()
        for t in all_tenants:
            print(f"   ID: {t.id}, Slug: {t.slug}, Name: {t.nombre}, Type: {t.tipo}")

        print("\n" + "="*50)
        print("DIAGNOSTICS FINISHED")
        print("="*50 + "\n")

if __name__ == "__main__":
    run_diagnostics()
