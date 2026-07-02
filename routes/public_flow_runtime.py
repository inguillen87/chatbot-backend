from __future__ import annotations

import uuid
from typing import Any, Mapping

from flask import Blueprint, current_app, jsonify, request
from flask_cors import cross_origin

from services.analytics.ingestor import analytics_ingestor
from services.tenant_resolver import TenantResolutionError, resolve_tenant_only
from services.whatsapp_experience import build_whatsapp_experience


public_flow_runtime_bp = Blueprint("public_flow_runtime_bp", __name__, url_prefix="/api/public/flows")

PUBLIC_FLOW_ANALYTICS_CONTRACT_VERSION = "public.flow_runtime.analytics.v1"


_ALLOWED_ACTIONS_BY_FAMILY = {
    "claims": {"open_webview", "public_tracking_experience", "claim_public_message", "server_to_server_callback"},
    "commerce": {"open_webview", "public_cart", "public_checkout", "assisted_order_upload", "server_to_server_callback"},
    "surveys": {"open_webview", "public_survey", "survey_response", "server_to_server_callback"},
    "finance": {"open_webview", "secure_finance_webview", "server_confirmation", "server_to_server_callback"},
    "education": {"open_webview", "crm_writeback", "server_to_server_callback"},
    "government_services": {"open_webview", "crm_writeback", "server_to_server_callback"},
    "omnichannel": {"open_webview", "crm_writeback", "server_to_server_callback"},
}

_FLOW_ANALYTICS_EVENTS = [
    "flow_runtime_opened",
    "flow_action_requested",
    "flow_webview_opened",
    "flow_webview_completed",
    "flow_webview_failed",
    "catalog_viewed",
    "assisted_upload_started",
    "assisted_upload_submitted",
    "cart_started",
    "checkout_previewed",
    "checkout_session_created",
    "order_tracking_opened",
    "survey_opened",
    "survey_response_submitted",
    "survey_live_results_opened",
    "survey_heatmap_opened",
]


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _safe_token(value: Any, *, max_length: int = 96) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in raw)
    return safe.strip("-._")[:max_length]


def _execution_id_for_action(tenant, *, flow_id: str, action_id: str, idempotency_key: str, payload: Mapping[str, Any]) -> str:
    explicit = _safe_token(payload.get("execution_id") or payload.get("execution") or "")
    if explicit:
        return explicit
    seed_parts = [
        "chatboc-public-flow",
        getattr(tenant, "slug", None) or getattr(tenant, "id", ""),
        flow_id,
        action_id,
        idempotency_key or uuid.uuid4().hex,
    ]
    return f"exec-{uuid.uuid5(uuid.NAMESPACE_URL, ':'.join(str(part) for part in seed_parts)).hex[:24]}"


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(message: str, status_code: int, reason_code: str, action_hint: str):
    return _json_response(
        {
            "contract_version": "public.flow_runtime.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": False,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
        },
        status_code,
    )


def _resolve_public_tenant():
    tenant_slug = (
        request.args.get("tenant")
        or request.args.get("tenant_slug")
        or request.args.get("slug")
        or request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
    )
    widget_token = (
        request.args.get("widget_token")
        or request.args.get("entityToken")
        or request.headers.get("X-Widget-Token")
        or request.headers.get("X-Entity-Token")
    )
    whatsapp_destination_number = (
        request.args.get("whatsapp_destination_number")
        or request.args.get("to")
        or request.headers.get("X-Whatsapp-Dst")
    )
    try:
        tenant = resolve_tenant_only(
            tenant_slug=tenant_slug,
            widget_token=widget_token,
            whatsapp_destination_number=whatsapp_destination_number,
            host=request.headers.get("X-Forwarded-Host") or request.host,
            require_explicit_slug=bool(tenant_slug),
        )
    except TenantResolutionError:
        return None, _error_response(
            "Tenant no encontrado para el runtime publico.",
            404,
            "tenant_resolution_failed",
            "send_tenant_or_widget_token",
        )
    return tenant, None


def _public_action_index(runtime: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    flows = runtime.get("flows") if isinstance(runtime.get("flows"), list) else []
    for flow in flows:
        if not isinstance(flow, Mapping):
            continue
        flow_id = str(flow.get("id") or "").strip()
        family = str(flow.get("family") or "omnichannel").strip()
        allowed = _ALLOWED_ACTIONS_BY_FAMILY.get(family, _ALLOWED_ACTIONS_BY_FAMILY["omnichannel"])
        actions = flow.get("actions") if isinstance(flow.get("actions"), list) else []
        for action in actions:
            if not isinstance(action, Mapping):
                continue
            action_id = str(action.get("id") or "").strip()
            if not action_id or action_id not in allowed:
                continue
            key = f"{flow_id}:{action_id}"
            index[key] = {
                "flow_id": flow_id,
                "flow_label": flow.get("label") or flow_id,
                "flow_family": family,
                "flow_ready": bool(flow.get("ready")),
                "action": dict(action),
            }
    return index


def _tenant_analytics_id(tenant) -> int | None:
    owner_id = getattr(tenant, "municipio_id", None) or getattr(tenant, "pyme_id", None)
    try:
        return int(owner_id or tenant.id)
    except (TypeError, ValueError):
        return None


def _analytics_contract(*, callback_endpoint_template: str = "/api/public/flows/{execution_id}/callback") -> dict[str, Any]:
    return {
        "contract_version": PUBLIC_FLOW_ANALYTICS_CONTRACT_VERSION,
        "mode": "server_reconciled_public_flow_events",
        "public_client_can_write_events_directly": False,
        "client_signal_channel": "dataLayer_or_runtime_callback",
        "server_reconciliation_endpoint_template": callback_endpoint_template,
        "event_endpoint": "/api/analytics/event",
        "recommended_events": _FLOW_ANALYTICS_EVENTS,
        "funnel_stages": [
            {"id": "entry", "event": "flow_runtime_opened", "label": "Runtime abierto"},
            {"id": "intent", "event": "flow_action_requested", "label": "Accion solicitada"},
            {"id": "catalog", "event": "catalog_viewed", "label": "Catalogo visto"},
            {"id": "assisted", "event": "assisted_upload_submitted", "label": "Pedido asistido"},
            {"id": "cart", "event": "cart_started", "label": "Carrito iniciado"},
            {"id": "checkout", "event": "checkout_session_created", "label": "Checkout creado"},
            {"id": "tracking", "event": "order_tracking_opened", "label": "Seguimiento abierto"},
            {"id": "survey", "event": "survey_opened", "label": "Encuesta abierta"},
            {"id": "vote", "event": "survey_response_submitted", "label": "Voto registrado"},
            {"id": "live_results", "event": "survey_live_results_opened", "label": "Resultados en vivo"},
        ],
        "privacy": {
            "raw_payment_data_allowed": False,
            "card_data_in_chat_allowed": False,
            "public_payload_should_avoid_pii": True,
        },
    }


def _track_public_flow_callback_event(tenant, *, execution_id: str, flow_id: str, action_id: str, status: str, payload: Mapping[str, Any]):
    tenant_id = _tenant_analytics_id(tenant)
    if not tenant_id:
        return
    event_name = "flow_webview_completed" if status in {"completed", "success", "ok"} else "flow_webview_failed"
    metadata = {
        "source": "public_flow_runtime_callback",
        "execution_id": execution_id,
        "flow_id": flow_id or None,
        "action_id": action_id,
        "status": status,
        "tenant_slug": getattr(tenant, "slug", None),
        "callback_contract": "public.flow_runtime.callback.v1",
    }
    extra_events = payload.get("events") if isinstance(payload.get("events"), list) else []
    if extra_events:
        metadata["client_events"] = [str(item) for item in extra_events if item]

    try:
        analytics_ingestor.track(
            tenant_id=tenant_id,
            event_name=event_name,
            payload=metadata,
            channel=str(payload.get("channel") or request.args.get("channel") or "whatsapp"),
            session_id=str(payload.get("session_id") or payload.get("conversation_id") or execution_id),
            anon_id=payload.get("anon_id"),
            entity_ref=payload.get("entity_ref") or execution_id,
            tenant_type=getattr(tenant, "tipo", None) or getattr(tenant, "vertical", None),
        )
    except Exception:
        current_app.logger.exception("[public_flow_runtime] analytics callback event failed")


@public_flow_runtime_bp.route("/runtime", methods=["GET", "OPTIONS"])
@cross_origin(origins="*", methods=["GET", "OPTIONS"])
def public_flow_runtime():
    if request.method == "OPTIONS":
        return "", 204
    tenant, error = _resolve_public_tenant()
    if error:
        return error

    experience = build_whatsapp_experience(tenant, app_config=current_app.config)
    runtime = experience.get("flow_runtime") if isinstance(experience, Mapping) else {}
    if not isinstance(runtime, dict):
        runtime = {}

    public_runtime = {
        **runtime,
        "contract_version": "public.whatsapp.flow_runtime.v1",
        "admin_contract_version": runtime.get("contract_version"),
        "tenant": {
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "vertical": tenant.vertical,
        },
        "channel": request.args.get("channel") or "whatsapp",
        "action_manifest": {
            "contract_version": "public.flow_runtime.action_manifest.v1",
            "post_endpoint": "/api/public/flows/actions",
            "requires_idempotency_key_for_mutations": True,
            "execution_policy": {
                "contract_version": "public.flow_runtime.execution_policy.v1",
                "id_strategy": "client_supplied_or_server_deterministic_from_idempotency_key",
                "callback_endpoint_template": "/api/public/flows/{execution_id}/callback",
                "resume_policy": "resume_conversation_on_callback_or_timeout",
            },
            "allowed_actions": sorted(_public_action_index(runtime).keys()),
            "analytics": _analytics_contract(),
        },
        "privacy": {
            "public_runtime_contains_secrets": False,
            "sensitive_user_data_in_chat_allowed": False,
            "signed_session_required_for_private_records": True,
        },
    }
    return _json_response(public_runtime)


@public_flow_runtime_bp.route("/actions", methods=["POST", "OPTIONS"])
@cross_origin(origins="*", methods=["POST", "OPTIONS"])
def public_flow_action_manifest():
    if request.method == "OPTIONS":
        return "", 204
    tenant, error = _resolve_public_tenant()
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    flow_id = str(payload.get("flow_id") or payload.get("flow") or "").strip()
    action_id = str(payload.get("action_id") or payload.get("action") or "").strip()
    idempotency_key = (
        str(payload.get("idempotency_key") or "").strip()
        or request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or ""
    ).strip()
    if not flow_id or not action_id:
        return _error_response("flow_id y action_id son obligatorios.", 400, "flow_action_required", "send_flow_and_action")

    experience = build_whatsapp_experience(tenant, app_config=current_app.config)
    runtime = experience.get("flow_runtime") if isinstance(experience, Mapping) else {}
    if not isinstance(runtime, dict):
        runtime = {}
    index = _public_action_index(runtime)
    action_key = f"{flow_id}:{action_id}"
    action_ref = index.get(action_key)
    if not action_ref:
        return _error_response("Accion no habilitada para este runtime publico.", 404, "flow_action_not_found", "refresh_runtime")

    method = str((action_ref.get("action") or {}).get("method") or "GET").upper()
    requires_idempotency = method not in {"GET", "HEAD", "OPTIONS"} or bool(
        (action_ref.get("action") or {}).get("idempotency_required")
    )
    if requires_idempotency and not idempotency_key:
        return _error_response(
            "idempotency_key requerido para ejecutar esta accion.",
            409,
            "idempotency_key_required",
            "send_idempotency_key",
        )

    execution_id = _execution_id_for_action(
        tenant,
        flow_id=flow_id,
        action_id=action_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    callback_endpoint = f"/api/public/flows/{execution_id}/callback"
    action = dict(action_ref["action"])
    target_endpoint = action.get("endpoint") or action.get("endpoint_template")
    return _json_response(
        {
            "contract_version": "public.flow_runtime.action.v1",
            "ok": True,
            "tenant": {"slug": tenant.slug, "tipo": tenant.tipo, "vertical": tenant.vertical},
            "flow": {
                "id": action_ref["flow_id"],
                "label": action_ref["flow_label"],
                "family": action_ref["flow_family"],
                "ready": action_ref["flow_ready"],
            },
            "action": action,
            "execution": {
                "id": execution_id,
                "flow_id": action_ref["flow_id"],
                "action_id": action_id,
                "idempotency_key": idempotency_key or None,
                "callback_endpoint": callback_endpoint,
                "callback_endpoint_template": "/api/public/flows/{execution_id}/callback",
                "status": "created",
            },
            "handoff": {
                "mode": "client_executes_target_endpoint",
                "target_endpoint": target_endpoint,
                "method": method,
                "idempotency_key": idempotency_key or None,
                "execution_id": execution_id,
                "callback_endpoint": callback_endpoint,
                "resume_policy": "resume_conversation_on_callback_or_timeout",
            },
            "crm_writeback": {
                "required": action_id in {"server_to_server_callback", "claim_public_message", "assisted_order_upload"},
                "callback_endpoint_template": "/api/public/flows/{execution_id}/callback",
            },
            "analytics": _analytics_contract(),
        }
    )


@public_flow_runtime_bp.route("/<string:execution_id>/callback", methods=["POST", "OPTIONS"])
@cross_origin(origins="*", methods=["POST", "OPTIONS"])
def public_flow_runtime_callback(execution_id: str):
    if request.method == "OPTIONS":
        return "", 204
    tenant, error = _resolve_public_tenant()
    if error:
        return error

    execution_id = str(execution_id or "").strip()
    if not execution_id:
        return _error_response("execution_id requerido.", 400, "execution_id_required", "send_execution_id")

    idempotency_key = (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or request.args.get("idempotency_key")
        or ""
    ).strip()
    if not idempotency_key:
        return _error_response(
            "idempotency_key requerido para confirmar el webview.",
            409,
            "idempotency_key_required",
            "send_idempotency_key",
        )

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    flow_id = str(payload.get("flow_id") or payload.get("flow") or "").strip()
    action_id = str(payload.get("action_id") or payload.get("action") or "server_to_server_callback").strip()
    status = str(payload.get("status") or payload.get("state") or "completed").strip().lower()
    writebacks = payload.get("writebacks") if isinstance(payload.get("writebacks"), list) else []

    _track_public_flow_callback_event(
        tenant,
        execution_id=execution_id,
        flow_id=flow_id,
        action_id=action_id,
        status=status,
        payload=payload,
    )

    current_app.logger.info(
        "PUBLIC_FLOW_CALLBACK tenant=%s execution=%s flow=%s action=%s status=%s idem=%s",
        tenant.slug,
        execution_id,
        flow_id,
        action_id,
        status,
        idempotency_key,
    )

    return _json_response(
        {
            "contract_version": "public.flow_runtime.callback.v1",
            "ok": True,
            "accepted": True,
            "tenant": {"slug": tenant.slug, "tipo": tenant.tipo, "vertical": tenant.vertical},
            "execution": {
                "id": execution_id,
                "flow_id": flow_id or None,
                "action_id": action_id,
                "status": status,
                "idempotency_key": idempotency_key,
            },
            "conversation": {
                "resume": True,
                "resume_policy": "resume_conversation_on_callback_or_timeout",
            },
            "crm_writeback": {
                "mode": "accepted_for_reconciliation",
                "writebacks": [str(item) for item in writebacks if item],
            },
            "analytics": {
                "contract_version": PUBLIC_FLOW_ANALYTICS_CONTRACT_VERSION,
                "event_name": "flow_webview_completed" if status in {"completed", "success", "ok"} else "flow_webview_failed",
                "event_ingested": True,
                "event_endpoint": "/api/analytics/event",
            },
        }
    )
