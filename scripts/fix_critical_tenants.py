import sys
import os
import argparse

# Add the project root to sys.path to ensure modules are found
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app
from models import User, TenantProfile
from extensions import db
from sqlalchemy import desc

def ensure_user_and_link_tenant(email, password, tenant_slug, tenant_name, tenant_type, plan="full"):
    """
    Creates/Updates a user and links them as the owner of a specific tenant.
    """
    print(f"\n--- 🔧 PROCESSING: {email} <-> {tenant_slug} ---")

    # 1. Check/Fix User
    user = User.query.filter_by(email=email).first()
    if not user:
        print(f"❌ User {email} not found. Creating...")
        user = User(
            email=email,
            name=f"Admin {tenant_name}",
            rol="admin",
            plan=plan,
            tipo_chat=tenant_type,
            # User model does not have is_active column, it uses UserMixin
            email_verified=True
        )
        if password:
            user.set_password(password)
        db.session.add(user)
        db.session.flush() # Get ID
        print(f"✅ Created User ID: {user.id}")
    else:
        print(f"✓ User found (ID: {user.id}). Updating plan/role...")
        user.plan = plan
        user.rol = "admin"
        if password:
            user.set_password(password)
            print("✓ Password updated.")

    # 2. Check/Fix Tenant
    # First try by slug
    tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()

    # If not found by slug, try by name (fuzzy match)
    if not tenant:
        tenant = TenantProfile.query.filter(TenantProfile.nombre.ilike(f"%{tenant_name}%")).first()
        if tenant:
             print(f"ℹ️  Found tenant by name match: {tenant.slug} (ID {tenant.id})")

    if not tenant:
        print(f"❌ Tenant '{tenant_slug}' not found. Creating...")
        tenant = TenantProfile(
            slug=tenant_slug,
            nombre=tenant_name,
            tipo=tenant_type,
            plan=plan,
            is_active=True
        )
        if tenant_type == 'municipio':
            tenant.municipio_id = user.id
        else:
            tenant.pyme_id = user.id

        db.session.add(tenant)
        db.session.flush()
        print(f"✅ Created Tenant ID: {tenant.id}")
    else:
        print(f"✓ Tenant found (ID: {tenant.id}, Slug: {tenant.slug}). Updating...")
        tenant.slug = tenant_slug # Enforce slug
        tenant.nombre = tenant_name
        tenant.tipo = tenant_type
        tenant.plan = plan
        tenant.is_active = True

        # Enforce ownership based on type
        if tenant_type == 'municipio':
            tenant.municipio_id = user.id
            tenant.pyme_id = None # Clear conflict
        else:
            tenant.pyme_id = user.id
            tenant.municipio_id = None # Clear conflict

    # 3. Link User to Tenant (Bi-directional)
    user.tenant_id = tenant.id
    if tenant_type == 'municipio':
        user.municipio_id = tenant.id
    else:
        user.pyme_id = tenant.id

    return user, tenant

def fix_critical_tenants(junin_pass=None, bodega_pass=None):
    with app.app_context():
        try:
            # --- CASE 1: JUNIN ---
            # mauricio@junin.com / 123456
            ensure_user_and_link_tenant(
                email="mauricio@junin.com",
                password=junin_pass or os.environ.get("JUNIN_PASS"),
                tenant_slug="junin",
                tenant_name="Municipalidad de Junin",
                tenant_type="municipio",
                plan="full"
            )

            # --- CASE 2: BODEGA CUATRO FINCAS ---
            # franco@cuatrofincas.com / 123456
            ensure_user_and_link_tenant(
                email="franco@cuatrofincas.com",
                password=bodega_pass or os.environ.get("BODEGA_PASS"),
                tenant_slug="bodega-cuatro-fincas", # Enforcing a clear slug
                tenant_name="Bodega Cuatro Fincas",
                tenant_type="pyme",
                plan="full"
            )

            db.session.commit()
            print("\n✅ SUCCESS: Database updated successfully.")

            print("\n--- 📋 VERIFICATION: SUPER ADMIN LIST (Top 20) ---")
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
            raise e

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix Critical Tenants (Junin & Bodega)")
    parser.add_argument("--junin-pass", help="Password for mauricio@junin.com", default=None)
    parser.add_argument("--bodega-pass", help="Password for franco@cuatrofincas.com", default=None)
    args = parser.parse_args()

    fix_critical_tenants(junin_pass=args.junin_pass, bodega_pass=args.bodega_pass)
