"""Seed demo survey responses to showcase analytics and heatmaps.

This helper mirrors the existing `/api/encuestas/<id>/seed-demo` endpoint but
allows operators to seed multiple surveys in bulk from the command line. It is
useful for sales demos where we need geolocated responses to render the
participation map (MapLibre/Google) without relying on real traffic.
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable, Optional

from app import create_app
from extensions import db
from models import EncEncuesta, User
from services.encuestas_service import seed_encuesta_respuestas_demo


def _find_admin(tenant_id: int) -> Optional[User]:
    """Return an admin user for the given tenant so seed calls pass validation."""

    return (
        User.query.filter_by(municipio_id=tenant_id, rol="admin").first()
        or User.query.filter_by(municipio_id=tenant_id).first()
        or User.query.filter_by(role="admin").first()
    )


def _iter_target_encuestas(tenant_id: int, encuesta_id: Optional[int]) -> Iterable[EncEncuesta]:
    query = EncEncuesta.query.filter_by(tenant_id=tenant_id)
    if encuesta_id:
        query = query.filter_by(id=encuesta_id)
    else:
        query = query.filter(EncEncuesta.estado == "publicada")
    return query.order_by(EncEncuesta.id.asc()).all()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed demo responses for encuestas")
    parser.add_argument("tenant", type=int, help="ID del tenant (municipio)")
    parser.add_argument("--encuesta-id", type=int, help="ID de una encuesta específica")
    parser.add_argument("--cantidad", type=int, default=100, help="Cantidad de respuestas a generar")
    parser.add_argument("--seed", type=int, help="Seed opcional para resultados deterministas")
    parser.add_argument("--geo-profile-key", dest="geo_profile_key", help="Perfil geo a usar")
    parser.add_argument(
        "--municipality-label",
        dest="municipality_label",
        help="Etiqueta de municipio para el generador geo",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = create_app()
    with app.app_context():
        admin = _find_admin(args.tenant)
        if not admin:
            print(f"❌ No se encontró un admin para el tenant {args.tenant}")
            return 1

        encuestas = list(_iter_target_encuestas(args.tenant, args.encuesta_id))
        if not encuestas:
            print("⚠️ No hay encuestas publicadas para seedear en este tenant")
            return 0

        for encuesta in encuestas:
            result = seed_encuesta_respuestas_demo(
                encuesta.id,
                admin,
                cantidad=args.cantidad,
                geo_profile_key=args.geo_profile_key,
                municipality_label=args.municipality_label,
                seed=args.seed,
            )
            db.session.commit()
            print(
                f"✅ Seed encuesta {encuesta.id} ({encuesta.titulo}): "
                f"{result['creadas']} creadas, {result['omitidas']} omitidas"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
