from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from models import TenantProfile


FINANCE_WEBVIEW_CONTRACT_VERSION = "finance.webview.v1"

public_finance_bp = Blueprint("public_finance_bp", __name__)

_FLOW_DEFINITIONS: dict[str, dict] = {
    "alta": {
        "title": "Alta digital",
        "description": "Completa consentimiento, identidad y documentos desde una pantalla segura.",
        "webview_flow_id": "finance_onboarding_kyc",
        "primary_label": "Iniciar alta segura",
        "support_label": "Pedir asistencia de alta",
        "crm_queue": {"id": "identity_review", "label": "KYC y validacion manual", "sla_minutes": 240},
        "templates": ["finance_account_onboarding", "finance_kyc_review"],
        "success_events": ["identity_verified", "onboarding_submitted", "crm_lead_updated"],
        "analytics_events": ["finance_onboarding_started", "kyc_submitted", "manual_review_required"],
        "user_tasks": [
            "Aceptar consentimiento informado",
            "Adjuntar documentos desde canal seguro",
            "Esperar validacion o revision manual",
        ],
        "steps": [
            ("consent", "Consentimiento", "Revisas condiciones, privacidad y autorizacion de uso de datos."),
            ("identity", "Identidad", "La validacion documental queda fuera de WhatsApp."),
            ("review", "Revision", "El equipo ve el resultado y continua el caso desde el CRM."),
        ],
    },
    "operacion": {
        "title": "Operacion segura",
        "description": "Revisa una cobranza, pago, firma o credito asociado antes de confirmar.",
        "webview_flow_id": "finance_credit_collection_signature",
        "primary_label": "Revisar y continuar",
        "support_label": "Consultar con un asesor",
        "crm_queue": {"id": "collections", "label": "Cobranzas y planes de pago", "sla_minutes": 120},
        "templates": ["finance_credit_offer", "finance_collection_due", "finance_secure_payment", "finance_document_signature"],
        "success_events": ["payment_webhook", "signature_completed", "crm_operation_updated"],
        "analytics_events": ["collection_opened", "payment_started", "signature_completed"],
        "user_tasks": [
            "Revisar monto, concepto y vencimiento",
            "Elegir pagar, pedir plan o firmar documento",
            "Recibir comprobante y seguimiento trazable",
        ],
        "steps": [
            ("operation_summary", "Resumen", "Validas monto, concepto, cuotas o condiciones."),
            ("payment_or_plan", "Pago o plan", "El checkout y el plan de pago se ejecutan con confirmacion servidor a servidor."),
            ("signature", "Firma", "Si hay documento, se firma con proveedor seguro y auditoria."),
        ],
    },
    "cuentas": {
        "title": "Estado de cuenta",
        "description": "Consulta resumen, vencimientos y soporte sin exponer saldos sensibles en el chat.",
        "webview_flow_id": "finance_account_servicing",
        "primary_label": "Abrir cuenta segura",
        "support_label": "Abrir soporte de cuenta",
        "crm_queue": {"id": "support", "label": "Soporte financiero", "sla_minutes": 240},
        "templates": ["finance_account_status", "finance_support_case"],
        "success_events": ["statement_opened", "support_case_linked", "crm_contact_updated"],
        "analytics_events": ["statement_opened", "support_case_created", "operator_handoff"],
        "user_tasks": [
            "Validar acceso con token o enlace firmado",
            "Ver resumen y vencimientos permitidos",
            "Abrir un caso de soporte si detectas un problema",
        ],
        "steps": [
            ("account_gate", "Acceso seguro", "Se verifica que el link pertenezca al contacto correcto."),
            ("statement", "Resumen", "El detalle sensible queda en la vista protegida."),
            ("support", "Soporte", "Cualquier duda queda asociada al historial operativo."),
        ],
    },
    "transferencias": {
        "title": "Transferencia o remesa",
        "description": "Valida destinatario, monto, comisiones y comprobante desde un flujo trazable.",
        "webview_flow_id": "finance_remittance_transfer",
        "primary_label": "Ver transferencia",
        "support_label": "Pedir ayuda con transferencia",
        "crm_queue": {"id": "payments", "label": "Pagos y comprobantes", "sla_minutes": 60},
        "templates": ["finance_remittance_transfer", "finance_secure_payment"],
        "success_events": ["transfer_validated", "transfer_receipt_ready", "crm_operation_updated"],
        "analytics_events": ["transfer_started", "transfer_validated", "transfer_receipt_ready"],
        "user_tasks": [
            "Confirmar datos visibles del destinatario",
            "Validar monto y comisiones",
            "Descargar comprobante cuando este listo",
        ],
        "steps": [
            ("transfer_summary", "Transferencia", "Revisas importe, referencia y estado operativo."),
            ("validation", "Validacion", "El sistema registra riesgo, consentimiento e idempotencia."),
            ("receipt", "Comprobante", "El recibo se libera cuando el backend confirma la operacion."),
        ],
    },
    "seguros": {
        "title": "Siniestro o cobertura",
        "description": "Carga documentacion, fotos o comprobantes para seguimiento del caso.",
        "webview_flow_id": "finance_insurance_claim",
        "primary_label": "Continuar siniestro",
        "support_label": "Hablar con siniestros",
        "crm_queue": {"id": "support", "label": "Soporte financiero", "sla_minutes": 240},
        "templates": ["finance_insurance_claim", "finance_document_signature", "finance_support_case"],
        "success_events": ["insurance_claim_created", "attachments_uploaded", "crm_case_updated"],
        "analytics_events": ["insurance_claim_started", "attachment_uploaded", "manual_review_assigned"],
        "user_tasks": [
            "Indicar tipo de siniestro o cobertura",
            "Adjuntar documentacion desde canal seguro",
            "Seguir estado y pedidos del equipo",
        ],
        "steps": [
            ("claim_type", "Tipo de caso", "Se identifica el siniestro sin publicar datos sensibles en el chat."),
            ("documents", "Documentacion", "Subis fotos o PDF para revision."),
            ("tracking", "Seguimiento", "El CRM mantiene timeline, responsable y proxima accion."),
        ],
    },
    "financiacion": {
        "title": "Financiacion o tasa",
        "description": "Revisa cuotas, tasas, impuestos, descuentos o planes de pago disponibles.",
        "webview_flow_id": "finance_fee_financing_tax",
        "primary_label": "Ver opciones de pago",
        "support_label": "Pedir plan manual",
        "crm_queue": {"id": "collections", "label": "Cobranzas y planes de pago", "sla_minutes": 120},
        "templates": ["finance_fee_financing", "finance_tax_payment", "finance_document_signature"],
        "success_events": ["financing_plan_requested", "payment_webhook", "signature_completed"],
        "analytics_events": ["debt_summary_opened", "financing_plan_requested", "tax_payment_completed"],
        "user_tasks": [
            "Revisar concepto, vencimiento y total",
            "Comparar opciones de pago o financiacion",
            "Confirmar plan con recibo auditable",
        ],
        "steps": [
            ("debt_summary", "Concepto", "Ves el detalle de la cuota, tasa o impuesto."),
            ("plan_options", "Opciones", "Elegis cuotas, descuento o pago completo cuando este disponible."),
            ("confirm", "Confirmacion", "El comprobante se genera despues de la confirmacion del proveedor."),
        ],
    },
}

_DEFAULT_FLOW = {
    "title": "Operacion financiera",
    "description": "Continua una gestion financiera segura.",
    "webview_flow_id": "finance_credit_collection_signature",
    "primary_label": "Continuar gestion segura",
    "support_label": "Pedir ayuda de un asesor",
    "crm_queue": {"id": "support", "label": "Soporte financiero", "sla_minutes": 240},
    "templates": ["finance_support_case"],
    "success_events": ["crm_operation_updated"],
    "analytics_events": ["finance_operation_opened"],
    "user_tasks": [
        "Validar el enlace seguro",
        "Revisar la informacion disponible",
        "Pedir asistencia si falta algun dato",
    ],
    "steps": [
        ("identity", "Identidad y consentimiento", "Validacion segura antes de ejecutar cualquier accion."),
        ("review", "Revision de la operacion", "El usuario confirma monto, vencimiento, documentos o destino."),
        ("confirmation", "Confirmacion trazable", "Chatboc registra evento, ticket y notificacion para el equipo."),
    ],
}

_SECURITY_HIGHLIGHTS = [
    "Nunca pedimos claves, PIN, CVV ni datos completos de tarjeta por chat.",
    "Los documentos y la identidad se validan en una vista segura.",
    "Pagos, firmas y comprobantes se confirman servidor a servidor.",
    "Cada accion deja auditoria para CRM, soporte y cumplimiento.",
]

_NEVER_REQUEST_IN_CHAT = [
    "clave bancaria",
    "token de seguridad",
    "CVV",
    "numero completo de tarjeta",
    "foto completa de DNI en WhatsApp",
]


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


def _flow_definition(flow: str) -> dict:
    return _FLOW_DEFINITIONS.get(flow, _DEFAULT_FLOW)


def _flow_steps(definition: dict, session_ready: bool) -> list[dict]:
    steps = []
    raw_steps = definition.get("steps") or _DEFAULT_FLOW["steps"]
    for index, item in enumerate(raw_steps):
        step_id, label, detail = item
        if not session_ready and index == 0:
            state = "blocked"
        elif not session_ready:
            state = "pending_session"
        elif index < min(2, len(raw_steps)):
            state = "ready"
        else:
            state = "pending"
        steps.append({"id": step_id, "label": label, "state": state, "detail": detail})
    return steps


def _finance_payload(tenant_slug: str, flow: str, operation_code: str) -> dict:
    session_token = (request.args.get("session") or request.args.get("token") or "").strip()
    amount = _money(request.args.get("amount") or request.args.get("monto"))
    currency = (request.args.get("currency") or request.args.get("moneda") or "ARS").strip().upper()[:8]
    contact_key = (request.args.get("contact_key") or request.args.get("contact") or "").strip()
    definition = _flow_definition(flow)
    session_ready = len(session_token) >= 8

    return {
        "contract_version": FINANCE_WEBVIEW_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_payload(tenant_slug),
        "operation": {
            "flow": flow,
            "code": operation_code,
            "title": definition["title"],
            "description": definition["description"],
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
            "requires_idempotency_key": True,
            "audit_trail_required": True,
            "never_request_in_chat": _NEVER_REQUEST_IN_CHAT,
            "highlights": _SECURITY_HIGHLIGHTS,
        },
        "steps": _flow_steps(definition, session_ready),
        "actions": {
            "primary": {
                "id": "continue_secure_flow",
                "label": definition["primary_label"],
                "enabled": session_ready,
                "disabled_reason": None if session_ready else "Falta token de sesion del link de WhatsApp.",
            },
            "support": {
                "id": "request_agent_help",
                "label": definition["support_label"],
                "enabled": True,
            },
        },
        "experience": {
            "webview_flow_id": definition["webview_flow_id"],
            "templates": definition["templates"],
            "crm_queue": definition["crm_queue"],
            "user_tasks": definition["user_tasks"],
            "service_level": {
                "label": definition["crm_queue"]["label"],
                "sla_minutes": definition["crm_queue"]["sla_minutes"],
            },
        },
        "compliance": {
            "source_of_truth": "server_to_server_confirmation",
            "consent_required": True,
            "sensitive_data_policy": "no_sensitive_data_in_chat",
            "allowed_chat_inputs": ["consulta", "confirmacion", "comentario", "adjunto_no_sensible"],
            "never_request_in_chat": _NEVER_REQUEST_IN_CHAT,
        },
        "events": {
            "success": definition["success_events"],
            "analytics": definition["analytics_events"],
        },
        "analytics": {
            "funnel": "finance_transactional_whatsapp",
            "flow": flow,
            "source": request.args.get("source") or "whatsapp_webview",
            "events": definition["analytics_events"],
            "crm_queue": definition["crm_queue"]["id"],
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
