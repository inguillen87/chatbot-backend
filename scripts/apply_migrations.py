# scripts/apply_migrations.py
import os
from sqlalchemy import create_engine
from alembic.config import Config
from alembic import command

dburl = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ["DATABASE_URL"]

# Engine que SABEMOS que conecta
engine = create_engine(dburl, pool_pre_ping=True)

cfg = Config("alembic.ini")
cfg.set_main_option("script_location", "migrations")
cfg.set_main_option("sqlalchemy.url", dburl)

with engine.connect() as conn:
    # Inyectamos la conexión al entorno Alembic
    cfg.attributes["connection"] = conn
    print("Running migrations with existing connection...")
    command.upgrade(cfg, "head")
    print("Migrations applied.")
