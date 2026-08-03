from __future__ import annotations

from flask import Blueprint, abort, g, jsonify, make_response, request
from sqlalchemy import func

from models import AdminAuditLog, Notification, NotificationAttempt, NotificationTemplate, User, db
from routes.auth import _add_cors, token_requerido
from services.notification_orchestrator import (
    NotificationIdempotencyConflict,
    NotificationOrchestrator,
)
from services.professional_message_preview import (
    ProfessionalMessageContractError,
    preview_notification_template,
)
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import _set_anon_cookie, get_or_create_anon_id
from utils.roles import ROLE_EMPLEADO, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role, is_authorized_superadmin_user
from utils.tenant import get_current_tenant, require_tenant

notifications_bp = Blueprint('notifications', __name__)


def _ensure_admin_role(user: User):
    role = canonical_role(getattr(user, "rol", None))
    if role not in {ROLE_TENANT_ADMIN, ROLE_SUPERADMIN, ROLE_EMPLEADO}:
        abort(403, description="Permisos insuficientes")
    if role == ROLE_SUPERADMIN and not is_authorized_superadmin_user(user):
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
    try:
        template = orchestrator.upsert_template(
            key=payload.get("key"),
            channel=payload.get("channel"),
            subject_template=payload.get("subject_template"),
            body_template=payload.get("body_template"),
            quiet_hours_start=payload.get("quiet_hours_start"),
            quiet_hours_end=payload.get("quiet_hours_end"),
            message_template_registry_id=payload.get(
                "message_template_registry_id"
            ),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

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


@notifications_bp.route('/api/admin/notifications/templates', methods=['GET'])
@token_requerido
@require_tenant
def list_notification_templates(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    channel = (request.args.get("channel") or "").strip().lower()
    q = NotificationTemplate.query.filter_by(tenant_id=tenant.id)
    if channel:
        q = q.filter(NotificationTemplate.channel == channel)
    templates = q.order_by(NotificationTemplate.created_at.desc()).all()

    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action="notification_templates_view",
            target_object=str(tenant.id),
            details={"tenant_id": tenant.id, "channel": channel or None, "count": len(templates)},
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        [
            {
                "id": t.id,
                "key": t.key,
                "channel": t.channel,
                "subject_template": t.subject_template,
                "body_template": t.body_template,
                "quiet_hours_start": t.quiet_hours_start,
                "quiet_hours_end": t.quiet_hours_end,
                "message_template_registry_id": t.message_template_registry_id,
                "is_active": t.is_active,
                "metadata": t.metadata_json if isinstance(t.metadata_json, dict) else {},
            }
            for t in templates
        ]
    )


@notifications_bp.route('/api/admin/notifications/templates/preview', methods=['POST'])
@token_requerido
@require_tenant
def preview_notification_template_admin(current_user: User):
    """Render a tenant-owned template without queuing or contacting a provider."""

    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(
        current_user,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    ):
        abort(403, description="Acceso denegado")

    payload = request.get_json(silent=True)
    try:
        if not isinstance(payload, dict):
            raise ProfessionalMessageContractError(
                "request_body_must_be_object",
                field="body",
            )
        preview = preview_notification_template(
            tenant_id=tenant.id,
            template_id=payload.get("template_id"),
            key=payload.get("key"),
            channel=payload.get("channel"),
            context=payload.get("context"),
            content_variables=payload.get("content_variables"),
        )
    except ProfessionalMessageContractError as exc:
        status = 404 if exc.code == "notification_template_not_found" else 400
        response = make_response(jsonify(exc.to_dict()), status)
    else:
        response = make_response(jsonify(preview), 200)

    # Rendered values can contain personal data supplied by an authorized
    # operator. A preview is ephemeral and must not be cached by browsers or
    # intermediary infrastructure.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


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

    raw_max_retries = payload.get("max_retries", 3)
    try:
        notification, created = orchestrator.queue_notification(
            channel=payload.get("channel"),
            recipient=payload.get("recipient"),
            idempotency_key=payload.get("idempotency_key"),
            user_id=payload.get("user_id"),
            template_key=payload.get("template_key"),
            template_context=payload.get("template_context") if isinstance(payload.get("template_context"), dict) else None,
            subject=payload.get("subject"),
            body=payload.get("body"),
            max_retries=int(raw_max_retries),
            template_registry_id=payload.get("template_registry_id"),
            content_variables=payload.get("content_variables"),
            metadata=payload.get("metadata"),
        )
    except NotificationIdempotencyConflict as exc:
        return jsonify({"error": exc.code}), 409
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

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

    return jsonify(
        {
            "id": notification.id,
            "status": notification.status,
            "provider_status": notification.provider_status,
            "created": created,
        }
    ), (201 if created else 200)


@notifications_bp.route('/api/admin/notifications/dispatch', methods=['POST'])
@token_requerido
@require_tenant
def dispatch_notifications_async(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    from services.tasks import dispatch_notifications_task

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


@notifications_bp.route('/api/admin/notifications/requeue-blocked', methods=['POST'])
@token_requerido
@require_tenant
def requeue_blocked_notifications(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    payload = request.get_json(silent=True) or {}
    raw_channels = payload.get("channels") or []
    raw_reasons = payload.get("reason_codes") or []
    if not isinstance(raw_channels, list) or not isinstance(raw_reasons, list):
        return jsonify({"error": "channels and reason_codes must be lists"}), 400
    try:
        limit = max(1, min(int(payload.get("limit") or 100), 500))
        count = NotificationOrchestrator(tenant.id).requeue_blocked_notifications(
            channels={str(value) for value in raw_channels},
            reason_codes={str(value) for value in raw_reasons},
            limit=limit,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action="notification_blocked_requeued",
            target_object=str(tenant.id),
            details={
                "tenant_id": tenant.id,
                "channels": sorted({str(value).strip().lower() for value in raw_channels}),
                "reason_codes": sorted({str(value).strip() for value in raw_reasons}),
                "count": count,
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()
    return jsonify({"requeued": count})


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
                "provider_status": a.provider_status,
                "provider_message_id": a.provider_message_id,
                "error_message": a.error_message,
                "error_digest": a.error_digest,
                "delivery_event_id": a.delivery_event_id,
                "attempted_at": a.attempted_at.isoformat() if a.attempted_at else None,
                "next_retry_at": a.next_retry_at.isoformat() if a.next_retry_at else None,
            }
            for a in attempts
        ]
    )


@notifications_bp.route('/api/admin/notifications/<string:notif_id>', methods=['GET'])
@token_requerido
@require_tenant
def get_notification_detail(current_user: User, notif_id: str):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    notification = Notification.query.filter_by(id=notif_id, tenant_id=tenant.id).first()
    if not notification:
        return jsonify({"error": "notification not found"}), 404

    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action="notification_detail_view",
            target_object=notification.id,
            details={"tenant_id": tenant.id, "channel": notification.channel},
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "id": notification.id,
            "tenant_id": notification.tenant_id,
            "user_id": notification.user_id,
            "template_id": notification.template_id,
            "channel": notification.channel,
            "recipient": notification.recipient,
            "subject": notification.subject,
            "body": notification.body,
            "status": notification.status,
            "provider_status": notification.provider_status,
            "provider_message_id": notification.provider_message_id,
            "message_template_registry_id": notification.message_template_registry_id,
            "provider_connection_id": notification.provider_connection_id,
            "provider_sender_id": notification.provider_sender_id,
            "idempotency_key": notification.idempotency_key,
            "max_retries": notification.max_retries,
            "attempt_count": notification.attempt_count,
            "next_retry_at": notification.next_retry_at.isoformat() if notification.next_retry_at else None,
            "sent_at": notification.sent_at.isoformat() if notification.sent_at else None,
            "last_error": notification.last_error,
            "leased_until": notification.leased_until.isoformat() if notification.leased_until else None,
            "metadata": notification.metadata_json if isinstance(notification.metadata_json, dict) else {},
            "created_at": notification.created_at.isoformat() if notification.created_at else None,
            "updated_at": notification.updated_at.isoformat() if notification.updated_at else None,
        }
    )


@notifications_bp.route('/api/admin/notifications/metrics', methods=['GET'])
@token_requerido
@require_tenant
def notification_metrics(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    period_days = int(request.args.get("period_days", 7) or 7)
    period_days = max(1, min(period_days, 90))
    from datetime import timedelta
    from utils.time_utils import get_local_now

    since = get_local_now() - timedelta(days=period_days)

    rows = (
        db.session.query(Notification.channel, Notification.status, func.count(Notification.id))
        .filter(Notification.tenant_id == tenant.id)
        .filter(Notification.created_at >= since)
        .group_by(Notification.channel, Notification.status)
        .all()
    )
    by_channel = {}
    total_sent = 0
    total_failed = 0
    total_blocked = 0
    total_uncertain = 0
    for channel, status, count in rows:
        c = str(channel)
        s = str(status)
        n = int(count or 0)
        by_channel.setdefault(
            c,
            {
                "sent": 0,
                "failed": 0,
                "blocked": 0,
                "queued": 0,
                "delayed": 0,
                "sending": 0,
                "retry_wait": 0,
                "send_uncertain": 0,
            },
        )
        if s in by_channel[c]:
            by_channel[c][s] += n
        if s == "sent":
            total_sent += n
        elif s == "failed":
            total_failed += n
        elif s == "blocked":
            total_blocked += n
        elif s == "send_uncertain":
            total_uncertain += n

    provider_rows = (
        db.session.query(Notification.provider_status, func.count(Notification.id))
        .filter(Notification.tenant_id == tenant.id)
        .filter(Notification.created_at >= since)
        .group_by(Notification.provider_status)
        .all()
    )
    by_provider_status = {
        str(status or "unknown"): int(count or 0)
        for status, count in provider_rows
    }
    denominator = total_sent + total_failed + total_blocked + total_uncertain
    success_rate = round((total_sent / denominator) * 100, 2) if denominator > 0 else 100.0

    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action="notification_metrics_view",
            target_object=str(tenant.id),
            details={"tenant_id": tenant.id, "period_days": period_days},
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "tenant_id": tenant.id,
            "period_days": period_days,
            "since": since.isoformat() if since else None,
            "totals": {
                "sent": total_sent,
                "failed": total_failed,
                "blocked": total_blocked,
                "send_uncertain": total_uncertain,
                "success_rate": success_rate,
            },
            "by_channel": by_channel,
            "by_provider_status": by_provider_status,
        }
    )


@notifications_bp.route('/api/admin/notifications/alerts', methods=['GET'])
@token_requerido
@require_tenant
def notification_alerts(current_user: User):
    tenant = g.tenant_profile
    _ensure_admin_role(current_user)
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")

    period_days = int(request.args.get("period_days", 7) or 7)
    period_days = max(1, min(period_days, 90))
    threshold_pct = float(request.args.get("threshold_pct", 5) or 5)
    min_volume = int(request.args.get("min_volume", 5) or 5)

    from datetime import timedelta
    from utils.time_utils import get_local_now

    since = get_local_now() - timedelta(days=period_days)
    rows = (
        db.session.query(Notification.channel, Notification.status, func.count(Notification.id))
        .filter(Notification.tenant_id == tenant.id)
        .filter(Notification.created_at >= since)
        .group_by(Notification.channel, Notification.status)
        .all()
    )

    by_channel = {}
    for channel, status, count in rows:
        c = str(channel)
        s = str(status)
        n = int(count or 0)
        by_channel.setdefault(
            c,
            {"sent": 0, "failed": 0, "blocked": 0, "send_uncertain": 0},
        )
        if s in {"sent", "failed", "blocked", "send_uncertain"}:
            by_channel[c][s] += n

    alerts = []
    for channel, data in by_channel.items():
        volume = (
            data["sent"]
            + data["failed"]
            + data["blocked"]
            + data["send_uncertain"]
        )
        if volume < min_volume:
            continue
        failure_rate = (
            round(
                (
                    (
                        data["failed"]
                        + data["blocked"]
                        + data["send_uncertain"]
                    )
                    / volume
                )
                * 100,
                2,
            )
            if volume > 0
            else 0.0
        )
        if failure_rate >= threshold_pct:
            alerts.append(
                {
                    "channel": channel,
                    "volume": volume,
                    "sent": data["sent"],
                    "failed": data["failed"],
                    "blocked": data["blocked"],
                    "send_uncertain": data["send_uncertain"],
                    "failure_rate": failure_rate,
                    "threshold_pct": threshold_pct,
                }
            )

    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action="notification_alerts_view",
            target_object=str(tenant.id),
            details={
                "tenant_id": tenant.id,
                "period_days": period_days,
                "threshold_pct": threshold_pct,
                "min_volume": min_volume,
                "alerts_count": len(alerts),
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "tenant_id": tenant.id,
            "period_days": period_days,
            "threshold_pct": threshold_pct,
            "min_volume": min_volume,
            "alerts": alerts,
        }
    )
