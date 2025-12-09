# scripts/backfill_legacy_tenants.py

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app
from extensions import db
from models import User, TenantProfile

app = create_app()

def run_backfill():
    with app.app_context():
        # 1. Backfill Municipio
        municipio_tenant = TenantProfile.query.filter_by(slug="municipio").first()
        if not municipio_tenant:
            print("⚠️ Tenant 'municipio' not found. Skipping.")
        else:
            print(f"Found tenant 'municipio' (ID: {municipio_tenant.id})")

            # Example user mentioned
            mauri = User.query.filter_by(email="mauricio@junin.com").first()
            if mauri:
                print(f"Fixing Mauricio: {mauri.id} (Current Tenant: {mauri.tenant_id})")
                mauri.tenant_id = municipio_tenant.id
                mauri.tenant_slug = municipio_tenant.slug
                db.session.add(mauri)

            # Backfill all users with tipo_chat='municipio' and no tenant_id
            legacy_users = User.query.filter(
                User.tipo_chat == "municipio",
                User.tenant_id.is_(None)
            ).all()

            count = 0
            for u in legacy_users:
                u.tenant_id = municipio_tenant.id
                u.tenant_slug = municipio_tenant.slug
                db.session.add(u)
                count += 1
            print(f"Backfilled {count} legacy municipio users.")

        # 2. General backfill for others if needed (optional but good)
        # Find all tenants owned by someone
        tenants = TenantProfile.query.filter(TenantProfile.municipio_id.isnot(None)).all()
        for t in tenants:
            # Users belonging to this municipio (by municipio_id link)
            users = User.query.filter(
                User.municipio_id == t.municipio_id,
                User.tenant_id.is_(None)
            ).all()
            for u in users:
                u.tenant_id = t.id
                u.tenant_slug = t.slug
                db.session.add(u)

        db.session.commit()
        print("Backfill complete.")

if __name__ == "__main__":
    run_backfill()
