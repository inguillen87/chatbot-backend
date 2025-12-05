
from app import app
from database import db
from models import TenantProfile, User

def list_tenants():
    with app.app_context():
        tenants = TenantProfile.query.all()
        print(f"Found {len(tenants)} tenants:")
        for t in tenants:
            config = t.configuracion or {}
            widget_tokens = config.get("widget_tokens", [])
            print(f"- ID: {t.id}, Slug: '{t.slug}', Nombre: '{t.nombre}', Tokens: {widget_tokens}")

            owner = t.municipio or t.pyme
            if owner:
                print(f"  Owner: ID {owner.id}, Email: {owner.email}")
            else:
                print(f"  Owner: None")

if __name__ == "__main__":
    list_tenants()
