from app import app
from models import db, TenantProfile, User, CatalogoItem
from services.tenant_factory import create_tenant_from_template

def seed_market_tenants():
    with app.app_context():
        # 1. Cuatro Fincas (Bodega)
        slug = "cuatrofincas"
        tenant = TenantProfile.query.filter_by(slug=slug).first()
        if not tenant:
            print(f"Creating {slug}...")
            try:
                tenant = create_tenant_from_template(
                    slug=slug,
                    nombre="Cuatro Fincas",
                    tipo="pyme",
                    template_key="bodega",
                    owner_email="admin@cuatrofincas.com",
                    owner_password="password123",
                    allow_existing_owner=True,
                )
            except Exception as e:
                print(f"Error creating {slug}: {e}")
        else:
            print(f"{slug} already exists.")

        # 2. Servill (Ropa/Retail)
        slug = "servill"
        tenant = TenantProfile.query.filter_by(slug=slug).first()
        if not tenant:
            print(f"Creating {slug}...")
            try:
                tenant = create_tenant_from_template(
                    slug=slug,
                    nombre="Servill Tienda",
                    tipo="pyme",
                    template_key="local_comercial_general",
                    owner_email="admin@servill.com",
                    owner_password="password123",
                    allow_existing_owner=True,
                )
            except Exception as e:
                print(f"Error creating {slug}: {e}")
        else:
            print(f"{slug} already exists.")

        print("Market tenants seeded/verified.")

if __name__ == "__main__":
    seed_market_tenants()
