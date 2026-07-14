from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from flask import Blueprint, abort, current_app, g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from twilio.rest import Client

from models import AuditEvent, MessageTemplateRegistry, NotificationTemplate, ProviderConnection, User, db
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService
from services.whatsapp_experience import build_whatsapp_experience
from services.plan_access import integration_access_payload, integration_frontend_contract
from services.twilio_tech_provider import TwilioRuntimeCredentials, resolve_twilio_runtime_credentials
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import token_requerido
from utils.roles import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role
from utils.tenant import require_tenant

whatsapp_rules_bp = Blueprint("whatsapp_rules_bp", __name__)
TWILIO_CONTENT_API_URL = "https://content.twilio.com/v1/Content"
TWILIO_CONFIRMATION_TTL_SECONDS = 15 * 60


class TwilioContentApiError(RuntimeError):
    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


def _guard(user: User, tenant):
    if canonical_role(getattr(user, "rol", None)) not in {ROLE_TENANT_ADMIN, ROLE_SUPERADMIN}:
        abort(403, description="Permisos insuficientes")
    if not _is_authorized_for_tenant(user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")


def _template_registry_payload(row: MessageTemplateRegistry | None) -> dict:
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "id": row.id,
        "name": row.name,
        "language": row.language,
        "category": row.category,
        "status": row.status,
        "content_sid": row.content_sid,
        "external_template_id": row.external_template_id,
        "last_sync_at": row.last_sync_at.isoformat() if row.last_sync_at else None,
    }


def _find_twilio_manifest_item(tenant, template_id: str) -> dict | None:
    payload = build_whatsapp_experience(tenant, app_config=current_app.config)
    manifest = ((payload.get("template_blueprint") or {}).get("creation_manifest") or {})
    for item in manifest.get("items") or []:
        if str(item.get("id") or "").lower() == template_id.lower():
            return item
    return None


def _find_meta_flow_blueprint(tenant, flow_id: str) -> dict | None:
    payload = build_whatsapp_experience(tenant, app_config=current_app.config)
    flows = ((payload.get("webview_blueprint") or {}).get("flows") or [])
    for flow in flows:
        if str(flow.get("id") or "").lower() != flow_id.lower():
            continue
        blueprint = flow.get("meta_flow_blueprint")
        if not isinstance(blueprint, dict) or not blueprint.get("safe_for_whatsapp_flow"):
            return None
        return {"flow": flow, "blueprint": blueprint}
    return None


def _sync_status_from_approval(status: str | None, *, submitted: bool) -> str:
    value = str(status or "").strip().lower()
    if value == "approved":
        return "approved"
    if value == "rejected":
        return "rejected"
    if value in {"paused", "disabled"}:
        return "disabled"
    if value in {"pending", "received", "in_review"}:
        return "pending_approval"
    if value in {"draft", "unsubmitted"}:
        return "created"
    if submitted and not value:
        return "pending_approval"
    return "created"


def _mapping_or_attributes(value) -> dict:
    if isinstance(value, dict):
        return value
    result = {}
    for key in ("status", "approval_status", "rejection_reason", "whatsapp", "flow", "flows"):
        candidate = getattr(value, key, None)
        if candidate is not None:
            result[key] = candidate
    return result


def _approval_details(value) -> dict:
    payload = _mapping_or_attributes(value)
    whatsapp = _mapping_or_attributes(payload.get("whatsapp"))
    flow = _mapping_or_attributes(
        whatsapp.get("flow")
        or whatsapp.get("flows")
        or whatsapp.get("whatsapp/flows")
        or payload.get("flow")
        or payload.get("flows")
    )
    status = (
        flow.get("status")
        or whatsapp.get("status")
        or payload.get("status")
        or payload.get("approval_status")
    )
    flow_status = flow.get("status") or (status if flow else None)
    rejection_reason = (
        flow.get("rejection_reason")
        or whatsapp.get("rejection_reason")
        or payload.get("rejection_reason")
    )
    return {
        "status": str(status or "").strip() or None,
        "flow_status": str(flow_status or "").strip() or None,
        "rejection_reason": str(rejection_reason or "").strip() or None,
    }


def _twilio_credentials(tenant) -> TwilioRuntimeCredentials:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    tech_state = cfg.get("twilio_tech_provider") if isinstance(cfg.get("twilio_tech_provider"), dict) else {}
    scoped_account_sid = str(tech_state.get("twilio_account_sid") or "").strip()

    query = ProviderConnection.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
    )
    provider_connection = None
    if scoped_account_sid:
        provider_connection = query.filter_by(external_account_id=scoped_account_sid).first()
    if provider_connection is None:
        connections = query.order_by(ProviderConnection.updated_at.desc()).limit(2).all()
        if len(connections) == 1:
            provider_connection = connections[0]

    return resolve_twilio_runtime_credentials(
        tenant=tenant,
        provider_connection=provider_connection,
        app_config=current_app.config,
    )


def _twilio_client(tenant) -> tuple[Client, TwilioRuntimeCredentials]:
    credentials = _twilio_credentials(tenant)
    if not credentials.ready:
        if credentials.scope == "conflict":
            abort(409, description="La cuenta Twilio del tenant no coincide con su conexion registrada")
        abort(503, description="Las credenciales Twilio del tenant no estan configuradas para esta cuenta")
    return Client(credentials.account_sid, credentials.auth_token), credentials


def _confirmation_serializer() -> URLSafeTimedSerializer:
    secret = str(current_app.config.get("SECRET_KEY") or "").strip()
    if not secret:
        abort(503, description="SECRET_KEY no configurada para confirmar operaciones reales")
    return URLSafeTimedSerializer(secret, salt="chatboc.twilio-content-sync.v1")


def _confirmation_manifest_digest(*payloads: dict) -> str:
    serialized = json.dumps(payloads, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _issue_execution_confirmation(*, purpose: str, tenant, user: User, fields: dict) -> str:
    return _confirmation_serializer().dumps(
        {
            "purpose": purpose,
            "tenant_id": tenant.id,
            "actor_user_id": user.id,
            **fields,
        }
    )


def _verify_execution_confirmation(
    token,
    *,
    purpose: str,
    tenant,
    user: User,
    fields: dict,
) -> None:
    try:
        decoded = _confirmation_serializer().loads(
            str(token or ""),
            max_age=TWILIO_CONFIRMATION_TTL_SECONDS,
        )
    except SignatureExpired:
        abort(409, description="La confirmacion vencio; valida nuevamente antes de ejecutar")
    except BadSignature:
        abort(409, description="Confirmacion invalida; valida nuevamente antes de ejecutar")
    expected = {
        "purpose": purpose,
        "tenant_id": tenant.id,
        "actor_user_id": user.id,
        **fields,
    }
    if decoded != expected:
        abort(409, description="La confirmacion no corresponde a esta operacion o tenant")


def _twilio_json_request(client: Client, method: str, url: str, *, payload: dict | None = None) -> dict:
    response = client.request(
        method,
        url,
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=20,
    )
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise TwilioContentApiError(status_code, "twilio_content_api_rejected_request")
    try:
        decoded = json.loads(str(getattr(response, "text", "") or "{}"))
    except json.JSONDecodeError as exc:
        raise TwilioContentApiError(status_code, "twilio_content_api_invalid_json") from exc
    if not isinstance(decoded, dict):
        raise TwilioContentApiError(status_code, "twilio_content_api_invalid_contract")
    return decoded


def _twilio_create_content(client: Client, create_request: dict) -> dict:
    return _twilio_json_request(client, "POST", TWILIO_CONTENT_API_URL, payload=create_request)


def _twilio_request_approval(client: Client, content_sid: str, approval_request: dict) -> dict:
    return _twilio_json_request(
        client,
        "POST",
        f"{TWILIO_CONTENT_API_URL}/{content_sid}/ApprovalRequests/whatsapp",
        payload=approval_request,
    )


def _twilio_fetch_approval(client: Client, content_sid: str) -> dict:
    return _twilio_json_request(
        client,
        "GET",
        f"{TWILIO_CONTENT_API_URL}/{content_sid}/ApprovalRequests",
    )


def _claim_registry_sync(
    existing: MessageTemplateRegistry | None,
    *,
    tenant,
    friendly_name: str,
    language: str,
    category: str,
    force: bool,
    metadata: dict,
) -> tuple[MessageTemplateRegistry, str]:
    if existing and existing.status in {"syncing", "sync_uncertain"} and not force:
        abort(409, description="Ya existe una sincronizacion en curso o con resultado incierto")
    row = existing or MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=friendly_name,
        language=language,
    )
    operation_id = uuid4().hex
    merged_metadata = dict(row.metadata_json) if isinstance(row.metadata_json, dict) else {}
    merged_metadata.update(metadata)
    merged_metadata.update(
        {
            "sync_operation_id": operation_id,
            "sync_state": "creating_content",
            "sync_started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    row.category = category
    row.status = "syncing"
    row.metadata_json = merged_metadata
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, description="Otra sincronizacion creo este registro; actualiza antes de reintentar")
    return row, operation_id


def _mark_registry_sync_failure(
    row: MessageTemplateRegistry,
    *,
    operation_id: str,
    stage: str,
    content_sid: str | None = None,
) -> None:
    metadata = dict(row.metadata_json) if isinstance(row.metadata_json, dict) else {}
    metadata.update(
        {
            "sync_operation_id": operation_id,
            "sync_state": "uncertain" if stage == "content_create" else "approval_failed",
            "sync_failed_stage": stage,
            "sync_failed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    row.metadata_json = metadata
    row.status = "sync_uncertain" if stage == "content_create" else "approval_failed"
    if content_sid:
        row.content_sid = content_sid
    db.session.add(row)
    db.session.commit()


def _validated_meta_flow_id(value) -> str:
    meta_flow_id = str(value or "").strip()
    if not re.fullmatch(r"\d{6,32}", meta_flow_id):
        abort(400, description="meta_flow_id debe ser el identificador numerico real publicado por Meta")
    return meta_flow_id


def _flow_content_copy(payload: dict, blueprint: dict) -> tuple[str, str]:
    screens = blueprint.get("screens") if isinstance(blueprint.get("screens"), list) else []
    first_screen = screens[0] if screens and isinstance(screens[0], dict) else {}
    first_title = str(first_screen.get("title") or "gestion").strip()
    body = str(
        payload.get("body")
        or f"Completa {first_title.lower()} de forma segura sin salir de WhatsApp."
    ).strip()
    button_text = str(payload.get("button_text") or "Abrir gestion").strip()
    if not body or len(body) > 1024:
        abort(400, description="body es requerido y admite hasta 1024 caracteres")
    if not button_text or len(button_text) > 20:
        abort(400, description="button_text es requerido y admite hasta 20 caracteres")
    return body, button_text


def _twilio_template_frontend_contract(access: dict, *, action: str) -> dict:
    return integration_frontend_contract(
        access,
        "whatsapp_sender_management",
        render_as="whatsapp_template_creation_lock",
        primary_action="upgrade_to_full",
        action=action,
        allow_dry_run_preview=True,
        show_payload_preview=True,
        hide_execute_controls=not bool(access.get("enabled")),
        hide_refresh_controls=not bool(access.get("enabled")),
        copy={
            "title": "Plantillas productivas bloqueadas",
            "description": (
                access.get("message")
                or "Las plantillas productivas de WhatsApp requieren plan Full activo."
            ),
        },
    )


def _twilio_operator_guardrails(access: dict, *, action: str, execute_confirmation_issued: bool) -> dict:
    enabled = bool(access.get("enabled"))
    blocked_actions = (
        ["create_twilio_native_flow", "submit_twilio_native_flow_for_approval"]
        if action == "create_twilio_native_flow"
        else ["create_twilio_content_template", "refresh_twilio_content_template"]
    )
    return {
        "contract_version": "twilio.content.operator_guardrails.v1",
        "action": action,
        "dry_run_preview_allowed": True,
        "show_manifest_payload": True,
        "execute_confirmation_issued": bool(enabled and execute_confirmation_issued),
        "twilio_call_allowed": enabled,
        "requires_full_plan": not enabled,
        "blocked_actions": [] if enabled else blocked_actions,
        "next_action": "execute_twilio_content_call" if enabled else "upgrade_to_full",
    }


def _twilio_access_lock_response(tenant, *, action: str):
    access = integration_access_payload(tenant)
    return jsonify(
        {
            "ok": False,
            "blocked": True,
            "error": "plan_required",
            "reason": access.get("reason_code") or "plan_full_required",
            "locked_reason": access.get("lock_reason_code") or access.get("reason_code") or "plan_full_required",
            "action": action,
            "message": access.get("message"),
            "integration_access": access,
            "frontend_contract": _twilio_template_frontend_contract(access, action=action),
            "operator_guardrails": _twilio_operator_guardrails(
                access,
                action=action,
                execute_confirmation_issued=False,
            ),
            "upgrade": access.get("upgrade"),
        }
    ), 403


@whatsapp_rules_bp.route("/api/admin/whatsapp/rules", methods=["GET"])
@token_requerido
@require_tenant
def get_rules(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    rule = WhatsAppEnterpriseRulesService(tenant.id).get_or_create()
    return jsonify(
        {
            "tenant_id": tenant.id,
            "enforce_template_outside_24h": rule.enforce_template_outside_24h,
            "max_outbound_per_hour": rule.max_outbound_per_hour,
            "quiet_hours_start": rule.quiet_hours_start,
            "quiet_hours_end": rule.quiet_hours_end,
            "blocked_keywords": rule.blocked_keywords or [],
        }
    )


@whatsapp_rules_bp.route("/api/admin/whatsapp/rules", methods=["PUT"])
@token_requerido
@require_tenant
def update_rules(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)

    payload = request.get_json(silent=True) or {}
    svc = WhatsAppEnterpriseRulesService(tenant.id)
    rule = svc.get_or_create()

    if "enforce_template_outside_24h" in payload:
        rule.enforce_template_outside_24h = bool(payload.get("enforce_template_outside_24h"))
    if "max_outbound_per_hour" in payload:
        rule.max_outbound_per_hour = payload.get("max_outbound_per_hour")
    if "quiet_hours_start" in payload:
        rule.quiet_hours_start = payload.get("quiet_hours_start")
    if "quiet_hours_end" in payload:
        rule.quiet_hours_end = payload.get("quiet_hours_end")
    if isinstance(payload.get("blocked_keywords"), list):
        rule.blocked_keywords = payload.get("blocked_keywords")

    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_rules.updated",
            resource_type="whatsapp_enterprise_rule",
            resource_id=str(rule.id),
            details=payload,
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify({"updated": True})


@whatsapp_rules_bp.route("/api/admin/templates", methods=["GET"])
@token_requerido
@require_tenant
def list_whatsapp_templates(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    templates = (
        NotificationTemplate.query.filter_by(tenant_id=tenant.id, channel="whatsapp")
        .order_by(NotificationTemplate.created_at.desc())
        .all()
    )
    out = []
    for t in templates:
        meta = t.metadata_json if isinstance(t.metadata_json, dict) else {}
        out.append(
            {
                "id": t.id,
                "key": t.key,
                "channel": t.channel,
                "body_template": t.body_template,
                "subject_template": t.subject_template,
                "is_active": t.is_active,
                "health_status": meta.get("health_status", "unknown"),
                "metadata": meta,
            }
        )
    return jsonify(out)


@whatsapp_rules_bp.route("/api/admin/templates", methods=["POST"])
@token_requerido
@require_tenant
def create_whatsapp_template(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}

    key = str(payload.get("key") or "").strip().lower()
    body_template = str(payload.get("body_template") or "").strip()
    if not key or not body_template:
        abort(400, description="key y body_template son requeridos")

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if payload.get("health_status"):
        metadata["health_status"] = str(payload.get("health_status")).strip().lower()

    template = NotificationTemplate(
        tenant_id=tenant.id,
        key=key,
        channel="whatsapp",
        body_template=body_template,
        subject_template=payload.get("subject_template"),
        is_active=bool(payload.get("is_active", True)),
        metadata_json=metadata,
    )
    db.session.add(template)
    db.session.flush()
    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_template.created",
            resource_type="notification_template",
            resource_id=str(template.id),
            details={"key": template.key, "channel": "whatsapp"},
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()
    return jsonify({"id": template.id, "created": True}), 201


@whatsapp_rules_bp.route("/api/admin/templates/<template_id>", methods=["PATCH"])
@token_requerido
@require_tenant
def patch_whatsapp_template(user: User, template_id: str):
    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}
    template = NotificationTemplate.query.filter_by(id=template_id, tenant_id=tenant.id, channel="whatsapp").first()
    if not template:
        abort(404, description="template not found")

    if "body_template" in payload:
        template.body_template = str(payload.get("body_template") or "").strip()
    if "subject_template" in payload:
        template.subject_template = payload.get("subject_template")
    if "is_active" in payload:
        template.is_active = bool(payload.get("is_active"))

    meta = template.metadata_json if isinstance(template.metadata_json, dict) else {}
    if "health_status" in payload:
        meta["health_status"] = str(payload.get("health_status") or "").strip().lower()
    template.metadata_json = meta

    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_template.updated",
            resource_type="notification_template",
            resource_id=str(template.id),
            details=payload,
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()
    return jsonify({"updated": True, "id": template.id})


@whatsapp_rules_bp.route("/api/admin/templates/twilio-content/sync", methods=["POST"])
@token_requerido
@require_tenant
def sync_twilio_content_template(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}

    template_id = str(payload.get("template_id") or "").strip()
    if not template_id:
        abort(400, description="template_id es requerido")

    dry_run = payload.get("dry_run", True) is not False
    submit_for_approval = payload.get("submit_for_approval", True) is not False
    force = bool(payload.get("force", False))
    integration_access = integration_access_payload(tenant)
    access_enabled = bool(integration_access.get("enabled"))

    manifest_item = _find_twilio_manifest_item(tenant, template_id)
    if not manifest_item:
        abort(404, description="template_id no existe en el manifiesto Twilio del tenant")

    create_request = manifest_item.get("create_request") if isinstance(manifest_item.get("create_request"), dict) else {}
    approval_request = manifest_item.get("approval_request") if isinstance(manifest_item.get("approval_request"), dict) else {}
    friendly_name = str(create_request.get("friendly_name") or manifest_item.get("friendly_name") or template_id).strip()
    language = str(create_request.get("language") or manifest_item.get("language") or "es").strip()
    category = str(approval_request.get("category") or manifest_item.get("category") or "UTILITY").strip().upper()
    types = create_request.get("types") if isinstance(create_request.get("types"), dict) else {}
    text_type = types.get("twilio/text") if isinstance(types.get("twilio/text"), dict) else {}
    body_preview = str(text_type.get("body") or manifest_item.get("body") or "")[:1000]

    if not friendly_name or not types:
        abort(400, description="El manifiesto de la plantilla no tiene friendly_name o types validos")

    existing = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=friendly_name,
        language=language,
    ).first()
    existing_has_sid = bool(existing and str(existing.content_sid or "").startswith("HX"))
    confirmation_fields = {
        "template_id": template_id,
        "manifest_digest": _confirmation_manifest_digest(create_request, approval_request),
        "submit_for_approval": submit_for_approval,
        "force": force,
    }

    if dry_run:
        ready_to_create = access_enabled
        execute_confirmation = (
            _issue_execution_confirmation(
                purpose="twilio_content_sync",
                tenant=tenant,
                user=user,
                fields=confirmation_fields,
            )
            if access_enabled
            else None
        )
        response_payload = {
            "dry_run": True,
            "template_id": template_id,
            "ready_to_create": ready_to_create,
            "blocked": not access_enabled,
            "locked_reason": None if access_enabled else integration_access.get("lock_reason_code"),
            "integration_access": integration_access,
            "frontend_contract": _twilio_template_frontend_contract(
                integration_access,
                action="create_twilio_content_template",
            ),
            "operator_guardrails": _twilio_operator_guardrails(
                integration_access,
                action="create_twilio_content_template",
                execute_confirmation_issued=bool(execute_confirmation),
            ),
            "would_submit_for_approval": submit_for_approval,
            "existing_registry": _template_registry_payload(existing),
            "create_request": create_request,
            "approval_request": approval_request,
            "content_family": manifest_item.get("content_family"),
            "action_capabilities": manifest_item.get("action_capabilities") or {},
            "meta_business": manifest_item.get("meta_business") or {},
            "quality_gate": manifest_item.get("quality_gate") or {},
        }
        if execute_confirmation:
            response_payload["execute_confirmation"] = execute_confirmation
        return jsonify(
            response_payload
        )

    if not access_enabled:
        return _twilio_access_lock_response(tenant, action="create_twilio_content_template")

    if existing_has_sid and not force:
        return jsonify(
            {
                "created": False,
                "reason": "already_registered",
                "template_id": template_id,
                "registry": _template_registry_payload(existing),
                "meta_business": manifest_item.get("meta_business") or {},
            }
        )

    _verify_execution_confirmation(
        payload.get("execute_confirmation"),
        purpose="twilio_content_sync",
        tenant=tenant,
        user=user,
        fields=confirmation_fields,
    )

    client, credentials = _twilio_client(tenant)
    row, operation_id = _claim_registry_sync(
        existing,
        tenant=tenant,
        friendly_name=friendly_name,
        language=language,
        category=category,
        force=force,
        metadata={
            "template_id": template_id,
            "content_family": manifest_item.get("content_family"),
            "source": "whatsapp_experience_creation_manifest",
        },
    )
    try:
        created = _twilio_create_content(client, create_request)
    except Exception:
        _mark_registry_sync_failure(row, operation_id=operation_id, stage="content_create")
        abort(502, description="Twilio no pudo confirmar la creacion; el registro quedo bloqueado para revision")
    content_sid = str(created.get("sid") or "").strip()
    if not content_sid.startswith("HX"):
        _mark_registry_sync_failure(row, operation_id=operation_id, stage="content_create")
        abort(502, description="Twilio no devolvio un ContentSid valido")

    row.content_sid = content_sid
    row.status = "created"
    interim_metadata = dict(row.metadata_json) if isinstance(row.metadata_json, dict) else {}
    interim_metadata.update({"sync_state": "content_created", "content_sid": content_sid})
    row.metadata_json = interim_metadata
    db.session.add(row)
    db.session.commit()

    approval_status = None
    approval_rejection_reason = None
    if submit_for_approval:
        try:
            approval = _twilio_request_approval(client, content_sid, approval_request)
        except Exception:
            _mark_registry_sync_failure(
                row,
                operation_id=operation_id,
                stage="approval_request",
                content_sid=content_sid,
            )
            abort(502, description="El contenido fue creado, pero Twilio no confirmo el pedido de aprobacion")
        approval_details = _approval_details(approval)
        approval_status = approval_details["status"]
        approval_rejection_reason = approval_details["rejection_reason"]

    row.category = category
    row.status = _sync_status_from_approval(approval_status, submitted=submit_for_approval)
    row.content_sid = content_sid
    row.external_template_id = friendly_name
    row.body_preview = body_preview
    row.components = types
    row.metadata_json = {
        "template_id": template_id,
        "twilio_type": manifest_item.get("twilio_type"),
        "content_family": manifest_item.get("content_family"),
        "action_capabilities": manifest_item.get("action_capabilities") or {},
        "meta_business": manifest_item.get("meta_business") or {},
        "approval_status": approval_status,
        "approval_rejection_reason": approval_rejection_reason,
        "approval_requested": submit_for_approval,
        "sample_values": manifest_item.get("sample_values") or {},
        "send_example": manifest_item.get("send_example") or {},
        "source": "whatsapp_experience_creation_manifest",
        "sync_operation_id": operation_id,
        "sync_state": "complete",
    }
    row.last_sync_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.flush()
    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_template.twilio_content_synced",
            resource_type="message_template_registry",
            resource_id=str(row.id),
            details={
                "template_id": template_id,
                "friendly_name": friendly_name,
                "content_sid": content_sid,
                "approval_requested": submit_for_approval,
                "approval_status": approval_status,
                "twilio_account_scope": credentials.scope,
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "created": True,
            "template_id": template_id,
            "content_sid": content_sid,
            "approval_status": approval_status,
            "registry": _template_registry_payload(row),
            "meta_business": manifest_item.get("meta_business") or {},
        }
    ), 201


@whatsapp_rules_bp.route("/api/admin/whatsapp/flows/twilio-content/sync", methods=["POST"])
@token_requerido
@require_tenant
def sync_twilio_native_flow(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}

    flow_id = str(payload.get("flow_id") or "").strip()
    if not flow_id:
        abort(400, description="flow_id es requerido")
    flow_record = _find_meta_flow_blueprint(tenant, flow_id)
    if not flow_record:
        abort(404, description="flow_id no existe o no tiene blueprint Meta Flow habilitado")

    meta_flow_id = _validated_meta_flow_id(payload.get("meta_flow_id"))
    flow = flow_record["flow"]
    blueprint = flow_record["blueprint"]
    screens = blueprint.get("screens") if isinstance(blueprint.get("screens"), list) else []
    first_screen = screens[0] if screens and isinstance(screens[0], dict) else {}
    first_screen_id = str(first_screen.get("id") or "").strip()
    if not first_screen_id:
        abort(400, description="El blueprint Meta Flow no define una primera pantalla valida")

    flow_name = str(blueprint.get("flow_name") or flow_id).strip().lower()
    friendly_base = re.sub(r"[^a-z0-9_]+", "_", flow_name).strip("_")
    if not friendly_base:
        abort(400, description="El blueprint Meta Flow no define un nombre valido")
    friendly_name = f"{friendly_base}_native_v1"[:255]
    language = str(payload.get("language") or "es").strip()
    if not re.fullmatch(r"[a-z]{2}(?:_[A-Z]{2})?", language):
        abort(400, description="language debe usar formato es o es_AR")
    body, button_text = _flow_content_copy(payload, blueprint)

    dry_run = payload.get("dry_run", True) is not False
    submit_for_approval = payload.get("submit_for_approval", True) is not False
    force = bool(payload.get("force", False))
    integration_access = integration_access_payload(tenant)
    access_enabled = bool(integration_access.get("enabled"))
    create_request = {
        "friendly_name": friendly_name,
        "language": language,
        "variables": {"1": "session-demo-123456"},
        "types": {
            "whatsapp/flows": {
                "body": body,
                "button_text": button_text,
                "flow_id": meta_flow_id,
                "flow_token": "{{1}}",
                "flow_first_page_id": first_screen_id,
                "is_flow_first_page_endpoint": False,
            }
        },
    }
    approval_request = {"name": friendly_name, "category": "UTILITY"}

    existing = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=friendly_name,
        language=language,
    ).first()
    existing_has_sid = bool(existing and str(existing.content_sid or "").startswith("HX"))
    existing_meta_flow_id = str(
        ((existing.metadata_json or {}).get("meta_flow_id") if existing and isinstance(existing.metadata_json, dict) else None)
        or (existing.external_template_id if existing else None)
        or ""
    ).strip()
    meta_flow_conflict = bool(existing_has_sid and existing_meta_flow_id and existing_meta_flow_id != meta_flow_id)
    conflict_blocks_execution = bool(meta_flow_conflict and not force)
    confirmation_fields = {
        "flow_id": flow_id,
        "meta_flow_id": meta_flow_id,
        "manifest_digest": _confirmation_manifest_digest(create_request, approval_request),
        "submit_for_approval": submit_for_approval,
        "force": force,
    }
    execute_confirmation = (
        _issue_execution_confirmation(
            purpose="twilio_native_flow_sync",
            tenant=tenant,
            user=user,
            fields=confirmation_fields,
        )
        if access_enabled and not conflict_blocks_execution
        else None
    )
    flow_contract = {
        "flow_id": flow_id,
        "flow_name": blueprint.get("flow_name"),
        "meta_flow_id": meta_flow_id,
        "first_screen_id": first_screen_id,
        "screens": screens,
        "completion_event": blueprint.get("completion_event"),
        "data_contract": blueprint.get("data_contract") or [],
    }

    if dry_run:
        response_payload = {
            "dry_run": True,
            "flow": flow_contract,
            "ready_to_create": bool(access_enabled and not conflict_blocks_execution),
            "blocked": not access_enabled,
            "locked_reason": None if access_enabled else integration_access.get("lock_reason_code"),
            "conflict": meta_flow_conflict,
            "forced_replacement": bool(force and meta_flow_conflict),
            "integration_access": integration_access,
            "frontend_contract": _twilio_template_frontend_contract(
                integration_access,
                action="create_twilio_native_flow",
            ),
            "operator_guardrails": _twilio_operator_guardrails(
                integration_access,
                action="create_twilio_native_flow",
                execute_confirmation_issued=bool(execute_confirmation),
            ),
            "would_submit_for_approval": submit_for_approval,
            "existing_registry": _template_registry_payload(existing),
            "create_request": create_request,
            "approval_request": approval_request,
            "content_family": "meta_native_flow",
        }
        if execute_confirmation:
            response_payload["execute_confirmation"] = execute_confirmation
        return jsonify(response_payload)

    if not access_enabled:
        return _twilio_access_lock_response(tenant, action="create_twilio_native_flow")
    if meta_flow_conflict and not force:
        abort(409, description="El blueprint ya esta asociado a otro meta_flow_id; usa force solo para un reemplazo auditado")
    if existing_has_sid and not force:
        return jsonify(
            {
                "created": False,
                "reason": "already_registered",
                "flow": flow_contract,
                "registry": _template_registry_payload(existing),
            }
        )

    _verify_execution_confirmation(
        payload.get("execute_confirmation"),
        purpose="twilio_native_flow_sync",
        tenant=tenant,
        user=user,
        fields=confirmation_fields,
    )

    client, credentials = _twilio_client(tenant)
    row, operation_id = _claim_registry_sync(
        existing,
        tenant=tenant,
        friendly_name=friendly_name,
        language=language,
        category="UTILITY",
        force=force,
        metadata={
            "flow_id": flow_id,
            "meta_flow_id": meta_flow_id,
            "content_family": "meta_native_flow",
            "source": "whatsapp_experience_meta_flow_blueprint",
        },
    )
    try:
        created = _twilio_create_content(client, create_request)
    except Exception:
        _mark_registry_sync_failure(row, operation_id=operation_id, stage="content_create")
        abort(502, description="Twilio no pudo confirmar la creacion del Flow; requiere revision antes de reintentar")
    content_sid = str(created.get("sid") or "").strip()
    if not content_sid.startswith("HX"):
        _mark_registry_sync_failure(row, operation_id=operation_id, stage="content_create")
        abort(502, description="Twilio no devolvio un ContentSid valido")

    row.content_sid = content_sid
    row.status = "created"
    interim_metadata = dict(row.metadata_json) if isinstance(row.metadata_json, dict) else {}
    interim_metadata.update({"sync_state": "content_created", "content_sid": content_sid})
    row.metadata_json = interim_metadata
    db.session.add(row)
    db.session.commit()

    approval_status = None
    meta_flow_status = None
    approval_rejection_reason = None
    if submit_for_approval:
        try:
            approval = _twilio_request_approval(client, content_sid, approval_request)
        except Exception:
            _mark_registry_sync_failure(
                row,
                operation_id=operation_id,
                stage="approval_request",
                content_sid=content_sid,
            )
            abort(502, description="El Flow fue creado, pero Twilio no confirmo el pedido de aprobacion")
        approval_details = _approval_details(approval)
        approval_status = approval_details["status"]
        meta_flow_status = approval_details["flow_status"] or approval_status
        approval_rejection_reason = approval_details["rejection_reason"]

    row.category = "UTILITY"
    row.status = _sync_status_from_approval(approval_status, submitted=submit_for_approval)
    row.content_sid = content_sid
    row.external_template_id = meta_flow_id
    row.body_preview = body
    row.components = create_request["types"]
    row.metadata_json = {
        "flow_id": flow_id,
        "flow_name": blueprint.get("flow_name"),
        "meta_flow_id": meta_flow_id,
        "first_screen_id": first_screen_id,
        "screens": screens,
        "completion_event": blueprint.get("completion_event"),
        "data_contract": blueprint.get("data_contract") or [],
        "content_family": "meta_native_flow",
        "approval_status": approval_status,
        "meta_flow_status": meta_flow_status,
        "approval_rejection_reason": approval_rejection_reason,
        "approval_requested": submit_for_approval,
        "source": "whatsapp_experience_meta_flow_blueprint",
        "sync_operation_id": operation_id,
        "sync_state": "complete",
    }
    row.last_sync_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.flush()
    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_flow.twilio_content_synced",
            resource_type="message_template_registry",
            resource_id=str(row.id),
            details={
                "flow_id": flow_id,
                "meta_flow_id": meta_flow_id,
                "friendly_name": friendly_name,
                "content_sid": content_sid,
                "first_screen_id": first_screen_id,
                "approval_requested": submit_for_approval,
                "approval_status": approval_status,
                "forced_replacement": bool(force and existing_has_sid),
                "twilio_account_scope": credentials.scope,
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "created": True,
            "flow": flow_contract,
            "content_sid": content_sid,
            "approval_status": approval_status,
            "registry": _template_registry_payload(row),
        }
    ), 201


@whatsapp_rules_bp.route("/api/admin/templates/twilio-content/refresh", methods=["POST"])
@token_requerido
@require_tenant
def refresh_twilio_content_template(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    integration_access = integration_access_payload(tenant)
    if not integration_access.get("enabled"):
        return _twilio_access_lock_response(tenant, action="refresh_twilio_content_template")

    payload = request.get_json(silent=True) or {}

    template_id = str(payload.get("template_id") or "").strip()
    content_sid = str(payload.get("content_sid") or "").strip()
    row = None

    if template_id:
        manifest_item = _find_twilio_manifest_item(tenant, template_id)
        if not manifest_item:
            abort(404, description="template_id no existe en el manifiesto Twilio del tenant")
        create_request = manifest_item.get("create_request") if isinstance(manifest_item.get("create_request"), dict) else {}
        friendly_name = str(create_request.get("friendly_name") or manifest_item.get("friendly_name") or template_id).strip()
        language = str(create_request.get("language") or manifest_item.get("language") or "es").strip()
        row = MessageTemplateRegistry.query.filter_by(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            name=friendly_name,
            language=language,
        ).first()
        if row and not content_sid:
            content_sid = str(row.content_sid or "").strip()
    elif content_sid:
        row = MessageTemplateRegistry.query.filter_by(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            content_sid=content_sid,
        ).first()

    if not content_sid.startswith("HX"):
        abort(400, description="No hay ContentSid valido para refrescar")
    if not row:
        abort(404, description="ContentSid no registrado para este tenant")

    client, credentials = _twilio_client(tenant)
    try:
        approval = _twilio_fetch_approval(client, content_sid)
    except Exception:
        abort(502, description="Twilio no pudo devolver el estado de aprobacion")
    approval_details = _approval_details(approval)
    approval_status = approval_details["status"]
    row.status = _sync_status_from_approval(approval_status, submitted=True)
    metadata = dict(row.metadata_json) if isinstance(row.metadata_json, dict) else {}
    metadata["approval_status"] = approval_status
    metadata["approval_rejection_reason"] = approval_details["rejection_reason"]
    if metadata.get("content_family") == "meta_native_flow":
        metadata["meta_flow_status"] = approval_details["flow_status"] or approval_status
    metadata["last_refresh_source"] = "twilio_approval_fetch"
    row.metadata_json = metadata
    row.last_sync_at = datetime.now(timezone.utc)
    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_template.twilio_content_refreshed",
            resource_type="message_template_registry",
            resource_id=str(row.id),
            details={
                "template_id": template_id or metadata.get("template_id"),
                "content_sid": content_sid,
                "approval_status": approval_status,
                "registry_status": row.status,
                "twilio_account_scope": credentials.scope,
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "refreshed": True,
            "template_id": template_id or metadata.get("template_id"),
            "content_sid": content_sid,
            "approval_status": approval_status,
            "registry": _template_registry_payload(row),
        }
    )


@whatsapp_rules_bp.route("/api/notifications/whatsapp/test", methods=["POST"])
@token_requerido
@require_tenant
def test_whatsapp_notification_policy(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}
    recipient = payload.get("recipient")
    body = payload.get("body") or ""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metadata["recipient"] = recipient
    svc = WhatsAppEnterpriseRulesService(tenant.id)
    svc.get_or_create()
    allowed, reason = svc.evaluate_outbound(body=body, metadata=metadata)
    return jsonify({"allowed": allowed, "reason": reason})
