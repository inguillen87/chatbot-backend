
import os
import sys
import logging
from sqlalchemy import text

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import Config to patch it BEFORE app import/creation
from config import Config
Config.SESSION_TYPE = 'filesystem'

from app import create_app, db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def fix_catalogo_item_schema():
    app = create_app()
    with app.app_context():
        logger.info("Checking catalogo_item schema...")

        # Check if column exists
        # Note: This query is generic enough for Postgres/SQLite inspection via SQLAlchemy inspector,
        # but raw SQL is trickier across dialects.
        # Let's use SQLAlchemy Inspector for dialect independence.
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('catalogo_item')]

        if 'fecha_vigencia' not in columns:
            logger.info("Column 'fecha_vigencia' missing. Adding it...")
            try:
                # Add the column
                # SQLite supports ADD COLUMN. Postgres does too.
                alter_query = text("ALTER TABLE catalogo_item ADD COLUMN fecha_vigencia DATE;")
                db.session.execute(alter_query)
                db.session.commit()
                logger.info("Successfully added 'fecha_vigencia' to 'catalogo_item'.")
            except Exception as e:
                db.session.rollback()
                logger.error(f"Error adding column: {e}")
        else:
            logger.info("Column 'fecha_vigencia' already exists.")

if __name__ == "__main__":
    fix_catalogo_item_schema()
