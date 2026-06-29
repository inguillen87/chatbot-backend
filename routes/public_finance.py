from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from models import TenantProfile


FINANCE_WEBVIEW_CONTRACT_VERSION = "finance.webview.v1"

public_finance_bp = Blueprint("public_finance_bp", __name__)


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _json(payload: dict, status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _money(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if parsed <= 0:
        return None
    return f"{parsed.quantize(Decimal('0.01'))}"


def _tenant_payload(tenant_slug: str) -> dict:
    tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
    if tenant:
        return {
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "vertical": tenant.vertical or "finanzas",
            "logo_url": tenant.logo_url,
        }
    return {
        "id": None,
        "slug": tenant_slug,
        "nombre": tenant_slug.replace("-", " ").title(),
        "tipo": "finanzas",
        "vertical": "finanzas",
        "logo_url": None,
    }


def _flow_label(flow: str) -> tuple[str, str]:
    labels = {
        "alta": ("Alta digital", "Verifica identidad, documentos y consentimiento."),
        "operacion": ("Operacion segura", "Revisa una cobranza, pago, firma o credito asociado."),
        "cuentas": ("Estado de cuenta", "Consulta saldo operativo, vencimientos y soporte."),
        "transferencias": ("Transferencia o remesa", "Valida destinatario, monto y comprobante."),
        "seguros": ("Siniestro o cobertura", "Adjunta documentacion y segui el caso."),
        "financiacion": ("Financiacion o tasa", "Simula cuotas, impuestos o planes de pago."),
    }
    return labels.get(flow, ("Operacion financiera", "Continua una gestion financiera segura."))


def _finance_payload(tenant_slug: str, flow: str, operation_code: str) -> dict:
    session_token = (request.args.get("session") or request.args.get("token") or "").strip()
    amount = _money(request.args.get("amount") or request.args.get("monto"))
    currency = (request.args.get("currency") or request.args.get("moneda") or "ARS").strip().upper()[:8]
    contact_key = (request.args.get("contact_key") or request.args.get("contact") or "").strip()
    flow_title, flow_description = _flow_label(flow)
    session_ready = len(session_token) >= 8

    return {
        "contract_version": FINANCE_WEBVIEW_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_payload(tenant_slug),
        "operation": {
            "flow": flow,
            "code": operation_code,
            "title": flow_title,
            "description": flow_description,
            "status": "ready_for_customer_review" if session_ready else "session_required",
            "amount": amount,
            "currency": currency,
            "contact_key": contact_key or None,
        },
        "security_policy": {
            "card_data_in_chat_allowed": False,
            "identity_data_in_chat_allowed": False,
            "requires_session_token": True,
            "session_state": "present" if session_ready else "missing_or_short",
            "server_to_server_confirmation_required": True,
        },
        "steps": [
            {
                "id": "identity",
                "label": "Identidad y consentimiento",
                "state": "ready" if session_ready else "blocked",
                "detail": "Validacion segura antes de ejecutar cualquier accion.",
            },
            {
                "id": "review",
                "label": "Revision de la operacion",
                "state": "ready" if session_ready else "pending_session",
                "detail": "El usuario confirma monto, vencimiento, documentos o destino.",
            },
            {
                "id": "confirmation",
                "label": "Confirmacion trazable",
                "state": "pending",
                "detail": "Chatboc registra evento, ticket y notificacion para el equipo.",
            },
        ],
        "actions": {
            "primary": {
                "id": "continue_secure_flow",
                "label": "Continuar gestion segura",
                "enabled": session_ready,
                "disabled_reason": None if session_ready else "Falta token de sesion del link de WhatsApp.",
            },
            "support": {
                "id": "request_agent_help",
                "label": "Pedir ayuda de un asesor",
                "enabled": True,
            },
        },
        "analytics": {
            "funnel": "finance_transactional_whatsapp",
            "flow": flow,
            "source": request.args.get("source") or "whatsapp_webview",
        },
    }


@public_finance_bp.get("/api/public/finance/<tenant_slug>/<flow>/<operation_code>")
@public_finance_bp.get("/finanzas/<tenant_slug>/<flow>/<operation_code>")
def public_finance_webview_payload(tenant_slug: str, flow: str, operation_code: str):
    tenant_slug = (tenant_slug or "").strip().lower()
    flow = (flow or "").strip().lower()
    operation_code = (operation_code or "").strip()
    if not tenant_slug or not flow or not operation_code:
        return _json(
            {
                "contract_version": FINANCE_WEBVIEW_CONTRACT_VERSION,
                "status_code": 400,
                "reason_code": "finance_operation_required",
                "action_hint": "send_tenant_flow_and_operation_code",
                "error": {"message": "tenant, flow y operation_code son requeridos."},
            },
            400,
        )
    return _json(_finance_payload(tenant_slug, flow, operation_code))
