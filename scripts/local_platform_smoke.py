"""Run a local, isolated platform smoke test.

This script uses Flask's test client and an in-memory SQLite database, so it
does not touch production, Render, Twilio, WhatsApp, Qdrant, or the local .env DB.
It is intended as a fast contract check before deploying frontend/backend syncs.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("ENABLE_DEMO_MODE", "true")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402
from config import Config  # noqa: E402
from models import CatalogoItem, TenantProfile, TenantTicket, User  # noqa: E402


class LocalSmokeConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SECRET_KEY = "local-smoke-secret-only-for-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    WTF_CSRF_ENABLED = False


def _auth_headers(app, user: User, tenant_slug: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "tenant_slug": tenant_slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant_slug}


def _seed_data() -> User:
    owner = User(name="Smoke Admin", email="smoke-admin@test.local", rol="admin", tenant_slug="colegio-demo")
    owner.set_password("secret123")
    db.session.add(owner)
    db.session.flush()

    tenants = [
        TenantProfile(
            slug="colegio-demo",
            nombre="Colegio Demo",
            tipo="pyme",
            vertical="educacion",
            subvertical="colegio",
            pyme_id=owner.id,
            configuracion={"widget_tokens": ["widget-colegio-demo"]},
        ),
        TenantProfile(
            slug="municipio",
            nombre="Municipio Inteligente",
            tipo="municipio",
            vertical="gobierno",
            subvertical="municipio",
            municipio_id=owner.id,
            configuracion={"widget_tokens": ["widget-municipio"]},
        ),
        TenantProfile(
            slug="bodega",
            nombre="Bodega Demo",
            tipo="pyme",
            vertical="pyme",
            subvertical="bebidas",
            pyme_id=owner.id,
            configuracion={"widget_tokens": ["widget-bodega"]},
        ),
    ]
    db.session.add_all(tenants)
    db.session.flush()

    owner.tenant_id = tenants[0].id
    db.session.add(owner)
    db.session.add(
        TenantTicket(
            tenant_id=tenants[0].id,
            user_id=owner.id,
            categoria="educacion",
            descripcion="Consulta por beca",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={
                "title": "Consulta por beca",
                "priority": "high",
                "contact": {"name": "Familia Demo", "phone": "+5491111111111"},
                "contact_key": "whatsapp:+5491111111111",
                "intent": "consulta_beca",
                "conversation_id": "conv-smoke-1",
                "demo_session_id": "demo-smoke-1",
                "attachments": [
                    {
                        "id": "att-smoke-1",
                        "name": "certificado.jpg",
                        "url": "https://example.com/certificado.jpg",
                        "mimeType": "image/jpeg",
                    }
                ],
            },
            latitud=-34.6,
            longitud=-58.4,
        )
    )
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenants[2].id,
            nombre="Vino demo",
            descripcion="Producto demo para smoke",
            precio="1000",
            cantidad="12",
            imagen_url="https://example.com/vino.jpg",
        )
    )
    db.session.commit()
    return owner


def _json(resp) -> dict[str, Any]:
    try:
        return resp.get_json() or {}
    except Exception:
        return {"raw": resp.get_data(as_text=True)[:300]}


def _check(name: str, ok: bool, status: int, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "status": status, "details": details or {}}


def run() -> int:
    app = create_app(LocalSmokeConfig)
    results: list[dict[str, Any]] = []

    with app.app_context():
        db.create_all()
        owner = _seed_data()
        client = app.test_client()

        resp = client.get("/api/v2/health")
        results.append(_check("v2_health", resp.status_code == 200 and _json(resp).get("ok") is True, resp.status_code))

        resp = client.get(
            "/api/public/widget-config",
            headers={"Host": "chatbot-backend-2e14.onrender.com", "X-Forwarded-Host": "www.chatboc.ar"},
        )
        body = _json(resp)
        results.append(
            _check(
                "widget_platform_onboarding_proxy",
                resp.status_code == 200
                and (body.get("tenant") or {}).get("slug") == "chatboc-platform"
                and (body.get("onboarding") or {}).get("mode") == "platform_sector_selector"
                and not bool((body.get("realtime") or {}).get("socket_enabled")),
                resp.status_code,
                {
                    "tenant": body.get("tenant"),
                    "onboarding_mode": (body.get("onboarding") or {}).get("mode"),
                    "quick_menu_count": len(body.get("quick_menu") or []),
                },
            )
        )

        for sector, tenant_slug, rubro, endpoint in [
            ("educacion", "colegio-demo", "colegio-demo", "/ask/pyme"),
            ("gobierno", "municipio", "municipio", "/ask/municipio"),
            ("empresas", "bodega", "bodega", "/ask/pyme"),
        ]:
            resp = client.post(
                "/api/v2/demo/session",
                json={"sector": sector, "tenant_slug": tenant_slug, "rubro": rubro},
                headers={"X-Request-Id": f"local-smoke-{sector}"},
            )
            body = _json(resp)
            bootstrap = (body.get("workspace") or {}).get("chat_bootstrap") or body.get("chat_bootstrap") or {}
            results.append(
                _check(
                    f"demo_session_{sector}",
                    resp.status_code == 200
                    and body.get("contract_version") == "demo.session.v2"
                    and bootstrap.get("endpoint") == endpoint
                    and (bootstrap.get("headers") or {}).get("X-Tenant-Slug") == tenant_slug,
                    resp.status_code,
                    {
                        "tenant_slug": body.get("tenant_slug"),
                        "endpoint": bootstrap.get("endpoint"),
                        "supports": bootstrap.get("supports"),
                    },
                )
            )

        resp = client.get("/api/public/realtime/voice-capabilities")
        body = _json(resp)
        results.append(
            _check(
                "realtime_voice_capabilities",
                resp.status_code == 200 and body.get("contract_version") == "realtime.voice_capabilities.v1",
                resp.status_code,
                {"enabled": body.get("enabled"), "model": body.get("recommended_model")},
            )
        )

        headers = _auth_headers(app, owner, "colegio-demo")
        for name, path, expected_contract in [
            ("tenant_admin_experience", "/api/v2/tenant/admin-experience", "tenant.admin_experience.v1"),
            ("whatsapp_experience", "/api/v2/whatsapp/experience", "whatsapp.experience.v1"),
            ("catalog_quality", "/api/v2/catalog/quality", "catalog.quality.v1"),
            ("inbox_omnichannel", "/api/v2/inbox/omnichannel", "inbox.omnichannel.v1"),
            ("production_smoke", "/api/v2/platform/production-smoke", "platform.production_smoke.v1"),
        ]:
            resp = client.get(path, headers=headers)
            body = _json(resp)
            results.append(
                _check(
                    name,
                    resp.status_code == 200 and body.get("contract_version") == expected_contract,
                    resp.status_code,
                    {"contract_version": body.get("contract_version"), "status": body.get("status")},
                )
            )

        db.session.remove()
        db.drop_all()

    failed = [item for item in results if not item["ok"]]
    print(json.dumps({"ok": not failed, "total": len(results), "failed": len(failed), "results": results}, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
