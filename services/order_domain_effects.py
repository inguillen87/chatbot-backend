"""Durable, tenant-bound external effects for newly created ``PymePedido`` rows.

The outbox stores only an owner binding and opaque recipient references.  Each
handler reloads the order and its current recipient during preflight; customer
and tenant contact details never enter the operational outbox payload.

Order/MarketOrder projections intentionally do not use this runtime: provider
handlers cross an I/O boundary and the generic executor rolls incidental ORM
writes back.  Those projections remain on the existing post-commit path until
they receive a dedicated transactional materialization contract.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from flask import current_app, has_app_context

from models import PymePedido, TenantProfile, User, db
from services.domain_effect_gate import (
    DomainEffectOutboxConfigurationError,
    resolve_domain_effect_outbox_policy,
)
from services.domain_effect_outbox import (
    AmbiguousDomainEffectError,
    DeliveredDomainEffect,
    DomainEffectClaim,
    DomainEffectRegistry,
    DomainEffectValidationError,
    PermanentDomainEffectError,
    PreparedDomainEffect,
    SkippedDomainEffect,
    stage_domain_effect,
)
from utils.validators import normalize_phone, validate_email_address


PYME_ORDER_AGGREGATE = "pyme_order"

CUSTOMER_EMAIL_HANDLER = "order.created.email.customer.v1"
OWNER_EMAIL_HANDLER = "order.created.email.owner.v1"
DISPATCH_EMAIL_HANDLER = "order.created.email.dispatch.v1"
CUSTOMER_WHATSAPP_HANDLER = "order.created.whatsapp.customer.v1"
DISPATCH_WHATSAPP_HANDLER = "order.created.whatsapp.dispatch.v1"

ORDER_CUSTOMER_RECIPIENT = "role:order.customer"
TENANT_OWNER_RECIPIENT = "role:tenant.owner"
ORDER_DISPATCH_RECIPIENT = "role:order.dispatch"

_MAX_DISPATCH_PHONE_RECIPIENTS = 10


def _skip(reason_code: str) -> SkippedDomainEffect:
    return SkippedDomainEffect(reason_code=reason_code, result={})


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized > 0 else None


def _owner_binding(tenant_id: int, owner_id: int) -> str:
    material = f"order-owner.v1:{tenant_id}:{owner_id}".encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _validate_order_effect_payload(payload: dict[str, Any]) -> None:
    owner_binding = payload.get("owner_binding")
    if (
        set(payload) != {"owner_binding"}
        or not isinstance(owner_binding, str)
        or len(owner_binding) != 64
        or owner_binding != owner_binding.lower()
    ):
        raise DomainEffectValidationError("order_effect_payload_invalid")
    try:
        int(owner_binding, 16)
    except ValueError as exc:
        raise DomainEffectValidationError("order_effect_payload_invalid") from exc


def _load_scoped_order(
    claim: DomainEffectClaim,
) -> tuple[PymePedido, TenantProfile, User]:
    tenant_id = _positive_int(claim.tenant_id)
    order_id = _positive_int(claim.aggregate_ref)
    if (
        tenant_id is None
        or order_id is None
        or claim.aggregate_type != PYME_ORDER_AGGREGATE
    ):
        raise PermanentDomainEffectError("order_aggregate_invalid")

    order = PymePedido.query.filter_by(
        id=order_id,
        tenant_id=tenant_id,
    ).one_or_none()
    if order is None:
        raise PermanentDomainEffectError("order_not_found")

    profile = db.session.get(TenantProfile, tenant_id)
    owner_id = _positive_int(getattr(profile, "pyme_id", None)) if profile else None
    if (
        profile is None
        or str(getattr(profile, "tipo", "")).strip() != "pyme"
        or not bool(getattr(profile, "is_active", False))
        or owner_id is None
        or _positive_int(getattr(order, "pyme_id", None)) != owner_id
    ):
        raise PermanentDomainEffectError("order_tenant_binding_invalid")

    expected_binding = str(claim.payload.get("owner_binding") or "")
    if not hmac.compare_digest(
        expected_binding,
        _owner_binding(tenant_id, owner_id),
    ):
        raise PermanentDomainEffectError("order_tenant_binding_invalid")

    owner = db.session.get(User, owner_id)
    if owner is None or _positive_int(getattr(owner, "tenant_id", None)) != tenant_id:
        raise PermanentDomainEffectError("order_owner_binding_invalid")
    return order, profile, owner


def _email_preflight_error() -> str | None:
    from services import email_service

    if not email_service._email_notifications_enabled():
        return "email_notifications_disabled"
    smtp_valid, _ = email_service.validar_configuracion_smtp(require_auth=True)
    if not smtp_valid:
        return "email_configuration_missing"
    return None


def _empresa_info(owner: User) -> dict[str, Any]:
    return {
        "nombre": getattr(owner, "nombre_empresa", None) or getattr(owner, "name", None),
        "direccion": getattr(owner, "direccion", None),
        "telefono": getattr(owner, "telefono", None),
        "email": getattr(owner, "email", None),
    }


def _optional_pdf(order: PymePedido, owner: User) -> bytes | None:
    try:
        from services.pedido_pdf import generar_pdf_nota_pedido

        return generar_pdf_nota_pedido(order, empresa_info=_empresa_info(owner))
    except Exception:
        # PDF is an optional attachment.  Existing email helpers safely send
        # their HTML/text body when local rendering is unavailable.
        return None


def _provider_accepted(delivered: Any) -> DeliveredDomainEffect:
    if not delivered:
        raise AmbiguousDomainEffectError("provider_acceptance_unknown")
    # Synchronous adapters prove provider acceptance only. Final delivery
    # still requires the provider's status callback/reconciliation contract.
    return DeliveredDomainEffect(result={"delivery": "provider_accepted"})


def _prepare_customer_email(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    order, profile, owner = _load_scoped_order(claim)
    if not bool(getattr(profile, "send_buyer_email", True)):
        return _skip("buyer_email_disabled")
    recipient = str(getattr(order, "email_cliente", None) or "").strip()
    if not recipient:
        return _skip("customer_email_missing")
    if not validate_email_address(recipient):
        return _skip("customer_email_invalid")
    preflight_error = _email_preflight_error()
    if preflight_error:
        return _skip(preflight_error)
    pdf_bytes = _optional_pdf(order, owner)
    owner_info = _empresa_info(owner)

    def deliver() -> DeliveredDomainEffect:
        from services.email_service import enviar_email_pedido_cliente

        return _provider_accepted(
            enviar_email_pedido_cliente(
                order,
                pdf_bytes=pdf_bytes,
                empresa_info=owner_info,
            )
        )

    return PreparedDomainEffect(deliver=deliver)


def _prepare_owner_email(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    order, _, owner = _load_scoped_order(claim)
    recipient = str(getattr(owner, "email", None) or "").strip()
    if not recipient:
        return _skip("owner_email_missing")
    if not validate_email_address(recipient):
        return _skip("owner_email_invalid")
    preflight_error = _email_preflight_error()
    if preflight_error:
        return _skip(preflight_error)
    pdf_bytes = _optional_pdf(order, owner)

    def deliver() -> DeliveredDomainEffect:
        # ``enviar_email_pedido_admin`` resolves a process-global ADMIN_EMAIL
        # and is therefore unsafe for a multi-tenant worker.  The explicit
        # destination helper keeps the effect bound to the current owner.
        from services.email_service import enviar_email_pedido_despacho

        return _provider_accepted(
            enviar_email_pedido_despacho(
                order,
                recipient,
                pdf_bytes=pdf_bytes,
            )
        )

    return PreparedDomainEffect(deliver=deliver)


def _prepare_dispatch_email(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    order, profile, owner = _load_scoped_order(claim)
    if not bool(getattr(profile, "send_dispatch_email", True)):
        return _skip("dispatch_email_disabled")
    recipient = str(getattr(profile, "dispatch_email", None) or "").strip()
    if not recipient:
        return _skip("dispatch_email_missing")
    if not validate_email_address(recipient):
        return _skip("dispatch_email_invalid")
    owner_email = str(getattr(owner, "email", None) or "").strip().lower()
    if owner_email and hmac.compare_digest(recipient.lower(), owner_email):
        return _skip("dispatch_email_duplicates_owner")
    preflight_error = _email_preflight_error()
    if preflight_error:
        return _skip(preflight_error)
    pdf_bytes = _optional_pdf(order, owner)

    def deliver() -> DeliveredDomainEffect:
        from services.email_service import enviar_email_pedido_despacho

        return _provider_accepted(
            enviar_email_pedido_despacho(
                order,
                recipient,
                pdf_bytes=pdf_bytes,
            )
        )

    return PreparedDomainEffect(deliver=deliver)


def _whatsapp_transport_available() -> bool:
    from services import notifications

    # The current compatibility shim only logs.  An actual adapter must opt in
    # explicitly before the worker is allowed to cross the durable I/O marker.
    return bool(getattr(notifications, "ORDER_WHATSAPP_TRANSPORT_IMPLEMENTED", False))


def _prepare_customer_whatsapp(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    order, _, _ = _load_scoped_order(claim)
    phone = normalize_phone(str(getattr(order, "telefono_cliente", None) or ""))
    if not phone:
        return _skip("customer_whatsapp_missing")
    if not _whatsapp_transport_available():
        return _skip("whatsapp_transport_unavailable")

    def deliver() -> DeliveredDomainEffect:
        from services.notifications import enviar_notificacion_whatsapp_con_plantilla

        return _provider_accepted(
            enviar_notificacion_whatsapp_con_plantilla(
                phone,
                getattr(order, "nombre_cliente", None) or "Cliente",
                str(order.nro_pedido),
                getattr(order, "rubro", None) or "Pedido",
            )
        )

    return PreparedDomainEffect(deliver=deliver)


def _dispatch_phones(profile: TenantProfile, *, strict: bool) -> list[str]:
    raw_tokens = [
        token.strip()
        for token in str(getattr(profile, "dispatch_phone", None) or "").split(",")
        if token.strip()
    ]
    if len(raw_tokens) > _MAX_DISPATCH_PHONE_RECIPIENTS:
        raise DomainEffectOutboxConfigurationError("order_dispatch_phone_limit_exceeded")
    normalized: list[str] = []
    for token in raw_tokens:
        phone = normalize_phone(token)
        if not phone:
            if strict:
                raise DomainEffectOutboxConfigurationError(
                    "order_dispatch_phone_invalid"
                )
            raise PermanentDomainEffectError("order_dispatch_phone_invalid")
        if phone not in normalized:
            normalized.append(phone)
    return normalized


def _dispatch_recipient_hash(
    secret: str,
    *,
    tenant_id: int,
    phone: str,
) -> str:
    material = f"order-dispatch-whatsapp.v1:{tenant_id}:{phone}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _prepare_dispatch_whatsapp(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    order, profile, _ = _load_scoped_order(claim)
    if not bool(getattr(profile, "send_dispatch_whatsapp", True)):
        return _skip("dispatch_whatsapp_disabled")
    policy = resolve_domain_effect_outbox_policy(
        current_app.config,
        tenant_id=int(claim.tenant_id),
    )
    if not policy.enabled or not policy.secret:
        raise PermanentDomainEffectError("order_outbox_policy_changed")
    expected_ref = str(claim.recipient_ref)
    selected_phone = next(
        (
            phone
            for phone in _dispatch_phones(profile, strict=False)
            if hmac.compare_digest(
                expected_ref,
                "recipient_hash:"
                + _dispatch_recipient_hash(
                    policy.secret,
                    tenant_id=int(claim.tenant_id),
                    phone=phone,
                ),
            )
        ),
        None,
    )
    if selected_phone is None:
        return _skip("dispatch_whatsapp_recipient_changed")
    if not _whatsapp_transport_available():
        return _skip("whatsapp_transport_unavailable")

    def deliver() -> DeliveredDomainEffect:
        from services.notifications import enviar_notificacion_whatsapp_con_plantilla

        return _provider_accepted(
            enviar_notificacion_whatsapp_con_plantilla(
                selected_phone,
                "Deposito",
                str(order.nro_pedido),
                "Nuevo Pedido a Preparar",
            )
        )

    return PreparedDomainEffect(deliver=deliver)


ORDER_DOMAIN_EFFECT_REGISTRY = DomainEffectRegistry()
ORDER_DOMAIN_EFFECT_REGISTRY.register(
    CUSTOMER_EMAIL_HANDLER,
    _prepare_customer_email,
    payload_validator=_validate_order_effect_payload,
)
ORDER_DOMAIN_EFFECT_REGISTRY.register(
    OWNER_EMAIL_HANDLER,
    _prepare_owner_email,
    payload_validator=_validate_order_effect_payload,
)
ORDER_DOMAIN_EFFECT_REGISTRY.register(
    DISPATCH_EMAIL_HANDLER,
    _prepare_dispatch_email,
    payload_validator=_validate_order_effect_payload,
)
ORDER_DOMAIN_EFFECT_REGISTRY.register(
    CUSTOMER_WHATSAPP_HANDLER,
    _prepare_customer_whatsapp,
    payload_validator=_validate_order_effect_payload,
)
ORDER_DOMAIN_EFFECT_REGISTRY.register(
    DISPATCH_WHATSAPP_HANDLER,
    _prepare_dispatch_whatsapp,
    payload_validator=_validate_order_effect_payload,
)


def stage_order_created_effects(
    order: PymePedido,
    *,
    expected_owner_id: Any = None,
    session: Any = None,
    registry: DomainEffectRegistry | None = None,
) -> bool:
    """Stage every external order-created effect without committing.

    ``True`` tells ``PedidoService`` to suppress the legacy direct dispatcher.
    ``False`` leaves a non-canary tenant on the existing path unchanged.
    """

    if not has_app_context():
        raise DomainEffectOutboxConfigurationError("domain_effect_app_context_required")
    tenant_id = _positive_int(getattr(order, "tenant_id", None))
    if tenant_id is None:
        mode = str(
            current_app.config.get("DOMAIN_EFFECT_OUTBOX_MODE", "legacy") or "legacy"
        ).strip().lower()
        if mode == "queue":
            raise DomainEffectOutboxConfigurationError("domain_effect_tenant_invalid")
        return False

    policy = resolve_domain_effect_outbox_policy(
        current_app.config,
        tenant_id=tenant_id,
    )
    if not policy.enabled:
        return False
    if not isinstance(order, PymePedido):
        raise ValueError("order_effect_aggregate_mismatch")

    effect_session = session or db.session
    profile = effect_session.get(TenantProfile, tenant_id)
    order_owner = _positive_int(getattr(order, "pyme_id", None))
    expected_owner = _positive_int(expected_owner_id)
    profile_owner = _positive_int(getattr(profile, "pyme_id", None)) if profile else None
    if (
        profile is None
        or str(getattr(profile, "tipo", "")).strip() != "pyme"
        or not bool(getattr(profile, "is_active", False))
        or order_owner is None
        or expected_owner is None
        or order_owner != expected_owner
        or profile_owner != expected_owner
    ):
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_order_tenant_binding_invalid"
        )

    owner = effect_session.get(User, expected_owner)
    if owner is None or _positive_int(getattr(owner, "tenant_id", None)) != tenant_id:
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_order_owner_binding_invalid"
        )
    order_id = _positive_int(getattr(order, "id", None))
    if order_id is None:
        raise DomainEffectOutboxConfigurationError("domain_effect_aggregate_invalid")
    if not policy.secret:
        raise DomainEffectOutboxConfigurationError("domain_effect_secret_invalid")

    effect_registry = registry or ORDER_DOMAIN_EFFECT_REGISTRY
    common = {
        "tenant_id": tenant_id,
        "aggregate_type": PYME_ORDER_AGGREGATE,
        "aggregate_ref": str(order_id),
        "intent_secret": policy.secret,
        "registry": effect_registry,
        "payload": {
            "owner_binding": _owner_binding(tenant_id, expected_owner),
        },
        "max_attempts": policy.max_attempts,
        "max_payload_bytes": policy.max_payload_bytes,
        "session": effect_session,
    }
    specs = [
        {
            "effect_type": "order.created.email.customer",
            "handler_name": CUSTOMER_EMAIL_HANDLER,
            "channel": "email",
            "recipient_ref": ORDER_CUSTOMER_RECIPIENT,
        },
        {
            "effect_type": "order.created.email.owner",
            "handler_name": OWNER_EMAIL_HANDLER,
            "channel": "email",
            "recipient_ref": TENANT_OWNER_RECIPIENT,
        },
        {
            "effect_type": "order.created.email.dispatch",
            "handler_name": DISPATCH_EMAIL_HANDLER,
            "channel": "email",
            "recipient_ref": ORDER_DISPATCH_RECIPIENT,
        },
        {
            "effect_type": "order.created.whatsapp.customer",
            "handler_name": CUSTOMER_WHATSAPP_HANDLER,
            "channel": "whatsapp",
            "recipient_ref": ORDER_CUSTOMER_RECIPIENT,
        },
    ]
    if bool(getattr(profile, "send_dispatch_whatsapp", True)):
        for phone in _dispatch_phones(profile, strict=True):
            specs.append(
                {
                    "effect_type": "order.created.whatsapp.dispatch",
                    "handler_name": DISPATCH_WHATSAPP_HANDLER,
                    "channel": "whatsapp",
                    "recipient_ref": "recipient_hash:"
                    + _dispatch_recipient_hash(
                        policy.secret,
                        tenant_id=tenant_id,
                        phone=phone,
                    ),
                }
            )

    for spec in specs:
        stage_domain_effect(
            **common,
            **spec,
            effect_key=(
                f"{PYME_ORDER_AGGREGATE}:{order_id}:created:"
                f"{spec['channel']}:{spec['recipient_ref']}"
            ),
        )
    return True


__all__ = [
    "CUSTOMER_EMAIL_HANDLER",
    "CUSTOMER_WHATSAPP_HANDLER",
    "DISPATCH_EMAIL_HANDLER",
    "DISPATCH_WHATSAPP_HANDLER",
    "ORDER_DOMAIN_EFFECT_REGISTRY",
    "OWNER_EMAIL_HANDLER",
    "PYME_ORDER_AGGREGATE",
    "stage_order_created_effects",
]
