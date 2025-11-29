"""Bootstrap helpers for the Junín municipality tenant.

This script creates (or updates) the Junín tenant, ensures the municipal
administrator user exists with a known password, and optionally copies the
default configuration JSON into the tenant profile. It is intended to make it
easy to recover a rollback by re-seeding the essentials with a single command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional

from app import create_app
from extensions import db
from models import Rubro, TenantProfile, User


DEFAULT_CONFIG_PATH = Path("data/municipios/default/config.json")
DEFAULT_EMAIL = "mauricio@junin.com"
DEFAULT_PASSWORD = "junin1234"
DEFAULT_TENANT_SLUG = "junin"
DEFAULT_TENANT_NAME = "Municipalidad de Junín"


def _load_config(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        print(f"⚠️ Config file not found at {path}; skipping tenant configuration sync.")
        return None

    with path.open("r", encoding="utf-8") as handler:
        return json.load(handler)


def _ensure_rubro() -> Rubro:
    rubro = Rubro.query.filter_by(clave="municipio").first()
    if rubro:
        return rubro

    rubro = Rubro(clave="municipio", nombre="Municipio", es_publico=True)
    db.session.add(rubro)
    db.session.flush()
    print("✅ Rubro 'municipio' creado.")
    return rubro


def _ensure_user(password: str, rubro: Rubro) -> User:
    user = User.query.filter_by(email=DEFAULT_EMAIL).first()
    created = False

    if not user:
        user = User(
            name="Mauricio",
            email=DEFAULT_EMAIL,
            rol="admin",
            tipo_chat="municipio",
            nombre_empresa=DEFAULT_TENANT_NAME,
            tenant_slug=DEFAULT_TENANT_SLUG,
            rubro_id=rubro.id,
        )
        created = True
    else:
        user.rol = "admin"
        user.tipo_chat = "municipio"
        user.nombre_empresa = user.nombre_empresa or DEFAULT_TENANT_NAME
        user.tenant_slug = user.tenant_slug or DEFAULT_TENANT_SLUG
        if not user.rubro_id:
            user.rubro_id = rubro.id

    user.set_password(password)
    db.session.add(user)
    db.session.flush()

    action = "creado" if created else "actualizado"
    print(f"✅ Usuario {DEFAULT_EMAIL} {action} con contraseña asegurada.")
    return user


def _ensure_tenant(user: User, config_data: Optional[Dict[str, Any]]) -> TenantProfile:
    tenant = TenantProfile.query.filter_by(slug=DEFAULT_TENANT_SLUG).first()
    created = False

    if not tenant:
        tenant = TenantProfile(
            slug=DEFAULT_TENANT_SLUG,
            nombre=DEFAULT_TENANT_NAME,
            tipo="municipio",
            municipio_id=user.id,
            dominio=f"{DEFAULT_TENANT_SLUG}.chatboc.ar",
            configuracion={},
        )
        created = True
    else:
        tenant.municipio_id = tenant.municipio_id or user.id
        tenant.nombre = tenant.nombre or DEFAULT_TENANT_NAME
        tenant.tipo = tenant.tipo or "municipio"
        tenant.dominio = tenant.dominio or f"{DEFAULT_TENANT_SLUG}.chatboc.ar"
        if tenant.configuracion is None:
            tenant.configuracion = {}

    cfg = tenant.configuracion or {}
    if config_data:
        cfg.setdefault("municipio_config", config_data)
    tenant.configuracion = cfg

    db.session.add(tenant)
    db.session.flush()

    action = "creado" if created else "actualizado"
    print(f"✅ Tenant '{DEFAULT_TENANT_SLUG}' {action}.")
    return tenant


def bootstrap(password: str, config_path: Path) -> None:
    config_data = _load_config(config_path)
    rubro = _ensure_rubro()
    user = _ensure_user(password, rubro)
    _ensure_tenant(user, config_data)
    db.session.commit()
    print("🎉 Base de datos de Junín lista.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap tenant for Junín")
    parser.add_argument(
        "--password",
        dest="password",
        default=DEFAULT_PASSWORD,
        help="Contraseña a asignar al usuario municipal (default: junin1234)",
    )
    parser.add_argument(
        "--config",
        dest="config",
        default=DEFAULT_CONFIG_PATH,
        type=Path,
        help=f"Ruta al JSON de configuración municipal (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        bootstrap(args.password, args.config)


if __name__ == "__main__":
    main()
