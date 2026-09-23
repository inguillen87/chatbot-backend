from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text

from extensions import db
from models import (
    ConsentLedger,
    MessageTemplateRegistry,
    MessagingEventLedger,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
)
from services.provider_connection_cutover_contract import (
    MANAGED_CONNECTION_CONTRACT_VERSION,
    MANAGED_CONNECTION_MARKER,
    advisory_lock_keys,
)


CONTRACT_VERSION = "provider.platform_status.v1"
READY_SENDER_STATUSES = frozenset({"online", "approved", "connected", "active"})


class ManagedProviderConnectionStateError(RuntimeError):
    """A legacy sync attempted to weaken a cutover-managed connection."""

    def __init__(self, code: str):
        self.code = str(code or "managed_provider_connection_state_invalid")
        super().__init__(self.code)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _is_sha256(value: Any) -> bool:
    cleaned = _clean(value).lower()
    return len(cleaned) == 64 and all(ch in "0123456789abcdef" for ch in cleaned)


def _is_deployment_revision(value: Any) -> bool:
    cleaned = _clean(value).lower()
    return 7 <= len(cleaned) <= 64 and all(
        ch in "0123456789abcdef" for ch in cleaned
    )


def is_sender_ready_status(value: Any) -> bool:
    return _clean(value).lower() in READY_SENDER_STATUSES


def _bool_config(config: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _backend_base_url(config: Mapping[str, Any]) -> str:
    return (
        _clean(config.get("PUBLIC_API_BASE_URL"))
        or _clean(config.get("BACKEND_URL"))
        or "https://www.chatboc.ar"
    ).rstrip("/")


def _normalize_phone(value: Any) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    if raw.startswith("whatsapp:"):
        raw = raw.replace("whatsapp:", "", 1)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    return raw if raw.startswith("+") else f"+{digits}"


def _sender_id_from_phone(phone: str | None) -> str | None:
    return f"whatsapp:{phone}" if phone else None


def _twilio_state(tenant: TenantProfile) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    state = cfg.get("twilio_tech_provider")
    return dict(state) if isinstance(state, dict) else {}


def _connection_status(state: Mapping[str, Any]) -> str:
    sender_status = _clean(state.get("sender_status")).lower()
    status = _clean(state.get("status")).lower()
    if is_sender_ready_status(sender_status):
        return "online"
    if status in {"pending_sender_registration", "subaccount_created", "provisioning_plan_ready"}:
        return status
    if status:
        return status
    return "needs_setup"


def _sender_status(state: Mapping[str, Any]) -> str:
    sender_status = _clean(state.get("sender_status")).lower()
    if sender_status:
        return sender_status
    if state.get("sender_sid"):
        return "registered"
    if state.get("waba_id") or state.get("phone_number_id"):
        return "pending_registration"
    if state.get("requested_phone_number"):
        return "draft"
    return "missing"


def _status_callback_urls(config: Mapping[str, Any]) -> dict[str, str]:
    base_url = _backend_base_url(config)
    return {
        "webhook_url": f"{base_url}/webhook/whatsapp",
        "status_callback_url": f"{base_url}/twilio/whatsapp/status",
    }


def _get_or_create_connection(tenant: TenantProfile, config: Mapping[str, Any]) -> ProviderConnection:
    live_enabled = _bool_config(config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED")
    tenant_connections = ProviderConnection.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
    ).all()
    managed_connections = [
        candidate
        for candidate in tenant_connections
        if isinstance(candidate.config, dict)
        and isinstance(candidate.config.get(MANAGED_CONNECTION_MARKER), dict)
        and candidate.config[MANAGED_CONNECTION_MARKER].get("enabled") is True
    ]
    if managed_connections:
        if len(managed_connections) != 1:
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_ambiguous"
            )
        connection = managed_connections[0]
        if not live_enabled:
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_live_runtime_disabled"
            )
        if _clean(connection.environment).lower() != "production":
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_environment_mismatch"
            )
        database_identity = _clean(
            config.get("CUTOVER_DATABASE_IDENTITY_SHA256")
        ).lower()
        if len(database_identity) != 64 or any(
            ch not in "0123456789abcdef" for ch in database_identity
        ):
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_database_identity_required"
            )
        bind = db.session.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_postgresql_required"
            )
        isolation = db.session.execute(
            text("SELECT current_setting('transaction_isolation')")
        ).scalar_one()
        if _clean(isolation).lower() != "serializable":
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_serializable_required"
            )
        for lock_key in advisory_lock_keys(
            database_identity_sha256=database_identity,
            tenant_slug=_clean(tenant.slug).lower(),
            external_account_id=_clean(connection.external_account_id),
        ):
            acquired = db.session.execute(
                text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
                {"lock_key": lock_key},
            ).scalar_one()
            if acquired is not True:
                raise ManagedProviderConnectionStateError(
                    "managed_provider_connection_lock_contended_retry_required"
                )
        db.session.refresh(connection)
        refreshed_marker = (
            connection.config.get(MANAGED_CONNECTION_MARKER)
            if isinstance(connection.config, dict)
            else None
        )
        if not isinstance(refreshed_marker, dict) or refreshed_marker.get("enabled") is not True:
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_marker_changed_after_lock"
            )
        return connection
    environment = "production" if live_enabled else "sandbox"
    connection = ProviderConnection.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment=environment,
    ).first()
    if connection:
        return connection
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment=environment,
        status="needs_setup",
    )
    db.session.add(connection)
    db.session.flush()
    return connection


def _sender_query(tenant: TenantProfile, state: Mapping[str, Any], phone: str | None) -> ProviderSender | None:
    if state.get("sender_sid"):
        sender = ProviderSender.query.filter_by(tenant_id=tenant.id, sender_sid=str(state.get("sender_sid"))).first()
        if sender:
            return sender
    if phone:
        sender = ProviderSender.query.filter_by(tenant_id=tenant.id, channel="whatsapp", phone_number=phone).first()
        if sender:
            return sender
    sender_id = state.get("sender_id") or _sender_id_from_phone(phone)
    if sender_id:
        return ProviderSender.query.filter_by(tenant_id=tenant.id, sender_id=str(sender_id)).first()
    return None


def sync_twilio_provider_records(
    tenant: TenantProfile,
    state: Mapping[str, Any] | None = None,
    *,
    app_config: Mapping[str, Any],
    actor_user: Any = None,
    request_id: str | None = None,
    event_type: str | None = None,
) -> tuple[ProviderConnection, ProviderSender | None]:
    state = dict(state or _twilio_state(tenant))
    connection = _get_or_create_connection(tenant, app_config)
    callbacks = _status_callback_urls(app_config)

    management = (
        connection.config.get(MANAGED_CONNECTION_MARKER)
        if isinstance(connection.config, dict)
        else None
    )
    is_managed = isinstance(management, dict) and management.get("enabled") is True
    derived_connection_status = _connection_status(state)
    managed_online = is_managed and is_sender_ready_status(connection.status)
    managed_sender = None
    if is_managed:
        if managed_online and not (
            management.get("contract_version")
            == MANAGED_CONNECTION_CONTRACT_VERSION
            and
            management.get("promotion_required") is False
            and _is_sha256(management.get("provider_snapshot_sha256"))
            and _is_sha256(management.get("credential_attestation_sha256"))
            and _is_deployment_revision(
                management.get("destination_deployment_revision")
            )
        ):
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_promotion_evidence_incomplete"
            )
        state_account = _clean(state.get("twilio_account_sid"))
        state_token_ref = _clean(state.get("twilio_subaccount_token_ref"))
        expected_account = _clean(connection.external_account_id)
        expected_credentials_ref = _clean(connection.credentials_ref)
        if not state_account or state_account != expected_account:
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_account_drift"
            )
        if (
            not state_token_ref
            or f"env:{state_token_ref}" != expected_credentials_ref
        ):
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_credentials_ref_drift"
            )
        if is_sender_ready_status(connection.status) and not is_sender_ready_status(
            derived_connection_status
        ):
            raise ManagedProviderConnectionStateError(
                "managed_provider_connection_status_downgrade_blocked"
            )
        if managed_online:
            state_phone = _normalize_phone(
                state.get("requested_phone_number")
                or state.get("phone_number")
                or state.get("sender_id")
            )
            managed_sender = _sender_query(tenant, state, state_phone)
            if managed_sender is None:
                raise ManagedProviderConnectionStateError(
                    "managed_provider_sender_missing"
                )
            authoritative_pairs = (
                (state_phone, managed_sender.phone_number, "phone"),
                (_clean(state.get("sender_id")), _clean(managed_sender.sender_id), "sender_id"),
                (_clean(state.get("sender_sid")), _clean(managed_sender.sender_sid), "sender_sid"),
                (
                    _clean(state.get("messaging_service_sid")),
                    _clean(managed_sender.messaging_service_sid),
                    "messaging_service_sid",
                ),
            )
            for observed, expected, field in authoritative_pairs:
                if not observed or observed != expected:
                    raise ManagedProviderConnectionStateError(
                        f"managed_provider_sender_{field}_drift"
                    )
            if _clean(managed_sender.status).lower() != "online":
                raise ManagedProviderConnectionStateError(
                    "managed_provider_sender_database_status_invalid"
                )
            if managed_sender.webhook_url != callbacks["webhook_url"]:
                raise ManagedProviderConnectionStateError(
                    "managed_provider_sender_webhook_drift"
                )
            if managed_sender.status_callback_url != callbacks["status_callback_url"]:
                raise ManagedProviderConnectionStateError(
                    "managed_provider_sender_status_callback_drift"
                )

    connection.status = (
        connection.status if is_managed else derived_connection_status
    )
    connection.display_name = state.get("display_name") or getattr(tenant, "nombre", None)
    connection.external_account_id = (
        connection.external_account_id
        if is_managed
        else state.get("twilio_account_sid")
    )
    connection.external_business_id = state.get("waba_id")
    connection.external_app_id = _clean(app_config.get("TWILIO_META_APP_ID")) or None
    connection.configuration_id = _clean(app_config.get("TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID")) or None
    connection.partner_solution_id = _clean(app_config.get("TWILIO_PARTNER_SOLUTION_ID")) or None
    connection.credentials_ref = (
        connection.credentials_ref
        if is_managed
        else (
            "env:twilio_parent"
            if _clean(app_config.get("TWILIO_ACCOUNT_SID"))
            else None
        )
    )
    connection.capabilities = {
        "embedded_signup": bool(_clean(app_config.get("TWILIO_META_APP_ID")) and _clean(app_config.get("TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID"))),
        "sender_registration": True,
        "status_callbacks": True,
        "subaccounts": True,
        "templates": True,
    }
    next_connection_config = {
        "webhook_url": callbacks["webhook_url"],
        "status_callback_url": callbacks["status_callback_url"],
        "live_enabled": _bool_config(app_config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED"),
    }
    connection.config = connection.config if is_managed else next_connection_config
    connection.health = {
        "last_step": state.get("last_step"),
        "updated_at": state.get("updated_at"),
        "missing": [
            key for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_META_APP_ID", "TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID")
            if not _clean(app_config.get(key))
        ],
    }

    phone = _normalize_phone(state.get("requested_phone_number") or state.get("phone_number") or state.get("sender_id"))
    sender = None
    if managed_online and not (
        phone
        or state.get("sender_sid")
        or state.get("sender_id")
        or state.get("phone_number_id")
    ):
        raise ManagedProviderConnectionStateError(
            "managed_provider_sender_state_missing"
        )
    if phone or state.get("sender_sid") or state.get("sender_id") or state.get("phone_number_id"):
        sender = managed_sender or _sender_query(tenant, state, phone)
        if managed_online and not sender:
            raise ManagedProviderConnectionStateError(
                "managed_provider_sender_missing"
            )
        if not sender:
            sender = ProviderSender(
                tenant_id=tenant.id,
                provider_connection_id=connection.id,
                channel="whatsapp",
                sender_type="whatsapp_business",
                phone_number=phone,
            )
            db.session.add(sender)
            db.session.flush()
        sender.provider_connection_id = connection.id
        if not managed_online:
            sender.phone_number = phone or sender.phone_number
            sender.sender_id = state.get("sender_id") or _sender_id_from_phone(phone) or sender.sender_id
            sender.sender_sid = state.get("sender_sid") or sender.sender_sid
            sender.messaging_service_sid = state.get("messaging_service_sid") or sender.messaging_service_sid
        if not managed_online:
            sender.waba_id = state.get("waba_id") or sender.waba_id
            sender.phone_number_id = state.get("phone_number_id") or sender.phone_number_id
        sender.display_name = state.get("display_name") or getattr(tenant, "nombre", None)
        derived_sender_status = _sender_status(state)
        if managed_online:
            if str(derived_sender_status or "").strip().lower() != "online":
                raise ManagedProviderConnectionStateError(
                    "managed_provider_sender_status_downgrade_blocked"
                )
            if sender.webhook_url and sender.webhook_url != callbacks["webhook_url"]:
                raise ManagedProviderConnectionStateError(
                    "managed_provider_sender_webhook_drift"
                )
            if (
                sender.status_callback_url
                and sender.status_callback_url != callbacks["status_callback_url"]
            ):
                raise ManagedProviderConnectionStateError(
                    "managed_provider_sender_status_callback_drift"
                )
        sender.status = sender.status if managed_online else derived_sender_status
        sender.webhook_url = sender.webhook_url if managed_online else callbacks["webhook_url"]
        sender.status_callback_url = (
            sender.status_callback_url
            if managed_online
            else callbacks["status_callback_url"]
        )
        sender.metadata_json = {
            "embedded_signup_session_id": state.get("embedded_signup_session_id"),
            "last_step": state.get("last_step"),
            "requested_phone_number": state.get("requested_phone_number"),
        }
        sender.last_status_at = datetime.now(timezone.utc)

    if event_type:
        record_messaging_event(
            tenant_id=tenant.id,
            provider_connection_id=connection.id,
            provider_sender_id=getattr(sender, "id", None),
            channel="whatsapp",
            direction="system",
            event_type=event_type,
            provider="twilio",
            request_id=request_id,
            payload={
                "state": state,
                "actor_user_id": getattr(actor_user, "id", None),
                "connection_status": connection.status,
                "sender_status": getattr(sender, "status", None),
            },
        )

    return connection, sender


def record_messaging_event(
    *,
    tenant_id: int,
    channel: str,
    direction: str,
    event_type: str,
    provider: str | None = None,
    provider_connection_id: int | None = None,
    provider_sender_id: int | None = None,
    contact_id: str | None = None,
    provider_event_id: str | None = None,
    external_message_sid: str | None = None,
    external_status: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    payload: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> MessagingEventLedger:
    event = MessagingEventLedger(
        tenant_id=tenant_id,
        provider_connection_id=provider_connection_id,
        provider_sender_id=provider_sender_id,
        contact_id=contact_id,
        channel=channel,
        direction=direction,
        event_type=event_type,
        provider=provider,
        provider_event_id=provider_event_id,
        external_message_sid=external_message_sid,
        external_status=external_status,
        sender=sender,
        recipient=recipient,
        payload=dict(payload or {}),
        metadata_json=dict(metadata or {}),
        request_id=request_id,
        error_code=error_code,
        error_message=error_message,
        occurred_at=datetime.now(timezone.utc),
    )
    db.session.add(event)
    return event


def _serialize_connection(connection: ProviderConnection | None) -> dict[str, Any] | None:
    if not connection:
        return None
    return {
        "id": connection.id,
        "provider": connection.provider,
        "channel": connection.channel,
        "environment": connection.environment,
        "status": connection.status,
        "display_name": connection.display_name,
        "external_account_id": connection.external_account_id,
        "external_business_id": connection.external_business_id,
        "external_app_id": connection.external_app_id,
        "configuration_id": connection.configuration_id,
        "partner_solution_id": connection.partner_solution_id,
        "capabilities": connection.capabilities or {},
        "health": connection.health or {},
        "config": connection.config or {},
        "updated_at": connection.updated_at.isoformat() if connection.updated_at else None,
    }


def _serialize_sender(sender: ProviderSender | None) -> dict[str, Any] | None:
    if not sender:
        return None
    return {
        "id": sender.id,
        "channel": sender.channel,
        "sender_type": sender.sender_type,
        "phone_number": sender.phone_number,
        "sender_id": sender.sender_id,
        "sender_sid": sender.sender_sid,
        "messaging_service_sid": sender.messaging_service_sid,
        "waba_id": sender.waba_id,
        "phone_number_id": sender.phone_number_id,
        "display_name": sender.display_name,
        "status": sender.status,
        "verification_status": sender.verification_status,
        "quality_rating": sender.quality_rating,
        "webhook_url": sender.webhook_url,
        "status_callback_url": sender.status_callback_url,
        "metadata": sender.metadata_json or {},
        "last_status_at": sender.last_status_at.isoformat() if sender.last_status_at else None,
    }


def _serialize_event(event: MessagingEventLedger) -> dict[str, Any]:
    return {
        "id": event.id,
        "channel": event.channel,
        "direction": event.direction,
        "event_type": event.event_type,
        "provider": event.provider,
        "external_message_sid": event.external_message_sid,
        "external_status": event.external_status,
        "recipient": event.recipient,
        "request_id": event.request_id,
        "error_code": event.error_code,
        "error_message": event.error_message,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
    }


def build_whatsapp_provider_status(tenant: TenantProfile, app_config: Mapping[str, Any]) -> dict[str, Any]:
    connection, sender = sync_twilio_provider_records(tenant, app_config=app_config)

    template_count = MessageTemplateRegistry.query.filter_by(tenant_id=tenant.id, channel="whatsapp").count()
    opt_in_count = ConsentLedger.query.filter_by(tenant_id=tenant.id, channel="whatsapp", status="opt_in").count()
    opt_out_count = ConsentLedger.query.filter_by(tenant_id=tenant.id, channel="whatsapp", status="opt_out").count()
    recent_events = (
        MessagingEventLedger.query.filter_by(tenant_id=tenant.id, channel="whatsapp")
        .order_by(MessagingEventLedger.occurred_at.desc())
        .limit(20)
        .all()
    )

    checks = [
        {
            "id": "platform_credentials",
            "label": "Credenciales plataforma",
            "ok": not (connection.health or {}).get("missing"),
            "missing": (connection.health or {}).get("missing", []),
        },
        {
            "id": "embedded_signup",
            "label": "Embedded Signup",
            "ok": bool((connection.capabilities or {}).get("embedded_signup")),
        },
        {
            "id": "subaccount",
            "label": "Subcuenta Twilio",
            "ok": bool(connection.external_account_id),
        },
        {
            "id": "sender",
            "label": "Sender WhatsApp",
            "ok": bool(sender and (sender.status == "registered" or is_sender_ready_status(sender.status))),
        },
        {
            "id": "templates",
            "label": "Templates",
            "ok": template_count > 0,
            "count": template_count,
        },
    ]

    if not checks[0]["ok"]:
        next_action = "complete_platform_env"
    elif not checks[1]["ok"]:
        next_action = "configure_meta_embedded_signup"
    elif not checks[2]["ok"]:
        next_action = "provision_twilio_subaccount"
    elif not checks[3]["ok"]:
        next_action = "complete_embedded_signup_and_register_sender"
    elif not checks[4]["ok"]:
        next_action = "sync_or_create_templates"
    else:
        next_action = "ready_for_pilot"

    return {
        "contract_version": CONTRACT_VERSION,
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "vertical": tenant.vertical,
        },
        "provider": "twilio",
        "channel": "whatsapp",
        "status": connection.status,
        "connection": _serialize_connection(connection),
        "sender": _serialize_sender(sender),
        "compliance": {
            "enforce_24h_window": True,
            "templates_required_outside_24h": True,
            "opt_in_count": opt_in_count,
            "opt_out_count": opt_out_count,
            "number_lookup_policy": "do_not_offer_unbounded_whatsapp_capability_lookup",
        },
        "readiness_checks": checks,
        "recent_events": [_serialize_event(event) for event in recent_events],
        "next_action": next_action,
        "frontend_contract": {
            "render_as": "whatsapp_provider_status",
            "primary_action": next_action,
            "show_progress_steps": True,
            "show_logs_link": True,
            "show_template_health": True,
        },
    }
