from __future__ import annotations

from flask import Blueprint, abort, current_app, g, jsonify, make_response, request

from models import AdminAuditLog, Notification, NotificationAttempt, NotificationTemplate, User, db
from routes.auth import _add_cors, token_requerido
from services.notification_orchestrator import NotificationOrchestrator
from services.tasks import dispatch_notifications_task
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import _set_anon_cookie, get_or_create_anon_id
from utils.tenant import get_current_tenant, require_tenant

notifications_bp = Blueprint('notifications', __name__)


def _ensure_admin_role(user: User):
    if getattr(user, "rol", None) not in {"admin", "super_admin", "empleado"}:
        abort(403, description="Permisos insuficientes")


@notifications_bp.route('/notifications', methods=['OPTIONS'])
def notifications_options():
    anon_id = get_or_create_anon_id()
    resp = make_response("", 200)
    resp = _add_cors(
        resp,
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=(
            "Authorization, Content-Type, Origin, Accept, "
            "X-Entity-Token, X-Chat-Session-Id, X-Anon-Id, Anon-Id, "
            "x-anon-id, anon-id, X-Tenant, x-tenant, X-Tenant-Id, x-tenant-id"
        ),
    )
    resp.headers.setdefault("X-Anon-Id", anon_id)
    resp.headers.setdefault("Anon-Id", anon_id)
    return _set_anon_cookie(resp, anon_id)


@notifications_bp.route('/notifications', methods=['GET'])
@token_requerido
@require_tenant
def get_notifications(current_user: User):
    tenant = g.tenant_profile
    get_current_tenant()
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    items = (
        Notification.query.filter_by(tenant_id=tenant.id, user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify(
        [
            {
                "id": item.id,
                "channel": item.channel,
                "subject": item.subject,
                "body": item.body,
                "status": item.status,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "sent_at": item.sent_at.isoformat() if item.sent_at else None,
            }
            for item in items
        ]
    )


@notifications_bp.route('/api/admin/notifications/templates', methods=['POST'])
@token_requerido
@require_tenant
def create_notification_template(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    payload = request.get_json(silent=True) or {}
    orchestrator = NotificationOrchestrator(tenant.id)
    template = orchestrator.upsert_template(
        key=payload.get("key"),
        channel=payload.get("channel"),
        subject_template=payload.get("subject_template"),
        body_template=payload.get("body_template"),
        quiet_hours_start=payload.get("quiet_hours_start"),
        quiet_hours_end=payload.get("quiet_hours_end"),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )

    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action="notification_template_upsert",
            target_object=template.id,
            details={"tenant_id": tenant.id, "key": template.key, "channel": template.channel},
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify({"id": template.id, "key": template.key, "channel": template.channel, "is_active": template.is_active}), 201


@notifications_bp.route('/api/admin/notifications', methods=['POST'])
@token_requerido
@require_tenant
def create_notification(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    payload = request.get_json(silent=True) or {}
    orchestrator = NotificationOrchestrator(tenant.id)

    notification, created = orchestrator.queue_notification(
        channel=payload.get("channel"),
        recipient=payload.get("recipient"),
        idempotency_key=payload.get("idempotency_key"),
        user_id=payload.get("user_id"),
        template_key=payload.get("template_key"),
        template_context=payload.get("template_context") if isinstance(payload.get("template_context"), dict) else None,
        subject=payload.get("subject"),
        body=payload.get("body"),
        max_retries=int(payload.get("max_retries") or 3),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )

    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action="notification_queued",
            target_object=notification.id,
            details={"tenant_id": tenant.id, "created": created, "channel": notification.channel},
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify({"id": notification.id, "status": notification.status, "created": created}), (201 if created else 200)


@notifications_bp.route('/api/admin/notifications/dispatch', methods=['POST'])
@token_requerido
@require_tenant
def dispatch_notifications_async(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    task = dispatch_notifications_task.delay(tenant.id)
    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action="notification_dispatch_enqueued",
            target_object=str(tenant.id),
            details={"tenant_id": tenant.id, "task_id": str(task.id)},
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify({"queued": True, "task_id": str(task.id)})


@notifications_bp.route('/api/workers/notifications/dispatch', methods=['POST'])
@token_requerido
@require_tenant
def dispatch_notifications_worker(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    payload = request.get_json(silent=True) or {}
    orchestrator = NotificationOrchestrator(tenant.id)
    result = orchestrator.dispatch_due_notifications(limit=int(payload.get("limit") or 50))
    db.session.commit()
    return jsonify(result)


@notifications_bp.route('/api/admin/notifications/<string:notif_id>/attempts', methods=['GET'])
@token_requerido
@require_tenant
def list_notification_attempts(current_user: User, notif_id: str):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    notification = Notification.query.filter_by(id=notif_id, tenant_id=tenant.id).first()
    if not notification:
        return jsonify({"error": "notification not found"}), 404

    attempts = (
        NotificationAttempt.query.filter_by(notification_id=notif_id, tenant_id=tenant.id)
        .order_by(NotificationAttempt.attempt_number.asc())
        .all()
    )
    return jsonify(
        [
            {
                "attempt_number": a.attempt_number,
                "status": a.status,
                "provider": a.provider,
                "error_message": a.error_message,
                "attempted_at": a.attempted_at.isoformat() if a.attempted_at else None,
                "next_retry_at": a.next_retry_at.isoformat() if a.next_retry_at else None,
            }
            for a in attempts
        ]
    )
