# scripts/upgrade_db.py
import os
from alembic.config import Config
from alembic import command

# Prioridad: SQLALCHEMY_DATABASE_URI (ya lo tenés armado con +psycopg y sslmode=require)
dburl = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ["DATABASE_URL"]

cfg = Config("alembic.ini")
cfg.set_main_option("script_location", "migrations")
cfg.set_main_option("sqlalchemy.url", dburl)

print("Upgrading DB with:", cfg.get_main_option("sqlalchemy.url").replace(os.getenv("DB_PASSWORD",""), "***") if os.getenv("DB_PASSWORD") else cfg.get_main_option("sqlalchemy.url"))
command.upgrade(cfg, "head")
print("DB upgraded to head.")
