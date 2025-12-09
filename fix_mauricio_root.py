from app import app
from models import User, TenantProfile, db

def fix_permissions():
    with app.app_context():
        email = "mauricio@junin.com"
        user = User.query.filter_by(email=email).first()
        if not user:
            print(f"❌ User {email} not found")
            return

        tenant = TenantProfile.query.filter_by(slug="municipio").first()
        if not tenant:
            print("❌ Tenant 'municipio' not found")
            return

        print(f"✅ Found User: {user.id} ({user.email}), Tenant: {tenant.id} ({tenant.slug})")

        # Link user to tenant explicitly
        if user.tenant_id != tenant.id:
            print(f"⚠️ Updating tenant_id for user {user.id} to {tenant.id}")
            user.tenant_id = tenant.id
            db.session.add(user)
            db.session.commit()
            print("✅ Permissions fixed: tenant_id set.")
        else:
            print("✅ User already has correct tenant_id.")

if __name__ == "__main__":
    fix_permissions()
