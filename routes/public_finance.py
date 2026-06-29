from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from models import AnalyticsEventV2, TenantProfile, TenantTicket, db


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

_SENSITIVE_TEXT_MARKERS = (
    "cvv",
    "clave bancaria",
    "clave de home banking",
    "clave home banking",
    "pin bancario",
    "pin de tarjeta",
    "token de seguridad",
    "codigo de seguridad",
    "password",
    "contrasena",
    "contraseña",
    "numero completo de tarjeta",
    "tarjeta completa",
)

_BASE_ACTIONS = {
    "continue_secure_flow": {
        "label": "Continuar en pantalla segura",
        "event": "finance_secure_flow_continued",
        "status": "ready_for_customer_review",
        "next_step": "secure_webview",
    },
    "request_agent_help": {
        "label": "Pedir asistencia",
        "event": "finance_agent_help_requested",
        "status": "waiting_agent",
        "next_step": "crm_queue",
    },
    "add_public_comment": {
        "label": "Agregar comentario",
        "event": "finance_comment_added",
        "status": "comment_registered",
        "next_step": "crm_timeline",
    },
}

_FLOW_ACTIONS = {
    "alta": [
        ("submit_kyc", "Enviar validacion KYC", "kyc_submitted", "identity_review"),
        ("upload_document", "Adjuntar documento seguro", "finance_document_upload_requested", "secure_upload"),
    ],
    "operacion": [
        ("pay_securely", "Pagar ahora", "finance_payment_started", "secure_checkout"),
        ("request_payment_plan", "Solicitar plan de pago", "finance_payment_plan_requested", "collections_queue"),
        ("sign_document", "Firmar documento", "finance_signature_started", "signature_flow"),
    ],
    "cuentas": [
        ("open_statement", "Abrir resumen", "finance_statement_opened", "secure_statement"),
        ("track_case", "Ver caso de soporte", "finance_case_tracked", "support_timeline"),
    ],
    "transferencias": [
        ("track_transfer", "Seguir transferencia", "finance_transfer_tracked", "transfer_tracking"),
        ("download_transfer_receipt", "Descargar comprobante", "finance_transfer_receipt_requested", "receipt_view"),
    ],
    "seguros": [
        ("start_insurance_claim", "Iniciar siniestro", "finance_insurance_claim_started", "claim_form"),
        ("upload_document", "Adjuntar documentacion", "finance_insurance_document_requested", "secure_upload"),
        ("track_case", "Ver seguimiento", "finance_case_tracked", "support_timeline"),
    ],
    "financiacion": [
        ("review_financing_plan", "Comparar financiacion", "finance_financing_reviewed", "plan_options"),
        ("request_payment_plan", "Solicitar plan", "finance_payment_plan_requested", "collections_queue"),
        ("pay_tax", "Pagar tasa o cuota", "finance_tax_payment_started", "secure_checkout"),
        ("sign_document", "Firmar acuerdo", "finance_signature_started", "signature_flow"),
    ],
}


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


def _request_value(data: Mapping[str, Any] | None, *keys: str) -> str:
    data = data if isinstance(data, Mapping) else {}
    for key in keys:
        raw = request.args.get(key)
        if raw not in (None, ""):
            return str(raw).strip()
        raw = data.get(key)
        if raw not in (None, ""):
            return str(raw).strip()
    return ""


def _clean_text(value: Any, *, limit: int = 500) -> str:
    cleaned = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return cleaned[:limit]


def _contains_sensitive_finance_text(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in _SENSITIVE_TEXT_MARKERS)


def _contact_payload(data: Mapping[str, Any] | None) -> dict:
    data = data if isinstance(data, Mapping) else {}
    raw_contact = data.get("contact") if isinstance(data.get("contact"), Mapping) else {}
    return {
        "name": _clean_text(raw_contact.get("name") or data.get("contact_name"), limit=120) or None,
        "phone": _clean_text(raw_contact.get("phone") or data.get("phone") or data.get("telefono"), limit=80) or None,
        "email": _clean_text(raw_contact.get("email") or data.get("email"), limit=120) or None,
        "contact_key": _clean_text(
            raw_contact.get("contact_key") or data.get("contact_key") or data.get("contact"),
            limit=160,
        )
        or None,
    }


def _available_actions(flow: str, definition: Mapping[str, Any], session_ready: bool) -> list[dict]:
    actions: list[dict] = []
    for action_id in ("continue_secure_flow", "request_agent_help", "add_public_comment"):
        action = dict(_BASE_ACTIONS[action_id])
        action.update(
            {
                "id": action_id,
                "enabled": action_id != "continue_secure_flow" or session_ready,
                "disabled_reason": None
                if action_id != "continue_secure_flow" or session_ready
                else "Falta token de sesion del link de WhatsApp.",
            }
        )
        actions.append(action)

    for action_id, label, event, next_step in _FLOW_ACTIONS.get(flow, []):
        actions.append(
            {
                "id": action_id,
                "label": label,
                "event": event,
                "status": "registered",
                "next_step": next_step,
                "enabled": session_ready,
                "disabled_reason": None if session_ready else "Falta token de sesion del link de WhatsApp.",
            }
        )
    return actions


def _action_contract(flow: str, definition: Mapping[str, Any], action_id: str, session_ready: bool) -> dict | None:
    for action in _available_actions(flow, definition, session_ready):
        if action["id"] == action_id:
            return action
    return None


def _finance_ticket_fingerprint(tenant_id: int, flow: str, operation_code: str, action_id: str, idempotency_key: str) -> str:
    raw = f"{tenant_id}:{flow}:{operation_code}:{action_id}:{idempotency_key}"
    digest = sha256(raw.encode("utf-8")).hexdigest()[:48]
    return f"finance:{digest}"


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


def _finance_payload(tenant_slug: str, flow: str, operation_code: str, data: Mapping[str, Any] | None = None) -> dict:
    session_token = _request_value(data, "session", "token", "session_token")
    amount = _money(_request_value(data, "amount", "monto"))
    currency = (_request_value(data, "currency", "moneda") or "ARS").strip().upper()[:8]
    contact_key = _request_value(data, "contact_key", "contact")
    definition = _flow_definition(flow)
    session_ready = len(session_token) >= 8
    actions = _available_actions(flow, definition, session_ready)

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
        "frontend_contract": {
            "render_as": "finance_secure_operation_view",
            "layout": "timeline_plus_action_panel",
            "primary_sections": ["hero_status", "secure_actions", "steps", "crm_followup", "audit_policy"],
            "cta_stack": [action["id"] for action in actions if action.get("enabled")][:4],
            "disabled_cta_stack": [action["id"] for action in actions if not action.get("enabled")],
            "empty_state": "finance_session_required" if not session_ready else None,
            "copy_tone": "clear_compliance_first",
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
        "action_catalog": actions,
    }


def _finance_action_response(
    *,
    tenant: TenantProfile,
    flow: str,
    operation_code: str,
    action: Mapping[str, Any],
    ticket: TenantTicket,
    duplicate: bool,
) -> dict:
    definition = _flow_definition(flow)
    ticket_extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    finance_extra = ticket_extra.get("finance") if isinstance(ticket_extra.get("finance"), dict) else {}
    return {
        "contract_version": "finance.action.v1",
        "status": "duplicate" if duplicate else "accepted",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "vertical": tenant.vertical or "finanzas",
        },
        "operation": {
            "flow": flow,
            "code": operation_code,
            "webview_flow_id": definition["webview_flow_id"],
            "amount": finance_extra.get("amount"),
            "currency": finance_extra.get("currency"),
            "status": action.get("status") or "registered",
        },
        "action": {
            "id": action["id"],
            "label": action["label"],
            "event": action["event"],
            "next_step": action["next_step"],
        },
        "ticket": {
            "id": ticket.id,
            "status": ticket.estado,
            "category": ticket.categoria,
            "fingerprint": ticket.fingerprint,
            "crm_queue": definition["crm_queue"],
        },
        "crm_followup": {
            "queue": definition["crm_queue"],
            "priority": ticket_extra.get("priority") or "high",
            "owner_hint": definition["crm_queue"]["label"],
            "next_actions": [
                "assign_agent",
                "review_customer_context",
                "answer_from_ticket_thread",
                "send_approved_template_if_outside_24h",
            ],
            "template_candidates": definition["templates"],
            "webview_flow_id": definition["webview_flow_id"],
        },
        "analytics": {
            "event_name": action["event"],
            "funnel": "finance_transactional_whatsapp",
            "entity_ref": f"tenant_ticket:{ticket.id}",
        },
        "frontend_contract": {
            "render_as": "finance_action_result",
            "toast": "Gestion registrada" if not duplicate else "Gestion ya registrada",
            "refresh_payload_url": f"/api/public/finance/{tenant.slug}/{flow}/{operation_code}",
            "show_ticket_reference": True,
        },
    }


def _create_or_get_finance_ticket(
    *,
    tenant: TenantProfile,
    flow: str,
    operation_code: str,
    action: Mapping[str, Any],
    data: Mapping[str, Any],
) -> tuple[TenantTicket, bool]:
    definition = _flow_definition(flow)
    idempotency_key = _request_value(data, "idempotency_key", "request_id") or uuid.uuid4().hex
    fingerprint = _finance_ticket_fingerprint(tenant.id, flow, operation_code, action["id"], idempotency_key)
    existing = TenantTicket.query.filter_by(tenant_id=tenant.id, fingerprint=fingerprint).first()
    if existing:
        return existing, True

    comment = _clean_text(data.get("comment") or data.get("message") or data.get("consulta"), limit=1200)
    contact = _contact_payload(data)
    amount = _money(_request_value(data, "amount", "monto"))
    currency = (_request_value(data, "currency", "moneda") or "ARS").strip().upper()[:8]
    session_token = _request_value(data, "session", "token", "session_token")
    session_ready = len(session_token) >= 8
    title = f"{definition['title']} {operation_code}".strip()
    description_parts = [
        f"Accion financiera: {action['label']}",
        f"Operacion: {operation_code}",
        f"Flujo: {flow}",
    ]
    if amount:
        description_parts.append(f"Monto: {amount} {currency}")
    if comment:
        description_parts.append(f"Comentario: {comment}")

    ticket = TenantTicket(
        tenant_id=tenant.id,
        categoria=definition["crm_queue"]["label"],
        descripcion="\n".join(description_parts),
        estado="nuevo",
        origen="webview",
        fingerprint=fingerprint,
        datos_extra={
            "title": title,
            "type": "finance_operation",
            "priority": "high" if flow in {"operacion", "transferencias", "financiacion"} else "medium",
            "channel": "whatsapp_webview",
            "source": "public_finance_webview",
            "contact": {key: value for key, value in contact.items() if value},
            "comments": [
                {
                    "id": uuid.uuid4().hex,
                    "body": comment,
                    "visibility": "public",
                    "author_user_id": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            if comment
            else [],
            "finance": {
                "contract_version": "finance.action.v1",
                "flow": flow,
                "operation_code": operation_code,
                "webview_flow_id": definition["webview_flow_id"],
                "action_id": action["id"],
                "action_event": action["event"],
                "next_step": action["next_step"],
                "amount": amount,
                "currency": currency,
                "session_state": "present" if session_ready else "missing_or_short",
                "crm_queue": definition["crm_queue"],
                "templates": definition["templates"],
                "idempotency_key": idempotency_key,
                "security_policy": {
                    "server_to_server_confirmation_required": True,
                    "card_data_in_chat_allowed": False,
                    "identity_data_in_chat_allowed": False,
                    "never_request_in_chat": _NEVER_REQUEST_IN_CHAT,
                },
            },
        },
    )
    db.session.add(ticket)
    db.session.flush()

    event = AnalyticsEventV2(
        tenant_id=tenant.id,
        tenant_type=tenant.tipo or "pyme",
        channel="whatsapp_webview",
        event_name=action["event"],
        session_id=session_token or None,
        entity_ref=f"tenant_ticket:{ticket.id}",
        metadata_payload={
            "contract_version": "finance.action.v1",
            "flow": flow,
            "operation_code": operation_code,
            "action_id": action["id"],
            "amount": amount,
            "currency": currency,
            "crm_queue": definition["crm_queue"]["id"],
            "webview_flow_id": definition["webview_flow_id"],
            "contact_key": contact.get("contact_key"),
            "source": "public_finance_webview",
        },
    )
    db.session.add(event)
    db.session.commit()
    return ticket, False


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


@public_finance_bp.post("/api/public/finance/<tenant_slug>/<flow>/<operation_code>/actions")
@public_finance_bp.post("/finanzas/<tenant_slug>/<flow>/<operation_code>/actions")
def public_finance_action(tenant_slug: str, flow: str, operation_code: str):
    tenant_slug = (tenant_slug or "").strip().lower()
    flow = (flow or "").strip().lower()
    operation_code = (operation_code or "").strip()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, Mapping):
        data = {}

    tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
    if not tenant:
        return _json(
            {
                "contract_version": "finance.action.v1",
                "status_code": 404,
                "reason_code": "tenant_not_found",
                "action_hint": "create_or_select_finance_tenant",
                "error": {"message": "No encontramos el tenant para registrar esta gestion financiera."},
            },
            404,
        )

    definition = _flow_definition(flow)
    session_token = _request_value(data, "session", "token", "session_token")
    session_ready = len(session_token) >= 8
    action_id = _request_value(data, "action_id", "action") or "continue_secure_flow"
    action = _action_contract(flow, definition, action_id, session_ready)
    if not action:
        return _json(
            {
                "contract_version": "finance.action.v1",
                "status_code": 400,
                "reason_code": "finance_action_not_supported",
                "action_hint": "use_action_catalog",
                "supported_actions": [item["id"] for item in _available_actions(flow, definition, session_ready)],
                "error": {"message": "La accion solicitada no existe para este flujo financiero."},
            },
            400,
        )
    if not action.get("enabled"):
        return _json(
            {
                "contract_version": "finance.action.v1",
                "status_code": 409,
                "reason_code": "finance_session_required",
                "action_hint": "open_from_signed_whatsapp_link",
                "action": {"id": action["id"], "label": action["label"]},
                "error": {"message": action.get("disabled_reason") or "Falta sesion segura."},
            },
            409,
        )

    comment = _clean_text(data.get("comment") or data.get("message") or data.get("consulta"), limit=1200)
    if _contains_sensitive_finance_text(comment):
        return _json(
            {
                "contract_version": "finance.action.v1",
                "status_code": 422,
                "reason_code": "sensitive_finance_data_rejected",
                "action_hint": "use_secure_webview_fields",
                "security_policy": {
                    "card_data_in_chat_allowed": False,
                    "identity_data_in_chat_allowed": False,
                    "never_request_in_chat": _NEVER_REQUEST_IN_CHAT,
                },
                "error": {
                    "message": "No envies claves, PIN, CVV ni datos completos de tarjeta. Usa la pantalla segura.",
                },
            },
            422,
        )

    ticket, duplicate = _create_or_get_finance_ticket(
        tenant=tenant,
        flow=flow,
        operation_code=operation_code,
        action=action,
        data=data,
    )
    return _json(
        _finance_action_response(
            tenant=tenant,
            flow=flow,
            operation_code=operation_code,
            action=action,
            ticket=ticket,
            duplicate=duplicate,
        ),
        200 if duplicate else 201,
    )
