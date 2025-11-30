# scripts/apply_migrations.py
import os

from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import create_engine

dburl = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ.get("DATABASE_URL")

if not dburl:
    raise RuntimeError("Definí SQLALCHEMY_DATABASE_URI o DATABASE_URL antes de correr migraciones.")

# Alinear explícitamente la URL de Alembic con la misma que usa SQLAlchemy
os.environ["ALEMBIC_DB_URL"] = dburl

# Engine que SABEMOS que conecta
engine = create_engine(dburl, pool_pre_ping=True)

cfg = Config("alembic.ini")
cfg.set_main_option("script_location", "migrations")
cfg.set_main_option("sqlalchemy.url", dburl)

with engine.connect() as conn:
    # Inyectamos la conexión al entorno Alembic
    cfg.attributes["connection"] = conn
    print("Running migrations with existing connection...")
    try:
        command.upgrade(cfg, "head")
    except CommandError as exc:
        message = str(exc)
        if "Multiple heads" in message:
            print("Multiple heads detected, upgrading all heads instead...")
            command.upgrade(cfg, "heads")
        else:
            raise
    print("Migrations applied.")
