#!/usr/bin/env python3
"""Seed/reset demo datasets by rubro slug.

Usage:
  python scripts/seed_demo_profiles.py seed --rubro municipio
  python scripts/seed_demo_profiles.py reset --rubro municipio
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.getcwd()))

from app import create_app
from extensions import db
from models import AnalyticsEvent, EncEncuesta, MarketOrder, TenantTicket, TenantProfile
from scripts.seed_demo_content import seed_content
from services.tenant_resolver import resolve_tenant_only


def _resolve_tenant(rubro: str) -> TenantProfile:
    tenant = resolve_tenant_only(tenant_slug=rubro, require_explicit_slug=True)
    if not tenant:
        raise RuntimeError(f"Tenant demo '{rubro}' no encontrado")
    return tenant


def reset_demo(rubro: str) -> None:
    tenant = _resolve_tenant(rubro)

    MarketOrder.query.filter_by(tenant_id=tenant.id).delete(synchronize_session=False)
    TenantTicket.query.filter_by(tenant_id=tenant.id).delete(synchronize_session=False)
    EncEncuesta.query.filter_by(tenant_id=tenant.id).delete(synchronize_session=False)
    AnalyticsEvent.query.filter_by(tenant_id=tenant.id).delete(synchronize_session=False)
    db.session.commit()
    print(f"✅ Demo reset completado para tenant={tenant.slug}")


def seed_demo(rubro: str) -> None:
    # Reuse existing content seeder (idempotent for many entities).
    seed_content()
    print(f"✅ Demo seed ejecutado (rubro solicitado={rubro})")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed/reset demo datasets by rubro")
    parser.add_argument("action", choices=["seed", "reset"], help="Acción a ejecutar")
    parser.add_argument("--rubro", required=True, help="Slug de rubro demo (municipio|bodega|ferreteria|...)" )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    app = create_app()
    with app.app_context():
        if args.action == "reset":
            reset_demo(args.rubro)
        else:
            seed_demo(args.rubro)


if __name__ == "__main__":
    main()
