"""Bootstrap helpers for the Junín municipality tenant.

This script creates (or updates) the Junín tenant, ensures the municipal
administrator user exists without embedding credentials, and optionally copies the
default configuration JSON into the tenant profile. It is intended to make it
easy to recover a rollback by re-seeding the essentials with a single command.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from app import create_app
from extensions import db
from models import Rubro, TenantProfile, User
from services.user_service import assign_whatsapp_numbers


DEFAULT_CONFIG_PATH = Path("data/municipios/default/config.json")
DEFAULT_EMAIL = "mauricio@junin.com"
DEFAULT_TENANT_SLUG = "municipio"
DEFAULT_TENANT_NAME = "Municipio de Junín"
OFFICIAL_WHATSAPP = "+17432643718"


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


def _ensure_user(password: Optional[str], rubro: Rubro, tenant_slug: str) -> User:
    user = User.query.filter_by(email=DEFAULT_EMAIL).first()
    created = False

    if not user:
        if not password:
            raise ValueError("JUNIN_ADMIN_BOOTSTRAP_PASSWORD or --password is required for a new user")
        user = User(
            name="Mauricio",
            email=DEFAULT_EMAIL,
            rol="admin",
            tipo_chat="municipio",
            nombre_empresa=DEFAULT_TENANT_NAME,
            tenant_slug=tenant_slug,
            rubro_id=rubro.id,
        )
        created = True
    else:
        user.rol = "admin"
        user.tipo_chat = "municipio"
        user.nombre_empresa = DEFAULT_TENANT_NAME
        user.tenant_slug = tenant_slug
        if not user.rubro_id:
            user.rubro_id = rubro.id

    if password:
        user.set_password(password)
    db.session.add(user)
    db.session.flush()

    action = "creado" if created else "actualizado"
    print(f"✅ Usuario {DEFAULT_EMAIL} {action}.")
    return user


def _ensure_tenant(
    user: User,
    config_data: Optional[Dict[str, Any]],
    *,
    tenant_slug: str,
    widget_token: str,
    whatsapp_number: str,
) -> TenantProfile:
    tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
    created = False

    if not tenant:
        tenant = TenantProfile(
            slug=tenant_slug,
            nombre=DEFAULT_TENANT_NAME,
            tipo="municipio",
            municipio_id=user.id,
            dominio=f"{tenant_slug}.chatboc.ar",
            configuracion={},
        )
        created = True
    else:
        tenant.municipio_id = user.id
        tenant.pyme_id = None
        tenant.nombre = DEFAULT_TENANT_NAME
        tenant.tipo = "municipio"
        tenant.dominio = tenant.dominio or f"{tenant_slug}.chatboc.ar"
        tenant.is_active = True
        if tenant.configuracion is None:
            tenant.configuracion = {}

    cfg = tenant.configuracion or {}
    if config_data:
        cfg.setdefault("municipio_config", config_data)

    cfg.setdefault("assistant_name", "JUNI")
    cfg.setdefault("nombre_municipio", DEFAULT_TENANT_NAME)
    cfg.setdefault("nombre", DEFAULT_TENANT_NAME)

    tokens = cfg.get("widget_tokens") or []
    if isinstance(tokens, str):
        tokens = [tokens]
    if widget_token and widget_token not in tokens:
        tokens.append(widget_token)
    cfg["widget_tokens"] = tokens

    whatsapp_numbers = cfg.get("whatsapp_numbers") or []
    if isinstance(whatsapp_numbers, str):
        whatsapp_numbers = [whatsapp_numbers]
    if whatsapp_number and whatsapp_number not in whatsapp_numbers:
        whatsapp_numbers.append(whatsapp_number)
    cfg["whatsapp_numbers"] = whatsapp_numbers
    cfg["whatsapp_oficial"] = whatsapp_number

    tenant.configuracion = cfg

    db.session.add(tenant)
    db.session.flush()

    action = "creado" if created else "actualizado"
    print(f"✅ Tenant '{DEFAULT_TENANT_SLUG}' {action}.")
    return tenant


def _remove_widget_token_from_others(widget_token: str, keep_slug: str) -> None:
    if not widget_token:
        return

    conflicts = (
        TenantProfile.query.filter(
            TenantProfile.slug != keep_slug,
            TenantProfile.configuracion["widget_tokens"].astext.contains(widget_token),
        )
        .order_by(TenantProfile.id.asc())
        .all()
    )

    for tenant in conflicts:
        cfg = tenant.configuracion or {}
        tokens = cfg.get("widget_tokens")
        cleaned: list[str] = []

        if isinstance(tokens, str):
            cleaned = [t for t in [tokens] if t != widget_token]
        elif isinstance(tokens, list):
            cleaned = [t for t in tokens if t != widget_token]

        cfg["widget_tokens"] = cleaned
        tenant.configuracion = cfg
        db.session.add(tenant)
        print(f"🔁 Removido widget_token {widget_token} del tenant {tenant.slug}.")


def bootstrap(password: Optional[str], config_path: Path, *, tenant_slug: str, widget_token: str) -> None:
    config_data = _load_config(config_path)
    rubro = _ensure_rubro()
    user = _ensure_user(password, rubro, tenant_slug)
    tenant = _ensure_tenant(
        user,
        config_data,
        tenant_slug=tenant_slug,
        widget_token=widget_token,
        whatsapp_number=OFFICIAL_WHATSAPP,
    )
    user.tenant_id = tenant.id
    user.tenant_slug = tenant.slug
    user.municipio_id = user.id
    assign_whatsapp_numbers(user, [OFFICIAL_WHATSAPP], activate=True, commit=False)
    _remove_widget_token_from_others(widget_token, tenant_slug)
    db.session.commit()
    print("🎉 Base de datos de Junín lista.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap tenant for Junín")
    parser.add_argument(
        "--password",
        dest="password",
        default=os.getenv("JUNIN_ADMIN_BOOTSTRAP_PASSWORD"),
        help="Nueva contraseña; también puede definirse con JUNIN_ADMIN_BOOTSTRAP_PASSWORD",
    )
    parser.add_argument(
        "--config",
        dest="config",
        default=DEFAULT_CONFIG_PATH,
        type=Path,
        help=f"Ruta al JSON de configuración municipal (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--slug",
        dest="slug",
        default=DEFAULT_TENANT_SLUG,
        help=f"Slug del tenant municipal (default: {DEFAULT_TENANT_SLUG})",
    )
    parser.add_argument(
        "--widget-token",
        dest="widget_token",
        default=os.getenv("DEMO_WIDGET_TOKEN_JUNIN", ""),
        help="Widget token a registrar para el tenant municipal",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        bootstrap(
            args.password,
            args.config,
            tenant_slug=args.slug,
            widget_token=args.widget_token,
        )


if __name__ == "__main__":
    main()
