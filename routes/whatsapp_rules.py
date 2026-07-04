from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, g, jsonify, request
from twilio.rest import Client

from models import AuditEvent, MessageTemplateRegistry, NotificationTemplate, User, db
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService
from services.whatsapp_experience import build_whatsapp_experience
from services.plan_access import integration_access_payload, integration_frontend_contract
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import token_requerido
from utils.tenant import require_tenant

whatsapp_rules_bp = Blueprint("whatsapp_rules_bp", __name__)


def _guard(user: User, tenant):
    if getattr(user, "rol", None) not in {"admin", "super_admin"}:
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


def _sync_status_from_approval(status: str | None, *, submitted: bool) -> str:
    value = str(status or "").strip().lower()
    if value == "approved":
        return "approved"
    if value in {"pending", "received", "in_review"} or submitted:
        return "pending_approval"
    if value == "rejected":
        return "rejected"
    return "created"


def _approval_status_from_content(content) -> str | None:
    approval_requests = getattr(content, "approval_requests", None)
    if approval_requests is None:
        approval_requests = getattr(content, "approvalRequests", None)
    if isinstance(approval_requests, dict):
        value = approval_requests.get("status") or approval_requests.get("approval_status")
    else:
        value = getattr(approval_requests, "status", None)
    return str(value or "").strip() or None


def _twilio_credentials() -> tuple[str | None, str | None]:
    account_sid = current_app.config.get("TWILIO_ACCOUNT_SID")
    auth_token = current_app.config.get("TWILIO_AUTH_TOKEN")
    return account_sid, auth_token


def _twilio_content_confirmation_token(template_id: str) -> str:
    return f"sync_twilio_content:{template_id}"


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
    return {
        "contract_version": "twilio.content.operator_guardrails.v1",
        "action": action,
        "dry_run_preview_allowed": True,
        "show_manifest_payload": True,
        "execute_confirmation_issued": bool(enabled and execute_confirmation_issued),
        "twilio_call_allowed": enabled,
        "requires_full_plan": not enabled,
        "blocked_actions": [] if enabled else ["create_twilio_content_template", "refresh_twilio_content_template"],
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

    if dry_run:
        ready_to_create = access_enabled
        execute_confirmation = _twilio_content_confirmation_token(template_id) if access_enabled else None
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

    expected_confirmation = _twilio_content_confirmation_token(template_id)
    if payload.get("execute_confirmation") != expected_confirmation:
        abort(409, description="Confirmacion requerida para crear o enviar plantilla real a Twilio")

    account_sid, auth_token = _twilio_credentials()
    if not account_sid or not auth_token:
        abort(503, description="Faltan TWILIO_ACCOUNT_SID o TWILIO_AUTH_TOKEN en el backend")

    client = Client(account_sid, auth_token)
    created = client.content.v1.contents.create(**create_request)
    content_sid = str(getattr(created, "sid", "") or "").strip()
    if not content_sid.startswith("HX"):
        abort(502, description="Twilio no devolvio un ContentSid valido")

    approval_status = None
    if submit_for_approval:
        approval = client.content.v1.contents(content_sid).approval_requests.create(**approval_request)
        approval_status = str(getattr(approval, "status", "") or "").strip() or None

    row = existing or MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=friendly_name,
        language=language,
    )
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
        "approval_requested": submit_for_approval,
        "sample_values": manifest_item.get("sample_values") or {},
        "send_example": manifest_item.get("send_example") or {},
        "source": "whatsapp_experience_creation_manifest",
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

    account_sid, auth_token = _twilio_credentials()
    if not account_sid or not auth_token:
        abort(503, description="Faltan TWILIO_ACCOUNT_SID o TWILIO_AUTH_TOKEN en el backend")

    client = Client(account_sid, auth_token)
    content = client.content.v1.contents(content_sid).fetch()
    approval_status = _approval_status_from_content(content)
    row.status = _sync_status_from_approval(approval_status, submitted=True)
    metadata = dict(row.metadata_json) if isinstance(row.metadata_json, dict) else {}
    metadata["approval_status"] = approval_status
    metadata["last_refresh_source"] = "twilio_content_fetch"
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
