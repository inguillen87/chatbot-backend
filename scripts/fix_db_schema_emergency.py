
import sys
import os
from sqlalchemy import text, inspect

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, app as existing_app
from database import db

def fix_schema():
    print("🚑 Starting Emergency DB Schema Fix (Database Agnostic)...")

    # Use existing app or create a new one
    app_instance = existing_app or create_app()

    with app_instance.app_context():
        inspector = inspect(db.engine)

        # 1. Check pyme_ticket.categoria_id
        table_name = 'pyme_ticket'
        if inspector.has_table(table_name):
            columns = [col['name'] for col in inspector.get_columns(table_name)]
            if 'categoria_id' not in columns:
                print(f"⚠️ 'categoria_id' missing in '{table_name}'. Adding it...")
                with db.engine.connect() as conn:
                    # SQLite syntax is slightly different for ALTER TABLE (limited), but adding a column is standard.
                    # REFERENCES might be ignored by SQLite if not enabled, but syntax is usually valid.
                    sql = "ALTER TABLE pyme_ticket ADD COLUMN categoria_id INTEGER REFERENCES categorias_ticket(id)"
                    conn.execute(text(sql))
                    conn.commit()
                print(f"✅ Added 'categoria_id' to '{table_name}'.")
            else:
                print(f"✅ 'categoria_id' already exists in '{table_name}'.")
        else:
            print(f"⚠️ Table '{table_name}' does not exist.")

        # 2. Check municipio_ticket.categoria_id
        table_name = 'municipio_ticket'
        if inspector.has_table(table_name):
            columns = [col['name'] for col in inspector.get_columns(table_name)]
            if 'categoria_id' not in columns:
                print(f"⚠️ 'categoria_id' missing in '{table_name}'. Adding it...")
                with db.engine.connect() as conn:
                    sql = "ALTER TABLE municipio_ticket ADD COLUMN categoria_id INTEGER REFERENCES categorias_ticket(id)"
                    conn.execute(text(sql))
                    conn.commit()
                print(f"✅ Added 'categoria_id' to '{table_name}'.")
            else:
                print(f"✅ 'categoria_id' already exists in '{table_name}'.")

        # 3. Check catalogo_item.disponible
        table_name = 'catalogo_item'
        if inspector.has_table(table_name):
            columns = [col['name'] for col in inspector.get_columns(table_name)]
            if 'disponible' not in columns:
                print(f"⚠️ 'disponible' missing in '{table_name}'. Adding it...")
                with db.engine.connect() as conn:
                    # SQLite doesn't support adding a column with a default value that isn't constant in older versions,
                    # but TRUE/1 works.
                    sql = "ALTER TABLE catalogo_item ADD COLUMN disponible BOOLEAN DEFAULT 1"
                    conn.execute(text(sql))
                    conn.commit()
                print(f"✅ Added 'disponible' to '{table_name}'.")
            else:
                print(f"✅ 'disponible' already exists in '{table_name}'.")

        print("🏁 DB Schema Fix Completed.")

if __name__ == "__main__":
    fix_schema()
