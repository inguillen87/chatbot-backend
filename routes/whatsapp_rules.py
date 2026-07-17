from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from flask import Blueprint, abort, current_app, g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from twilio.rest import Client

from models import (
    AuditEvent,
    MessageTemplateRegistry,
    MessagingEventLedger,
    NotificationTemplate,
    ProviderConnection,
    ProviderSender,
    User,
    WhatsAppFlowInteraction,
    db,
)
from services.provider_platform import is_sender_ready_status
from services.meta_flow_json import canonical_flow_json
from services.meta_flow_management import (
    MetaFlowGraphClient,
    MetaFlowManagementError,
    build_meta_flow_publication_readiness,
    resolve_meta_graph_credentials,
)
from services.meta_flow_data_exchange import MetaFlowActionError
from services.meta_flow_runtime import (
    ORDER_FLOW_ID,
    SURVEY_FLOW_ID,
    authorize_order_context,
    authorize_survey_context,
)
from services.whatsapp_enterprise_rules import (
    WhatsAppEnterpriseRulesService,
    whatsapp_flow_rate_limit_reservation_key,
)
from services.whatsapp_experience import build_whatsapp_experience
from services.whatsapp_flow_security import (
    WhatsAppFlowTokenError,
    issue_whatsapp_flow_token,
    normalize_flow_recipient,
    whatsapp_flow_recipient_scope,
    whatsapp_flow_token_key_ready,
)
from services.plan_access import integration_access_payload, integration_frontend_contract
from services.twilio_tech_provider import TwilioRuntimeCredentials, resolve_twilio_runtime_credentials
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import token_requerido
from utils.roles import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role
from utils.tenant import require_tenant

whatsapp_rules_bp = Blueprint("whatsapp_rules_bp", __name__)
TWILIO_CONTENT_API_URL = "https://content.twilio.com/v1/Content"
TWILIO_CONFIRMATION_TTL_SECONDS = 15 * 60
META_FLOW_PUBLICATION_ATTESTATION_TTL_SECONDS = 60 * 60


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


def _strict_boolean(payload: dict, field: str, *, default: bool) -> bool:
    value = payload.get(field, default)
    if not isinstance(value, bool):
        abort(400, description=f"{field} debe ser booleano")
    return value


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


def _find_meta_flow_artifact(tenant, flow_id: str) -> dict | None:
    payload = build_whatsapp_experience(tenant, app_config=current_app.config)
    flows = ((payload.get("webview_blueprint") or {}).get("flows") or [])
    for flow in flows:
        if str(flow.get("id") or "").lower() != flow_id.lower():
            continue
        blueprint = flow.get("meta_flow_blueprint")
        artifact = flow.get("meta_flow_artifact")
        validation = artifact.get("validation") if isinstance(artifact, dict) else {}
        if (
            not isinstance(blueprint, dict)
            or not isinstance(artifact, dict)
            or not artifact.get("publishable_flow_json")
            or not isinstance(validation, dict)
            or not validation.get("valid")
            or not artifact.get("content_sha256")
        ):
            return None
        return {"flow": flow, "blueprint": blueprint, "artifact": artifact}
    return None


def _native_flow_registry_identity(
    blueprint: dict,
    flow_id: str,
    language_value,
) -> tuple[str, str]:
    flow_name = str(blueprint.get("flow_name") or flow_id).strip().lower()
    friendly_base = re.sub(r"[^a-z0-9_]+", "_", flow_name).strip("_")
    if not friendly_base:
        abort(400, description="El blueprint Meta Flow no define un nombre valido")
    language = str(language_value or "es").strip()
    if not re.fullmatch(r"[a-z]{2}(?:_[A-Z]{2})?", language):
        abort(400, description="language debe usar formato es o es_AR")
    return f"{friendly_base}_native_v1"[:255], language


@whatsapp_rules_bp.route(
    "/api/admin/whatsapp/flows/meta/readiness",
    methods=["GET"],
)
@token_requerido
@require_tenant
def get_meta_native_flow_readiness(user: User):
    """Discover tenant-scoped Meta Flow plans without provider calls."""

    tenant = g.tenant_profile
    _guard(user, tenant)
    requested_flow_id = str(request.args.get("flow_id") or "").strip().lower()
    experience = build_whatsapp_experience(tenant, app_config=current_app.config)
    native_flows = ((experience.get("meta_platform") or {}).get("native_flows") or {})
    candidates = native_flows.get("flows") if isinstance(native_flows, dict) else []
    discovered: list[dict] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        flow_id = str(candidate.get("id") or "").strip()
        if requested_flow_id and flow_id.lower() != requested_flow_id:
            continue
        readiness = candidate.get("publication_readiness")
        if not isinstance(readiness, dict):
            continue
        discovered.append(
            {
                "id": flow_id,
                "flow_name": candidate.get("flow_name"),
                "category": candidate.get("category"),
                "first_screen_id": candidate.get("first_screen_id"),
                "screen_ids": candidate.get("screen_ids") or [],
                "data_contract": candidate.get("data_contract") or [],
                "flow_json_download_url": (
                    f"/api/admin/whatsapp/flows/{flow_id}/flow-json"
                ),
                "readiness": readiness,
            }
        )

    if requested_flow_id and not discovered:
        abort(
            404,
            description=(
                "flow_id no tiene un artefacto Meta Flow publicable para este tenant"
            ),
        )

    response = jsonify(
        {
            "contract_version": "whatsapp.meta_flow_readiness_catalog.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tenant": {"id": tenant.id, "slug": tenant.slug},
            "dry_run": True,
            "meta_graph_calls_performed": False,
            "twilio_calls_performed": False,
            "provider_writes_performed": False,
            "messages_sent": False,
            "claim_evidence_discoverable": any(
                item.get("id") == "claim_evidence" for item in discovered
            ),
            "summary": {
                "total": len(discovered),
                "ready": sum(
                    1 for item in discovered if item["readiness"].get("ready")
                ),
                "blocked": sum(
                    1 for item in discovered if item["readiness"].get("blocked")
                ),
                "verification_ready": sum(
                    1
                    for item in discovered
                    if item["readiness"].get("status") == "verification_ready"
                ),
            },
            "flows": discovered,
            "security": {
                "tenant_scoped": True,
                "provider_secrets_exposed": False,
                "execute_confirmation_issued": False,
            },
            "frontend_contract": {
                "render_as": "meta_flow_publication_readiness",
                "show_blockers": True,
                "show_artifact_identity": True,
                "show_idempotency_state": True,
                "allow_dry_run_from_payload": True,
                "allow_publish_from_catalog": False,
            },
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@whatsapp_rules_bp.route("/api/admin/whatsapp/flows/<flow_id>/flow-json", methods=["GET"])
@token_requerido
@require_tenant
def download_meta_flow_json(user: User, flow_id: str):
    """Return the exact validated artifact that an operator can upload to Meta."""

    tenant = g.tenant_profile
    _guard(user, tenant)
    flow_record = _find_meta_flow_artifact(tenant, flow_id)
    if not flow_record:
        abort(404, description="flow_id no tiene un artefacto Flow JSON validado y publicable")

    artifact = flow_record["artifact"]
    document = artifact.get("document")
    if not isinstance(document, dict):
        abort(409, description="El artefacto Flow JSON no contiene un documento valido")

    canonical = canonical_flow_json(document)
    content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if content_sha256 != str(artifact.get("content_sha256") or "").strip():
        abort(409, description="La identidad del artefacto Flow JSON no coincide")

    safe_flow_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", flow_id).strip("-.") or "whatsapp-flow"
    response = current_app.response_class(canonical, mimetype="application/json")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{safe_flow_id}.flow.json"'
    )
    response.headers["ETag"] = f'"sha256-{content_sha256}"'
    response.headers["X-Flow-JSON-Version"] = str(artifact.get("flow_json_version") or "")
    response.headers["X-Flow-Data-API-Version"] = str(artifact.get("data_api_version") or "")
    response.headers["X-Flow-Content-SHA256"] = content_sha256
    return response


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


def _twilio_client_for_sender(tenant, sender: ProviderSender) -> tuple[Client, TwilioRuntimeCredentials]:
    credentials = resolve_twilio_runtime_credentials(
        tenant=tenant,
        provider_connection=sender.provider_connection,
        app_config=current_app.config,
    )
    if not credentials.ready:
        if credentials.scope == "conflict":
            abort(409, description="La cuenta Twilio del sender no coincide con el tenant")
        abort(503, description="Las credenciales Twilio del sender no estan configuradas")
    return Client(credentials.account_sid, credentials.auth_token), credentials


def _flow_interaction_payload(row: WhatsAppFlowInteraction) -> dict:
    retry_mode = "none"
    if row.status == "send_uncertain":
        retry_mode = "reconcile_provider_status"
    elif row.status == "failed":
        retry_mode = "new_invocation_after_review"
    return {
        "id": row.id,
        "flow_id": row.flow_id,
        "meta_flow_id": row.meta_flow_id,
        "content_sid": row.content_sid,
        "recipient_hint": row.recipient_hint,
        "status": row.status,
        "external_message_sid": row.external_message_sid,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "consumed_at": row.consumed_at.isoformat() if row.consumed_at else None,
        # An idempotency key is immutable and this endpoint never retries an
        # existing invocation. Keep the flag aligned with executable behavior.
        "retry_safe": False,
        "retry_mode": retry_mode,
        "reconciliation_required": row.status == "send_uncertain",
    }


def _native_flow_registry_for_send(tenant_id: int, flow_id: str) -> MessageTemplateRegistry | None:
    rows = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant_id,
        provider="twilio",
        channel="whatsapp",
    ).all()
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if metadata.get("content_family") != "meta_native_flow":
            continue
        if str(metadata.get("flow_id") or "").strip().lower() == flow_id.lower():
            return row
    return None


def _ready_flow_sender(tenant_id: int, sender_id) -> ProviderSender | None:
    query = ProviderSender.query.filter_by(tenant_id=tenant_id, channel="whatsapp")
    if sender_id not in (None, ""):
        try:
            query = query.filter_by(id=int(sender_id))
        except (TypeError, ValueError):
            abort(400, description="provider_sender_id invalido")
    for sender in query.order_by(ProviderSender.updated_at.desc()).all():
        if is_sender_ready_status(sender.status):
            return sender
    return None


def _meta_flow_management_sender(tenant_id: int) -> ProviderSender | None:
    senders = (
        ProviderSender.query.filter_by(tenant_id=tenant_id, channel="whatsapp")
        .order_by(ProviderSender.updated_at.desc())
        .all()
    )
    for sender in senders:
        if str(sender.waba_id or "").strip():
            return sender
    return None


def _flow_registry_is_active(row: MessageTemplateRegistry | None) -> bool:
    if not row or not str(row.content_sid or "").startswith("HX"):
        return False
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    approval_status = str(
        metadata.get("meta_flow_status") or metadata.get("approval_status") or ""
    ).strip().lower()
    return bool(
        str(row.status or "").strip().lower() in {"approved", "active"}
        and approval_status in {"approved", "active", "published"}
        and str(metadata.get("meta_flow_id") or row.external_template_id or "").strip()
    )


def _flow_status_callback(sender: ProviderSender) -> str | None:
    configured = str(sender.status_callback_url or "").strip()
    if configured.startswith("https://"):
        return configured
    base_url = str(
        current_app.config.get("PUBLIC_API_BASE_URL")
        or current_app.config.get("BACKEND_URL")
        or current_app.config.get("APP_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if base_url.startswith("https://"):
        return f"{base_url}/twilio/whatsapp/status"
    return None


def _flow_interaction_status_callback(callback_url: str, interaction_id: int) -> str:
    """Bind a signed Twilio callback to an invocation without exposing PII."""

    parsed = urlsplit(callback_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["flow_interaction_id"] = str(int(interaction_id))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


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


def _verify_meta_publication_attestation(
    token,
    *,
    tenant,
    user: User,
    flow_id: str,
    meta_flow_id: str,
    waba_id: str,
    flow_json_sha256: str,
) -> bool:
    if not token:
        return False
    try:
        decoded = _confirmation_serializer().loads(
            str(token),
            max_age=META_FLOW_PUBLICATION_ATTESTATION_TTL_SECONDS,
        )
    except SignatureExpired:
        abort(409, description="La verificacion de Meta vencio; verifica nuevamente el Flow")
    except BadSignature:
        abort(409, description="La verificacion de Meta no es valida")
    expected = {
        "purpose": "meta_flow_publication_attestation",
        "tenant_id": tenant.id,
        "actor_user_id": user.id,
        "flow_id": flow_id,
        "meta_flow_id": meta_flow_id,
        "waba_id": waba_id,
        "flow_json_sha256": flow_json_sha256,
        "status": "PUBLISHED",
    }
    if decoded != expected:
        abort(409, description="La verificacion de Meta no corresponde a este tenant o artefacto")
    return True


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


def _claim_meta_flow_publication(
    existing: MessageTemplateRegistry | None,
    *,
    tenant,
    friendly_name: str,
    language: str,
    flow_id: str,
    meta_flow_id: str | None,
    flow_json_sha256: str,
    waba_id: str,
    operation_fingerprint: str,
) -> tuple[MessageTemplateRegistry, str, str]:
    row = (
        MessageTemplateRegistry.query.filter_by(id=existing.id)
        .with_for_update()
        .one()
        if existing
        else MessageTemplateRegistry(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            name=friendly_name,
            language=language,
        )
    )
    metadata = (
        dict(row.metadata_json)
        if isinstance(row.metadata_json, dict)
        else {}
    )
    current_state = str(metadata.get("meta_sync_state") or "").strip().lower()
    if current_state in {"publishing", "uncertain"}:
        abort(
            409,
            description=(
                "La publicacion anterior requiere reconciliacion antes de reintentar"
            ),
        )

    previous_status = str(row.status or "draft")
    operation_id = uuid4().hex
    metadata.update(
        {
            "flow_id": flow_id,
            "meta_flow_id": meta_flow_id,
            "flow_json_sha256": flow_json_sha256,
            "meta_flow_waba_id": waba_id or None,
            "content_family": "meta_native_flow",
            "externally_managed_meta_flow": True,
            "source": "meta_flow_json_7_3_artifact",
            "meta_sync_operation_id": operation_id,
            "meta_sync_operation_fingerprint": operation_fingerprint,
            "meta_sync_state": "publishing",
            "meta_sync_started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    row.category = row.category or "UTILITY"
    if not str(row.content_sid or "").startswith("HX"):
        row.status = "meta_syncing"
    row.metadata_json = metadata
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(
            409,
            description=(
                "Otra publicacion creo este registro; actualiza antes de reintentar"
            ),
        )
    return row, operation_id, previous_status


def _mark_meta_flow_publication_failure(
    row: MessageTemplateRegistry,
    *,
    operation_id: str,
    previous_status: str,
    error: MetaFlowManagementError,
) -> str:
    metadata = dict(row.metadata_json) if isinstance(row.metadata_json, dict) else {}
    if metadata.get("meta_sync_operation_id") != operation_id:
        return ""
    candidate_meta_flow_id = str(
        error.details.get("meta_flow_id")
        or metadata.get("meta_flow_id")
        or row.external_template_id
        or ""
    ).strip()
    uncertain = not candidate_meta_flow_id and error.code in {
        "meta_graph_unreachable",
        "meta_graph_invalid_json",
        "meta_graph_invalid_contract",
    }
    metadata.update(
        {
            "meta_flow_id": candidate_meta_flow_id or None,
            "meta_sync_state": "uncertain" if uncertain else "failed",
            "meta_sync_error_code": error.code,
            "meta_sync_failed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    if candidate_meta_flow_id:
        row.external_template_id = candidate_meta_flow_id
    if not str(row.content_sid or "").startswith("HX"):
        row.status = "meta_sync_uncertain" if uncertain else "meta_sync_failed"
    else:
        row.status = previous_status
    row.metadata_json = metadata
    row.last_sync_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    return candidate_meta_flow_id


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

    dry_run = _strict_boolean(payload, "dry_run", default=True)
    submit_for_approval = _strict_boolean(
        payload,
        "submit_for_approval",
        default=True,
    )
    force = _strict_boolean(payload, "force", default=False)
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


@whatsapp_rules_bp.route("/api/admin/whatsapp/flows/meta/sync", methods=["POST"])
@token_requerido
@require_tenant
def sync_meta_native_flow(user: User):
    """Create, upload, publish and verify the exact Flow JSON through Meta Graph API."""

    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}

    flow_id = str(payload.get("flow_id") or "").strip()
    if not flow_id:
        abort(400, description="flow_id es requerido")
    flow_record = _find_meta_flow_artifact(tenant, flow_id)
    if not flow_record:
        abort(422, description="flow_id no tiene un artefacto Flow JSON validado y publicable")

    supplied_meta_flow_id = str(payload.get("meta_flow_id") or "").strip()
    meta_flow_id = (
        _validated_meta_flow_id(supplied_meta_flow_id)
        if supplied_meta_flow_id
        else None
    )
    dry_run = _strict_boolean(payload, "dry_run", default=True)
    publish = _strict_boolean(payload, "publish", default=True)
    if not publish:
        abort(400, description="Este endpoint operativo requiere publish=true")

    flow = flow_record["flow"]
    blueprint = flow_record["blueprint"]
    artifact = flow_record["artifact"]
    document = artifact.get("document")
    if not isinstance(document, dict):
        abort(409, description="El artefacto Flow JSON no contiene un documento valido")
    artifact_sha256 = str(artifact.get("content_sha256") or "").strip()
    if not artifact_sha256:
        abort(409, description="El artefacto Flow JSON no tiene identidad verificable")
    friendly_name, language = _native_flow_registry_identity(
        blueprint,
        flow_id,
        payload.get("language"),
    )
    flow_name = str(blueprint.get("flow_name") or flow_id).strip()

    experience = build_whatsapp_experience(tenant, app_config=current_app.config)
    native_flows = ((experience.get("meta_platform") or {}).get("native_flows") or {})
    data_exchange = (
        native_flows.get("data_exchange")
        if isinstance(native_flows.get("data_exchange"), dict)
        else {}
    )
    endpoint_driven = bool(artifact.get("endpoint_driven"))
    endpoint_uri = (
        str(data_exchange.get("endpoint_url") or "").strip()
        if endpoint_driven
        else ""
    )
    sender = _meta_flow_management_sender(tenant.id)
    waba_id = str(sender.waba_id or "").strip() if sender else ""
    credentials = resolve_meta_graph_credentials(
        waba_id=waba_id,
        app_config=current_app.config,
    )
    integration_access = integration_access_payload(tenant)
    existing_registry = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=friendly_name,
        language=language,
    ).first()
    if existing_registry is None:
        existing_registry = _native_flow_registry_for_send(tenant.id, flow_id)
    existing_metadata = (
        existing_registry.metadata_json
        if existing_registry and isinstance(existing_registry.metadata_json, dict)
        else {}
    )
    registered_meta_flow_id = str(
        existing_metadata.get("meta_flow_id")
        or (existing_registry.external_template_id if existing_registry else None)
        or ""
    ).strip()
    if not meta_flow_id and registered_meta_flow_id:
        meta_flow_id = _validated_meta_flow_id(registered_meta_flow_id)
    registered_artifact_sha256 = str(
        existing_metadata.get("flow_json_sha256") or ""
    ).strip()
    registered_waba_id = str(
        existing_metadata.get("meta_flow_waba_id") or ""
    ).strip()
    registered_publication_verified = bool(
        existing_metadata.get("meta_flow_publication_verified")
    )
    meta_sync_state = str(
        existing_metadata.get("meta_sync_state") or ""
    ).strip().lower()
    readiness = build_meta_flow_publication_readiness(
        tenant_id=tenant.id,
        flow_id=flow_id,
        flow_name=flow_name,
        blueprint_category=blueprint.get("category"),
        artifact=artifact,
        credentials=credentials,
        integration_access=integration_access,
        data_exchange=data_exchange,
        registry={
            "configured": bool(existing_registry),
            "id": existing_registry.id if existing_registry else None,
            "tenant_id": existing_registry.tenant_id if existing_registry else None,
            "status": existing_registry.status if existing_registry else None,
            "meta_flow_id": registered_meta_flow_id,
            "flow_json_sha256": registered_artifact_sha256,
            "waba_id": registered_waba_id,
            "publication_verified": registered_publication_verified,
            "sync_state": meta_sync_state,
            "last_sync_at": (
                existing_registry.last_sync_at.isoformat()
                if existing_registry and existing_registry.last_sync_at
                else None
            ),
        },
        supplied_meta_flow_id=supplied_meta_flow_id,
        language=language,
    )
    blockers = list(readiness["blockers"])
    operation = readiness["operation"]
    clone_published_flow_id = operation.get("clone_flow_id")
    operation_fingerprint = readiness["idempotency"]["operation_fingerprint"]
    category = readiness["category"]
    confirmation_fields = {
        "flow_id": flow_id,
        "meta_flow_id": meta_flow_id,
        "waba_id": waba_id,
        "flow_json_sha256": artifact_sha256,
        "endpoint_uri": endpoint_uri or None,
        "clone_flow_id": clone_published_flow_id,
        "operation_fingerprint": operation_fingerprint,
        "publish": True,
    }
    execute_confirmation = (
        _issue_execution_confirmation(
            purpose="meta_native_flow_sync",
            tenant=tenant,
            user=user,
            fields=confirmation_fields,
        )
        if not blockers
        else None
    )
    readiness["dry_run"]["execute_confirmation_issued"] = bool(
        execute_confirmation
    )
    readiness["security"]["execute_confirmation_exposed"] = bool(
        execute_confirmation
    )
    management_contract = {
        "contract_version": "whatsapp.meta_flow_management.v1",
        "official_api": "Meta Graph API",
        "flow_json_version": artifact.get("flow_json_version"),
        "data_api_version": artifact.get("data_api_version"),
        "flow_json_sha256": artifact_sha256,
        "flow_json_byte_size": artifact.get("byte_size"),
        "endpoint_driven": endpoint_driven,
        "endpoint_uri": endpoint_uri or None,
        "waba_id_present": bool(waba_id),
        "graph": credentials.public_payload(),
        "irreversible_publish": operation.get("irreversible_publish"),
        "replacement_mode": operation.get("replacement_mode"),
        "operation_mode": operation.get("mode"),
        "clone_flow_id": clone_published_flow_id,
        "steps": operation.get("steps") or [],
        "idempotency": readiness.get("idempotency"),
    }

    if dry_run:
        response = {
            "dry_run": True,
            "ready_to_sync": not blockers,
            "blocked": bool(blockers),
            "blockers": blockers,
            "flow_id": flow_id,
            "meta_flow_id": meta_flow_id,
            "flow_name": flow_name,
            "category": category,
            "clone_flow_id": clone_published_flow_id,
            "management": management_contract,
            "readiness": readiness,
            "integration_access": integration_access,
        }
        if execute_confirmation:
            response["execute_confirmation"] = execute_confirmation
        return jsonify(response)

    if blockers:
        return jsonify(
            {
                "ok": False,
                "blocked": True,
                "blockers": blockers,
                "management": management_contract,
                "readiness": readiness,
                "integration_access": integration_access,
            }
        ), 409

    _verify_execution_confirmation(
        payload.get("execute_confirmation"),
        purpose="meta_native_flow_sync",
        tenant=tenant,
        user=user,
        fields=confirmation_fields,
    )

    publication_row, operation_id, previous_status = _claim_meta_flow_publication(
        existing_registry,
        tenant=tenant,
        friendly_name=friendly_name,
        language=language,
        flow_id=flow_id,
        meta_flow_id=meta_flow_id,
        flow_json_sha256=artifact_sha256,
        waba_id=waba_id,
        operation_fingerprint=operation_fingerprint,
    )
    try:
        result = MetaFlowGraphClient(credentials).provision_and_publish(
            flow_name=flow_name,
            category=category,
            document=document,
            expected_sha256=artifact_sha256,
            endpoint_uri=endpoint_uri or None,
            meta_flow_id=meta_flow_id,
            publish=True,
            clone_published_on_change=bool(clone_published_flow_id),
            verification_only=operation.get("mode") == "verify_published",
        )
    except MetaFlowManagementError as exc:
        failed_meta_flow_id = _mark_meta_flow_publication_failure(
            publication_row,
            operation_id=operation_id,
            previous_status=previous_status,
            error=exc,
        )
        db.session.add(
            AuditEvent(
                tenant_id=tenant.id,
                actor_user_id=user.id,
                event_type="whatsapp_flow.meta_sync_failed",
                resource_type="meta_whatsapp_flow",
                resource_id=failed_meta_flow_id or meta_flow_id or flow_id,
                details={
                    "flow_id": flow_id,
                    "meta_flow_id": failed_meta_flow_id or meta_flow_id,
                    "waba_id": waba_id,
                    "flow_json_sha256": artifact_sha256,
                    "operation_fingerprint": operation_fingerprint,
                    "operation_mode": operation.get("mode"),
                    "error_code": exc.code,
                },
                ip_address=request.remote_addr,
            )
        )
        db.session.commit()
        return jsonify({"error": exc.public_payload()}), exc.status_code

    verification = result["verification"]
    verified_meta_flow_id = str(verification.get("meta_flow_id") or "").strip()
    publication_attestation = _issue_execution_confirmation(
        purpose="meta_flow_publication_attestation",
        tenant=tenant,
        user=user,
        fields={
            "flow_id": flow_id,
            "meta_flow_id": verified_meta_flow_id,
            "waba_id": waba_id,
            "flow_json_sha256": artifact_sha256,
            "status": "PUBLISHED",
        },
    )

    updated_metadata = (
        dict(publication_row.metadata_json)
        if isinstance(publication_row.metadata_json, dict)
        else {}
    )
    updated_metadata.update(
        {
            "meta_flow_id": verified_meta_flow_id,
            "meta_flow_status": "published",
            "meta_flow_publication_verified": True,
            "meta_flow_publication_verified_at": datetime.now(timezone.utc).isoformat(),
            "meta_flow_publication_source": "meta_graph_api",
            "meta_flow_remote_sha256": verification.get("flow_json_sha256"),
            "flow_json_sha256": artifact_sha256,
            "meta_flow_waba_id": waba_id,
            "meta_sync_operation_id": operation_id,
            "meta_sync_operation_fingerprint": operation_fingerprint,
            "meta_sync_state": "complete",
            "meta_sync_completed_at": datetime.now(timezone.utc).isoformat(),
            "meta_flow_cloned_from_id": (
                result.get("cloned_from_flow_id")
                or existing_metadata.get("meta_flow_cloned_from_id")
            ),
        }
    )
    publication_row.external_template_id = verified_meta_flow_id
    publication_row.metadata_json = updated_metadata
    publication_row.last_sync_at = datetime.now(timezone.utc)
    if not str(publication_row.content_sid or "").startswith("HX"):
        publication_row.status = "meta_published"
    else:
        publication_row.status = previous_status
    db.session.add(publication_row)

    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_flow.meta_published_verified",
            resource_type="meta_whatsapp_flow",
            resource_id=verified_meta_flow_id,
            details={
                "flow_id": flow_id,
                "meta_flow_id": verified_meta_flow_id,
                "waba_id": waba_id,
                "flow_json_sha256": artifact_sha256,
                "operation_fingerprint": operation_fingerprint,
                "operation_mode": operation.get("mode"),
                "created": result["created"],
                "uploaded": result["uploaded"],
                "published_now": result["published_now"],
                "idempotent": result["idempotent"],
                "cloned_from_flow_id": result.get("cloned_from_flow_id"),
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "synced": True,
            "flow_id": flow_id,
            "meta_flow_id": verified_meta_flow_id,
            "management": management_contract,
            "readiness": readiness,
            "result": result,
            "meta_publication_attestation": publication_attestation,
            "next_action": (
                "replace_twilio_wrapper"
                if (
                    result.get("cloned_from_flow_id")
                    and str(publication_row.content_sid or "").startswith("HX")
                )
                else (
                    "verify_twilio_wrapper"
                    if str(publication_row.content_sid or "").startswith("HX")
                    else "create_twilio_wrapper"
                )
            ),
        }
    )


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
    flow_record = _find_meta_flow_artifact(tenant, flow_id)
    if not flow_record:
        abort(422, description="flow_id no tiene un artefacto Flow JSON validado y publicable")

    meta_flow_id = _validated_meta_flow_id(payload.get("meta_flow_id"))
    flow = flow_record["flow"]
    blueprint = flow_record["blueprint"]
    artifact = flow_record["artifact"]
    artifact_document = artifact.get("document") if isinstance(artifact.get("document"), dict) else {}
    screens = artifact_document.get("screens") if isinstance(artifact_document.get("screens"), list) else []
    first_screen = screens[0] if screens and isinstance(screens[0], dict) else {}
    first_screen_id = str(artifact.get("first_screen_id") or first_screen.get("id") or "").strip()
    if not first_screen_id:
        abort(400, description="El artefacto Flow JSON no define una primera pantalla valida")

    friendly_name, language = _native_flow_registry_identity(
        blueprint,
        flow_id,
        payload.get("language"),
    )
    body, button_text = _flow_content_copy(payload, blueprint)

    dry_run = _strict_boolean(payload, "dry_run", default=True)
    submit_for_approval = _strict_boolean(
        payload,
        "submit_for_approval",
        default=True,
    )
    force = _strict_boolean(payload, "force", default=False)
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
                "is_flow_first_page_endpoint": bool(artifact.get("endpoint_driven")),
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
    existing_metadata = existing.metadata_json if existing and isinstance(existing.metadata_json, dict) else {}
    existing_artifact_sha256 = str(existing_metadata.get("flow_json_sha256") or "").strip()
    expected_artifact_sha256 = str(artifact.get("content_sha256") or "").strip()
    management_sender = _meta_flow_management_sender(tenant.id)
    management_waba_id = (
        str(management_sender.waba_id or "").strip() if management_sender else ""
    )
    publication_attestation = payload.get("meta_publication_attestation")
    publication_attested = _verify_meta_publication_attestation(
        publication_attestation,
        tenant=tenant,
        user=user,
        flow_id=flow_id,
        meta_flow_id=meta_flow_id,
        waba_id=management_waba_id,
        flow_json_sha256=expected_artifact_sha256,
    )
    existing_publication_waba_id = str(
        existing_metadata.get("meta_flow_waba_id") or ""
    ).strip()
    existing_publication_verified = bool(
        existing_metadata.get("meta_flow_publication_verified")
        and existing_meta_flow_id == meta_flow_id
        and existing_artifact_sha256 == expected_artifact_sha256
        and existing_publication_waba_id
        and existing_publication_waba_id == management_waba_id
    )
    publication_verified = bool(
        publication_attested or existing_publication_verified
    )
    meta_flow_conflict = bool(existing_has_sid and existing_meta_flow_id and existing_meta_flow_id != meta_flow_id)
    artifact_conflict = bool(
        existing_has_sid
        and (
            not existing_artifact_sha256
            or existing_artifact_sha256 != expected_artifact_sha256
        )
    )
    registry_conflict = bool(meta_flow_conflict or artifact_conflict)
    conflict_blocks_execution = bool(registry_conflict and not force)
    confirmation_fields = {
        "flow_id": flow_id,
        "meta_flow_id": meta_flow_id,
        "flow_json_sha256": expected_artifact_sha256,
        "manifest_digest": _confirmation_manifest_digest(create_request, approval_request),
        "submit_for_approval": submit_for_approval,
        "force": force,
        "meta_publication_verified": publication_verified,
        "waba_id": management_waba_id or None,
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
        "screen_ids": artifact.get("screen_ids") or [],
        "flow_json_version": artifact.get("flow_json_version"),
        "data_api_version": artifact.get("data_api_version"),
        "content_sha256": artifact.get("content_sha256"),
        "byte_size": artifact.get("byte_size"),
        "endpoint_driven": bool(artifact.get("endpoint_driven")),
        "completion_event": blueprint.get("completion_event"),
        "data_contract": blueprint.get("data_contract") or [],
        "meta_flow_json_upload_performed": False,
        "publication_scope": "twilio_content_wrapper_only",
        "meta_flow_publication_verified": publication_verified,
    }

    if dry_run:
        response_payload = {
            "dry_run": True,
            "flow": flow_contract,
            "ready_to_create": bool(access_enabled and not conflict_blocks_execution),
            "blocked": not access_enabled,
            "locked_reason": None if access_enabled else integration_access.get("lock_reason_code"),
            "conflict": registry_conflict,
            "meta_flow_id_conflict": meta_flow_conflict,
            "artifact_conflict": artifact_conflict,
            "forced_replacement": bool(force and registry_conflict),
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
    if registry_conflict and not force:
        abort(409, description="El registro no coincide con el meta_flow_id o el hash Flow JSON validado; usa force solo para un reemplazo auditado")
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
            "flow_json_sha256": expected_artifact_sha256,
            "flow_json_version": artifact.get("flow_json_version"),
            "data_api_version": artifact.get("data_api_version"),
            "content_family": "meta_native_flow",
            "source": "meta_flow_json_7_3_artifact",
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
    publication_verified_at = (
        datetime.now(timezone.utc).isoformat()
        if publication_attested
        else existing_metadata.get("meta_flow_publication_verified_at")
        if publication_verified
        else None
    )
    publication_source = (
        "meta_graph_api_attestation"
        if publication_attested
        else existing_metadata.get("meta_flow_publication_source")
        if publication_verified
        else None
    )
    row.metadata_json = {
        "flow_id": flow_id,
        "flow_name": blueprint.get("flow_name"),
        "meta_flow_id": meta_flow_id,
        "first_screen_id": first_screen_id,
        "screen_ids": artifact.get("screen_ids") or [],
        "flow_json_sha256": expected_artifact_sha256,
        "flow_json_version": artifact.get("flow_json_version"),
        "data_api_version": artifact.get("data_api_version"),
        "flow_json_byte_size": artifact.get("byte_size"),
        "meta_flow_json_upload_performed": False,
        "meta_flow_publication_verified": publication_verified,
        "meta_flow_publication_verified_at": publication_verified_at,
        "meta_flow_publication_source": publication_source,
        "meta_flow_waba_id": management_waba_id or None,
        "externally_managed_meta_flow": True,
        "completion_event": blueprint.get("completion_event"),
        "data_contract": blueprint.get("data_contract") or [],
        "content_family": "meta_native_flow",
        "approval_status": approval_status,
        "meta_flow_status": "published" if publication_verified else meta_flow_status,
        "approval_rejection_reason": approval_rejection_reason,
        "approval_requested": submit_for_approval,
        "source": "meta_flow_json_7_3_artifact",
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
                "forced_replacement": bool(force and registry_conflict),
                "flow_json_sha256": expected_artifact_sha256,
                "meta_flow_json_upload_performed": False,
                "meta_flow_publication_verified": publication_verified,
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


@whatsapp_rules_bp.route("/api/admin/whatsapp/flows/send", methods=["POST"])
@token_requerido
@require_tenant
def send_twilio_native_flow(user: User):
    """Preview or send one approved native Flow with a one-time signed token."""

    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}
    flow_id = str(payload.get("flow_id") or "").strip()
    if not flow_id:
        abort(400, description="flow_id es requerido")
    try:
        recipient = normalize_flow_recipient(payload.get("recipient"))
    except WhatsAppFlowTokenError:
        abort(400, description="recipient debe ser un telefono E.164 valido")
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:\-]{8,120}", idempotency_key):
        abort(400, description="idempotency_key debe tener entre 8 y 120 caracteres seguros")

    dry_run_value = payload.get("dry_run", True)
    if not isinstance(dry_run_value, bool):
        abort(400, description="dry_run debe ser booleano")
    dry_run = dry_run_value
    integration_access = integration_access_payload(tenant)
    access_enabled = bool(integration_access.get("enabled"))
    token_secret = current_app.config.get("WHATSAPP_FLOW_TOKEN_KEY_V1")
    token_key_ready = whatsapp_flow_token_key_ready(token_secret)
    token_ttl = current_app.config.get("WHATSAPP_FLOW_TOKEN_TTL_SECONDS")
    registry = _native_flow_registry_for_send(tenant.id, flow_id)
    registry_active = _flow_registry_is_active(registry)
    sender = _ready_flow_sender(tenant.id, payload.get("provider_sender_id"))
    sender_ready = bool(sender)
    registry_metadata = (
        registry.metadata_json
        if registry and isinstance(registry.metadata_json, dict)
        else {}
    )
    experience = build_whatsapp_experience(tenant, app_config=current_app.config)
    native_flows = ((experience.get("meta_platform") or {}).get("native_flows") or {})
    flow_runtime = next(
        (
            item
            for item in native_flows.get("flows") or []
            if isinstance(item, dict)
            and str(item.get("id") or "").strip().lower() == flow_id.lower()
        ),
        None,
    )
    data_exchange = native_flows.get("data_exchange") if isinstance(native_flows.get("data_exchange"), dict) else {}
    meta_flow_id = str(
        registry_metadata.get("meta_flow_id")
        or (registry.external_template_id if registry else None)
        or ""
    ).strip()

    credentials = None
    if sender:
        credentials = resolve_twilio_runtime_credentials(
            tenant=tenant,
            provider_connection=sender.provider_connection,
            app_config=current_app.config,
        )
    credentials_ready = bool(credentials and credentials.ready)

    order_context = None
    order_context_error: MetaFlowActionError | None = None
    if flow_id == ORDER_FLOW_ID:
        try:
            order_context = authorize_order_context(
                tenant.id,
                payload.get("order_context"),
            )
        except MetaFlowActionError as exc:
            order_context_error = exc

    survey_context = None
    survey_context_error: MetaFlowActionError | None = None
    if flow_id == SURVEY_FLOW_ID:
        try:
            survey_context = authorize_survey_context(
                tenant.id,
                payload.get("survey_context"),
            )
        except MetaFlowActionError as exc:
            survey_context_error = exc

    recipient_scope = None
    if token_key_ready:
        recipient_scope = whatsapp_flow_recipient_scope(
            secret=token_secret,
            tenant_id=tenant.id,
            recipient=recipient,
        )
    recipient_hint = (
        recipient_scope["recipient_hint"] if recipient_scope else f"***{recipient[-4:]}"
    )

    existing = WhatsAppFlowInteraction.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key=idempotency_key,
    ).first()
    if existing and recipient_scope:
        existing_metadata = (
            existing.metadata_json if isinstance(existing.metadata_json, dict) else {}
        )
        same_scope = bool(
            existing.flow_id == flow_id
            and existing.recipient_hash == recipient_scope["recipient_hash"]
            and (not sender or existing.provider_sender_id == sender.id)
            and (
                flow_id != ORDER_FLOW_ID
                or existing_metadata.get("order_context") == order_context
            )
            and (
                flow_id != SURVEY_FLOW_ID
                or existing_metadata.get("survey_context") == survey_context
            )
        )
        if not same_scope:
            abort(409, description="idempotency_key ya pertenece a otra operacion")

    policy_allowed = False
    policy_reason = None
    if registry:
        policy_allowed, policy_reason = WhatsAppEnterpriseRulesService(
            tenant.id
        ).evaluate_outbound(
            body=str(registry.body_preview or ""),
            metadata={"recipient": recipient, "is_template": True},
        )

    blockers: list[str] = []
    if not access_enabled:
        blockers.append(str(integration_access.get("lock_reason_code") or "plan_full_required"))
    if not token_key_ready:
        blockers.append("flow_token_key_not_configured")
    if not registry:
        blockers.append("flow_not_registered")
    elif not registry_active:
        blockers.append("flow_not_approved_or_published")
    if not flow_runtime:
        blockers.append("flow_json_artifact_not_compiled")
    else:
        if not flow_runtime.get("artifact_identity_verified"):
            blockers.append("flow_json_artifact_not_verified")
        if not flow_runtime.get("meta_flow_publication_verified"):
            blockers.append("meta_flow_publication_not_verified")
    if not data_exchange.get("ready"):
        blockers.append("data_exchange_not_ready")
    if order_context_error:
        blockers.append(order_context_error.code)
    if survey_context_error:
        blockers.append(survey_context_error.code)
    if not sender_ready:
        blockers.append("sender_not_ready")
    elif not credentials_ready:
        blockers.append("sender_credentials_not_ready")
    if registry and not policy_allowed:
        blockers.append(policy_reason or "enterprise_policy_blocked")
    if existing:
        blockers.append(f"idempotency_{existing.status}")

    confirmation_fields = None
    execute_confirmation = None
    if not blockers and registry and sender and recipient_scope:
        confirmation_fields = {
            "flow_id": flow_id,
            "registry_id": registry.id,
            "provider_sender_id": sender.id,
            "recipient_hash": recipient_scope["recipient_hash"],
            "idempotency_key": idempotency_key,
            "manifest_digest": _confirmation_manifest_digest(
                {
                    "flow_id": flow_id,
                    "meta_flow_id": meta_flow_id,
                    "content_sid": registry.content_sid,
                    "flow_json_sha256": registry_metadata.get("flow_json_sha256"),
                    "data_contract": registry_metadata.get("data_contract") or [],
                    "order_context": order_context,
                    "survey_context": survey_context,
                }
            ),
        }
        execute_confirmation = _issue_execution_confirmation(
            purpose="twilio_native_flow_send",
            tenant=tenant,
            user=user,
            fields=confirmation_fields,
        )

    if dry_run:
        response = {
            "dry_run": True,
            "ready_to_send": not blockers,
            "blocked": bool(blockers),
            "blockers": blockers,
            "flow_id": flow_id,
            "meta_flow_id": meta_flow_id or None,
            "content_sid": registry.content_sid if registry else None,
            "recipient_hint": recipient_hint,
            "provider_sender_id": sender.id if sender else None,
            "idempotency_key": idempotency_key,
            "order_context": {
                "required": flow_id == ORDER_FLOW_ID,
                "ready": bool(order_context),
                "kind": order_context.get("kind") if order_context else None,
                "id": order_context.get("id") if order_context else None,
            },
            "survey_context": {
                "required": flow_id == SURVEY_FLOW_ID,
                "ready": bool(survey_context),
                "id": survey_context.get("id") if survey_context else None,
                "slug": survey_context.get("slug") if survey_context else None,
            },
            "token_ttl_seconds": int(token_ttl or 48 * 60 * 60),
            "security": {
                "dedicated_key_ready": token_key_ready,
                "one_time_token": True,
                "tenant_bound": True,
                "recipient_bound": True,
                "token_exposed": False,
                "flow_json_artifact_verified": bool(
                    flow_runtime and flow_runtime.get("artifact_identity_verified")
                ),
                "meta_publication_verified": bool(
                    flow_runtime and flow_runtime.get("meta_flow_publication_verified")
                ),
                "data_exchange_ready": bool(data_exchange.get("ready")),
            },
            "integration_access": integration_access,
            "existing_interaction": _flow_interaction_payload(existing) if existing else None,
        }
        if execute_confirmation:
            response["execute_confirmation"] = execute_confirmation
        return jsonify(response)

    if existing:
        status_code = 200 if existing.status in {"sent", "consumed"} else 409
        return jsonify(
            {
                "sent": existing.status in {"sent", "consumed"},
                "idempotent_replay": True,
                "interaction": _flow_interaction_payload(existing),
            }
        ), status_code
    if not access_enabled:
        return _twilio_access_lock_response(tenant, action="send_twilio_native_flow")
    if not token_key_ready:
        abort(503, description="WHATSAPP_FLOW_TOKEN_KEY_V1 no esta configurada de forma segura")
    if not registry or not registry_active:
        abort(409, description="El Flow debe estar registrado, aprobado y publicado")
    if not flow_runtime or not flow_runtime.get("artifact_identity_verified"):
        abort(409, description="El registro no coincide con el artefacto Flow JSON validado")
    if not flow_runtime.get("meta_flow_publication_verified"):
        abort(409, description="La publicacion del Flow en Meta no fue verificada")
    if not data_exchange.get("ready"):
        abort(503, description="El endpoint Data Exchange del WABA no esta listo")
    if order_context_error:
        abort(
            order_context_error.status_code,
            description="El pedido asociado no existe o no pertenece a este tenant",
        )
    if survey_context_error:
        abort(
            survey_context_error.status_code,
            description=(
                "La encuesta no es compatible, no esta publicada o no pertenece a este tenant"
            ),
        )
    if not sender or not credentials_ready:
        abort(409, description="No hay un sender WhatsApp operativo con credenciales validas")
    if not policy_allowed:
        abort(429 if policy_reason == "rate_limited" else 403, description=policy_reason or "Envio bloqueado")
    if not confirmation_fields:
        abort(409, description="La operacion debe validarse nuevamente")
    _verify_execution_confirmation(
        payload.get("execute_confirmation"),
        purpose="twilio_native_flow_send",
        tenant=tenant,
        user=user,
        fields=confirmation_fields,
    )

    issued = issue_whatsapp_flow_token(
        secret=token_secret,
        tenant_id=tenant.id,
        recipient=recipient,
        flow_id=flow_id,
        meta_flow_id=meta_flow_id,
        provider_sender_id=sender.id,
        ttl_seconds=token_ttl,
    )
    reservation_allowed, reservation_reason = WhatsAppEnterpriseRulesService(
        tenant.id
    ).reserve_outbound(
        body=str(registry.body_preview or ""),
        reservation_key=whatsapp_flow_rate_limit_reservation_key(idempotency_key),
        metadata={"recipient": recipient, "is_template": True},
        provider="twilio",
        provider_connection_id=sender.provider_connection_id,
        provider_sender_id=sender.id,
        source="native_flow",
    )
    if not reservation_allowed:
        abort(
            429 if reservation_reason == "rate_limited" else 403,
            description=reservation_reason or "Envio bloqueado",
        )
    interaction = WhatsAppFlowInteraction(
        tenant_id=tenant.id,
        template_registry_id=registry.id,
        provider_sender_id=sender.id,
        flow_id=flow_id,
        meta_flow_id=meta_flow_id,
        content_sid=registry.content_sid,
        recipient_hash=issued.recipient_hash,
        recipient_hint=issued.recipient_hint,
        token_digest=issued.token_digest,
        idempotency_key=idempotency_key,
        status="claimed",
        data_contract=registry_metadata.get("data_contract") or [],
        metadata_json={
            "source": "admin_whatsapp_operations",
            "actor_user_id": user.id,
            "credential_scope": credentials.scope,
            **({"order_context": order_context} if order_context else {}),
            **({"survey_context": survey_context} if survey_context else {}),
        },
        expires_at=issued.expires_at,
    )
    db.session.add(interaction)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raced = WhatsAppFlowInteraction.query.filter_by(
            tenant_id=tenant.id,
            idempotency_key=idempotency_key,
        ).first()
        if raced:
            return jsonify(
                {
                    "sent": raced.status in {"sent", "consumed"},
                    "idempotent_replay": True,
                    "interaction": _flow_interaction_payload(raced),
                }
            ), 200 if raced.status in {"sent", "consumed"} else 409
        abort(409, description="No se pudo reservar una invocacion unica")

    message_params = {
        "to": f"whatsapp:{recipient}",
        "content_sid": registry.content_sid,
        "content_variables": json.dumps({"1": issued.token}, separators=(",", ":")),
    }
    if sender.messaging_service_sid:
        message_params["messaging_service_sid"] = sender.messaging_service_sid
    else:
        sender_address = str(sender.sender_id or sender.phone_number or "").strip()
        if not sender_address:
            interaction.status = "failed"
            interaction.error_code = "sender_address_missing"
            db.session.add(interaction)
            db.session.commit()
            abort(409, description="El sender no tiene direccion WhatsApp utilizable")
        message_params["from_"] = (
            sender_address
            if sender_address.startswith("whatsapp:")
            else f"whatsapp:{sender_address}"
        )
    status_callback = _flow_status_callback(sender)
    if status_callback:
        message_params["status_callback"] = _flow_interaction_status_callback(
            status_callback,
            interaction.id,
        )

    client = Client(credentials.account_sid, credentials.auth_token)
    try:
        message = client.messages.create(**message_params)
        message_sid = str(getattr(message, "sid", "") or "").strip()
        if not message_sid:
            raise RuntimeError("twilio_message_sid_missing")
    except Exception as exc:
        now = datetime.now(timezone.utc)
        error_code = type(exc).__name__[:80]
        # A callback may win the race while the provider request times out.
        # Only a still-unclaimed invocation can transition to uncertain; a
        # callback-bound sent/failed/consumed row is never regressed here.
        WhatsAppFlowInteraction.query.filter(
            WhatsAppFlowInteraction.id == interaction.id,
            WhatsAppFlowInteraction.tenant_id == tenant.id,
            WhatsAppFlowInteraction.status == "claimed",
            WhatsAppFlowInteraction.external_message_sid.is_(None),
        ).update(
            {
                WhatsAppFlowInteraction.status: "send_uncertain",
                WhatsAppFlowInteraction.error_code: error_code,
                WhatsAppFlowInteraction.updated_at: now,
            },
            synchronize_session=False,
        )
        db.session.add(
            AuditEvent(
                tenant_id=tenant.id,
                actor_user_id=user.id,
                event_type="whatsapp_flow.send_uncertain",
                resource_type="whatsapp_flow_interaction",
                resource_id=str(interaction.id),
                details={
                    "flow_id": flow_id,
                    "content_sid": registry.content_sid,
                    "provider_sender_id": sender.id,
                    "recipient_hint": issued.recipient_hint,
                    "error_type": error_code,
                },
                ip_address=request.remote_addr,
            )
        )
        db.session.commit()
        abort(502, description="Twilio no confirmo el envio; no reintentes con otra clave hasta revisar el estado")

    # A signed callback can commit before the synchronous Twilio response
    # returns. Refresh first and never overwrite a terminal callback outcome.
    db.session.refresh(interaction)
    if not interaction.external_message_sid:
        interaction.external_message_sid = message_sid[:180]
    if interaction.status not in {"failed", "consumed"}:
        interaction.status = "sent"
        interaction.error_code = None
    interaction.updated_at = datetime.now(timezone.utc)
    db.session.add(interaction)
    db.session.add(
        MessagingEventLedger(
            tenant_id=tenant.id,
            provider_connection_id=sender.provider_connection_id,
            provider_sender_id=sender.id,
            channel="whatsapp",
            direction="outbound",
            event_type="native_flow_sent",
            provider="twilio",
            provider_event_id=message_sid[:180],
            external_message_sid=message_sid[:180],
            external_status="queued",
            sender=str(sender.sender_id or sender.phone_number or "")[:255] or None,
            recipient=issued.recipient_hint,
            payload={"flow_id": flow_id, "content_sid": registry.content_sid},
            metadata_json={"interaction_id": interaction.id, "idempotency_key": idempotency_key},
            request_id=idempotency_key,
        )
    )
    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_flow.sent",
            resource_type="whatsapp_flow_interaction",
            resource_id=str(interaction.id),
            details={
                "flow_id": flow_id,
                "meta_flow_id": meta_flow_id,
                "content_sid": registry.content_sid,
                "provider_sender_id": sender.id,
                "recipient_hint": issued.recipient_hint,
                "external_message_sid": message_sid[:180],
                "token_exposed": False,
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "sent": True,
            "idempotent_replay": False,
            "interaction": _flow_interaction_payload(interaction),
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
    allowed, reason = svc.evaluate_outbound(
        body=body,
        metadata=metadata,
        lock_rate_limit=False,
    )
    return jsonify({"allowed": allowed, "reason": reason})
