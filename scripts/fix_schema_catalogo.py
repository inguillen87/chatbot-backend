
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text, create_engine, inspect

def fix_catalogo_schema():
    print("🔧 [Standalone] Checking catalogo_item schema...")

    # We create a minimal engine to avoid loading the whole Flask App
    # Reading config from environment or .env
    from dotenv import load_dotenv
    load_dotenv()

    db_uri = os.getenv("SQLALCHEMY_DATABASE_URI") or "sqlite:///instance/database.db"
    if not db_uri:
        print("❌ SQLALCHEMY_DATABASE_URI not set.")
        return

    print(f"   Connecting to {db_uri.split('://')[0]}://...")

    try:
        engine = create_engine(db_uri)
        inspector = inspect(engine)

        # Check if table exists
        if not inspector.has_table("catalogo_item"):
            print("❌ Table 'catalogo_item' does not exist. Run migrations first.")
            return

        # Check columns
        columns = [c['name'] for c in inspector.get_columns("catalogo_item")]
        if "disponible" in columns:
            print("✅ 'disponible' column already exists.")
        else:
            print("⚠️ 'disponible' column MISSING. Adding it now...")
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE catalogo_item ADD COLUMN disponible BOOLEAN DEFAULT true"))
                conn.commit()
            print("✅ Column added successfully.")

    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    fix_catalogo_schema()
