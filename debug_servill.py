from app import app
from models import User, TenantProfile, Rubro, WhatsappNumero
from extensions import db

with app.app_context():
    print("--- DEBUGGING SERVILL USER & TENANT ---")
    user = User.query.filter_by(email="info@servill.ar").first()
    if not user:
        print("User info@servill.ar NOT FOUND")
    else:
        print(f"User: ID={user.id}, Name={user.name}, Rol={user.rol}, TenantID={user.tenant_id}, TenantSlug={user.tenant_slug}")
        print(f"Ownership: PymeID={user.pyme_id}, MunicipioID={user.municipio_id}, EmpresaID={user.empresa_id}")

        tenant = TenantProfile.query.filter_by(slug="servill").first()
        if not tenant:
            print("Tenant 'servill' NOT FOUND")
        else:
            print(f"Tenant: ID={tenant.id}, Slug={tenant.slug}, PymeID={tenant.pyme_id}, MunicipioID={tenant.municipio_id}")

            # Check linkage
            if tenant.pyme_id == user.id:
                print("✅ Tenant is owned by User (PymeID match)")
            else:
                print(f"❌ Tenant is NOT owned by User (Tenant.pyme_id={tenant.pyme_id} != User.id={user.id})")

            if user.tenant_id == tenant.id:
                print("✅ User is linked to Tenant (TenantID match)")
            else:
                print(f"❌ User is NOT linked to Tenant (User.tenant_id={user.tenant_id} != Tenant.id={tenant.id})")

    # Check 'municipio' tenant to see if user is somehow linked there
    muni = TenantProfile.query.filter_by(slug="municipio").first()
    if muni:
        print(f"Tenant 'municipio': ID={muni.id}, PymeID={muni.pyme_id}, MunicipioID={muni.municipio_id}")
        if user.tenant_id == muni.id:
            print("⚠️ User is linked to 'municipio' tenant!")
