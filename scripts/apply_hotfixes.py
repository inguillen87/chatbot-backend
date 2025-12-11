from app import app, db
from sqlalchemy import text
from models import User, TenantProfile

def fix_schema():
    print("🔧 Checking schema 'catalogo_item.disponible'...")
    try:
        is_sqlite = 'sqlite' in str(db.engine.url)
        exists = False

        if is_sqlite:
            res = db.session.execute(text("PRAGMA table_info(catalogo_item)")).fetchall()
            for row in res:
                if row[1] == 'disponible':
                    exists = True
                    break
        else:
            # Postgres check
            # We use a try/catch block for the check itself to be robust
            try:
                check_sql = text("SELECT column_name FROM information_schema.columns WHERE table_name='catalogo_item' AND column_name='disponible'")
                res = db.session.execute(check_sql).fetchone()
                if res:
                    exists = True
            except Exception as e:
                print(f"  ⚠️ Error checking schema: {e}")

        if not exists:
            print("  ⚠️ Column missing. Attempting ADD COLUMN...")
            try:
                if is_sqlite:
                    db.session.execute(text("ALTER TABLE catalogo_item ADD COLUMN disponible BOOLEAN DEFAULT 1"))
                else:
                    db.session.execute(text("ALTER TABLE catalogo_item ADD COLUMN disponible BOOLEAN DEFAULT true"))
                db.session.commit()
                print("  ✅ Column 'disponible' added successfully.")
            except Exception as e:
                print(f"  ❌ Failed to add column: {e}")
                db.session.rollback()
        else:
            print("  ✅ Column 'disponible' already exists.")

    except Exception as e:
        print(f"  ❌ Critical error in schema fix: {e}")
        db.session.rollback()

def fix_permissions():
    print("🔧 Checking permissions for 'mauricio@junin.com'...")
    try:
        mauricio = User.query.filter_by(email="mauricio@junin.com").first()
        if not mauricio:
            print("  ⚠️ User not found.")
            return

        tenant = TenantProfile.query.filter_by(slug="municipio").first()
        if not tenant:
            print("  ⚠️ Tenant 'municipio' not found.")
            return

        if mauricio.tenant_id != tenant.id:
            print(f"  🔧 Tenant ID mismatch (User: {mauricio.tenant_id}, Tenant: {tenant.id}). Linking...")
            mauricio.tenant_id = tenant.id
            db.session.add(mauricio)
            db.session.commit()
            print("  ✅ User linked to tenant.")
        else:
            print("  ✅ User already correctly linked.")

    except Exception as e:
        print(f"  ❌ Critical error in permissions fix: {e}")
        db.session.rollback()

if __name__ == "__main__":
    with app.app_context():
        fix_schema()
        fix_permissions()
