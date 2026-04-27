from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from models import TenantProfile
from routes.auth import demo_catalog as legacy_demo_catalog
from services.demo_experience_contract import build_demo_experience_contract
from services.demo_registry import load_demo_rubros
from routes.v2.tenants import create_demo_session_token

v2_demo_bp = Blueprint("v2_demo", __name__, url_prefix="/api/v2/demo")


def _tenant_dict(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
    }


def _safe_demo_rubros() -> list[dict[str, Any]]:
    items = []
    for rubro in load_demo_rubros(require_owner=False):
        items.append(
            {
                "key": rubro.key,
                "label": rubro.label,
                "tipo_chat": rubro.tipo_chat,
                "tenant_slug": getattr(rubro, "tenant_slug", None) or rubro.key,
            }
        )
    return items


@v2_demo_bp.route('/catalog', methods=['GET'])
def demo_catalog_v2():
    # Reuse legacy catalog generation, then sanitize/reshape into v2 contract.
    legacy_response = legacy_demo_catalog()
    legacy_payload = legacy_response.get_json(silent=True) if hasattr(legacy_response, "get_json") else {}

    rubros = []
    for item in (legacy_payload or {}).get("tenant_demos") or []:
        rubros.append(
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "tipo_chat": item.get("tipo_chat"),
                "tenant_slug": item.get("tenant_slug"),
            }
        )
    if not rubros:
        rubros = _safe_demo_rubros()
    gobierno = [r for r in rubros if (r.get("tipo_chat") or "").lower() == "municipio"]
    empresas = [r for r in rubros if (r.get("tipo_chat") or "").lower() == "pyme"]

    payload = {
        "sectors": [
            {
                "key": "gobierno",
                "label": "Gobiernos y municipios",
                "rubros": gobierno,
            },
            {
                "key": "empresas",
                "label": "Empresas y pymes",
                "rubros": empresas,
            },
        ]
    }
    return jsonify(payload)


@v2_demo_bp.route('/session', methods=['POST'])
def demo_session_v2():
    data = request.get_json(silent=True) or {}
    sector = str(data.get("sector") or "").strip().lower()
    rubro = str(data.get("rubro") or "").strip().lower()
    tenant_slug = str(data.get("tenant_slug") or "").strip().lower()

    if sector not in {"gobierno", "empresas"}:
        return jsonify({"error": "sector debe ser 'gobierno' o 'empresas'"}), 400

    if not rubro and not tenant_slug:
        return jsonify({"error": "rubro o tenant_slug es obligatorio"}), 400

    tenant = None
    if tenant_slug:
        tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == tenant_slug).first()
        if not tenant:
            return jsonify({"error": "Tenant no encontrado"}), 404
    else:
        for entry in _safe_demo_rubros():
            entry_key = (entry.get("key") or "").strip().lower()
            if entry_key == rubro:
                slug = (entry.get("tenant_slug") or "").strip().lower()
                if slug:
                    tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug).first()
                    if tenant:
                        break
        if not tenant:
            return jsonify({"error": "No se pudo resolver tenant demo"}), 404

    tenant_type = (tenant.tipo or "pyme").strip().lower()
    experience = build_demo_experience_contract(
        tenant_type=tenant_type,
        rubro_label=tenant.nombre,
    )
    onboarding = experience.get("guided_onboarding") or {}
    quick_replies = onboarding.get("starter_prompts") or []

    demo_session_id = create_demo_session_token(
        tenant_slug=tenant.slug,
        sector=sector,
        rubro=rubro or tenant.slug,
    )

    return jsonify(
        {
            "demo_session_id": demo_session_id,
            "tenant": _tenant_dict(tenant),
            "chat_seed": {
                "entry_prompt": onboarding.get("entry_prompt") or "¿Sobre qué te gustaría preguntar primero?",
                "autostart_chat": bool(onboarding.get("autostart_chat", True)),
                "open_widget": bool(onboarding.get("open_widget", True)),
            },
            "quick_replies": quick_replies,
        }
    )
