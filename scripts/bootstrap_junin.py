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

    _georeference_junin_tickets(tenant.id, user.id)

    db.session.commit()
    print("🎉 Base de datos de Junín lista con georreferenciación completa.")


def _georeference_junin_tickets(tenant_id: int, user_id: int) -> None:
    import random
    from datetime import datetime, timedelta, timezone
    from models import AnalyticsEventV2, MunicipioTicket

    calles_conocidas = [
        ("mitre", "Av. Mitre 450", "Centro", -33.1412, -68.4839),
        ("san martin", "Av. San Martín 820", "Centro", -33.1425, -68.4851),
        ("san martín", "Av. San Martín 820", "Centro", -33.1425, -68.4851),
        ("salvador gonzalez", "Calle Salvador González 120", "Barrio Norte", -33.1385, -68.4820),
        ("salvador gonzález", "Calle Salvador González 120", "Barrio Norte", -33.1385, -68.4820),
        ("primavera", "Calle Primavera y Ladislao Segura", "Barrio Este", -33.1440, -68.4795),
        ("barriales", "Ruta 60 Km 12", "Los Barriales", -33.1250, -68.5120),
        ("corvalan", "Av. Corvalán 300", "La Colonia", -33.1180, -68.4750),
        ("corvalán", "Av. Corvalán 300", "La Colonia", -33.1180, -68.4750),
        ("bousquet", "Calle Isidoro Bousquet 700", "Philipps", -33.1650, -68.4100),
        ("philipps", "Calle Isidoro Bousquet 700", "Philipps", -33.1650, -68.4100),
        ("necochea", "Calle Necochea y 25 de Mayo", "Centro", -33.1408, -68.4845),
        ("25 de mayo", "Calle Necochea y 25 de Mayo", "Centro", -33.1408, -68.4845),
        ("ferroviario", "Barrio Jardín Ferroviario M-B C-12", "La Colonia", -33.1195, -68.4735),
        ("la colonia", "Av. Corvalán y Neuquén", "La Colonia", -33.1185, -68.4740),
        ("don bosco", "Calle Don Bosco 550", "Medrano", -33.1780, -68.5950),
        ("medrano", "Calle Don Bosco 550", "Medrano", -33.1780, -68.5950),
        ("segura", "Calle Ladislao Segura 310", "Centro", -33.1430, -68.4810),
    ]

    tickets = MunicipioTicket.query.filter(
        (MunicipioTicket.municipio_id == tenant_id)
        | (MunicipioTicket.municipio_id == user_id)
        | (MunicipioTicket.tenant_id == tenant_id)
    ).all()

    now = datetime.now(timezone.utc)
    for i, t in enumerate(tickets):
        if t.tenant_id is None:
            t.tenant_id = tenant_id

        # 1. Intentar detectar la dirección real desde detalles, pregunta o direccion
        texto_completo = f"{t.direccion or ''} {t.detalles or ''} {t.pregunta or ''} {t.asunto or ''}".lower()
        
        direccion_res = None
        distrito_res = None
        lat_base_res = None
        lng_base_res = None

        for keyword, dir_nom, dist_nom, lat_val, lng_val in calles_conocidas:
            if keyword in texto_completo:
                direccion_res = dir_nom
                distrito_res = dist_nom
                lat_base_res = lat_val
                lng_base_res = lng_val
                break

        if not lat_base_res:
            _, dir_nom, dist_nom, lat_base_res, lng_base_res = calles_conocidas[i % len(calles_conocidas)]
            direccion_res = dir_nom
            distrito_res = dist_nom

        lat_jitter = lat_base_res + random.uniform(-0.0015, 0.0015)
        lng_jitter = lng_base_res + random.uniform(-0.0015, 0.0015)

        if not t.latitud or not t.longitud:
            t.latitud = lat_jitter
            t.longitud = lng_jitter
            if not t.direccion:
                t.direccion = direccion_res
            if not t.distrito:
                t.distrito = distrito_res

        existing_event = AnalyticsEventV2.query.filter_by(
            tenant_id=tenant_id,
            entity_id=t.id,
            entity_type="municipio_ticket"
        ).first()

        if not existing_event:
            event_ts = t.fecha if t.fecha else (now - timedelta(days=random.randint(1, 30)))
            if event_ts.tzinfo is None:
                event_ts = event_ts.replace(tzinfo=timezone.utc)

            event = AnalyticsEventV2(
                tenant_id=tenant_id,
                channel=t.canal_ingreso or "whatsapp",
                event_name="ticket_created",
                category=t.categoria or "Luminarias",
                lat=t.latitud,
                lng=t.longitud,
                ts=event_ts,
                entity_type="municipio_ticket",
                entity_id=t.id,
                metadata_payload={
                    "ticket_id": t.id,
                    "nro_ticket": t.nro_ticket,
                    "categoria": t.categoria or "Luminarias",
                    "estado": t.estado or "nuevo",
                    "distrito": t.distrito or distrito,
                    "direccion": t.direccion or direccion,
                }
            )
            db.session.add(event)


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
