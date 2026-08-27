from flask import Blueprint, current_app, jsonify, g
from utils.auth_helpers import token_requerido
from models import IntegrationAccount, TenantProfile
from services.plan_access import (
    integration_access_payload,
    integration_plan_required_payload,
    plan_allows_full_integrations,
)

integrations_bp = Blueprint('integrations', __name__)

_LEGACY_TRANSPORT_CONTRACT_VERSION = "tenant.integration.legacy_transport_disabled.v1"


def legacy_integration_transport_disabled_response(
    *,
    surface: str,
    replacement_endpoint: str | None = None,
):
    """Return a side-effect-free denial for the retired integration transport.

    ``LEGACY_INTEGRATIONS_TRANSPORT_ENABLED`` is deliberately only an
    operational migration marker.  Setting it cannot revive the old handlers:
    a future implementation must first provide signed, expiring, one-time OAuth
    state and tenant-bound provider credentials.  This prevents an accidental
    Vercel/Render environment change from re-enabling raw ``tenant_id`` or
    ``state`` trust.
    """

    migration_flag_requested = bool(
        current_app.config.get("LEGACY_INTEGRATIONS_TRANSPORT_ENABLED", False)
    )
    reason_codes = {
        "oauth_connect": "legacy_oauth_connect_disabled",
        "oauth_callback": "legacy_oauth_callback_disabled",
        "unsigned_webhook": "legacy_unsigned_webhook_disabled",
    }
    payload = {
        "contract_version": _LEGACY_TRANSPORT_CONTRACT_VERSION,
        "status": "disabled",
        "reason_code": reason_codes[surface],
        "retryable": False,
        "next_action": "configure_tenant_bound_signed_provider_adapter",
    }
    if replacement_endpoint:
        payload["replacement_endpoint"] = replacement_endpoint

    # A mistakenly enabled migration flag must still fail closed until a
    # secure transport replaces these handlers.  Use 503 to make that operator
    # misconfiguration visible; the default response is an intentionally quiet
    # 404 so the retired public surface is not advertised.
    status_code = 503 if migration_flag_requested else 404
    if migration_flag_requested:
        payload["reason_code"] = "legacy_integration_secure_transport_unavailable"

    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


def _plan_allows_integrations(tenant: TenantProfile) -> bool:
    return plan_allows_full_integrations(tenant)


def _provider_feature_id(provider: str) -> str:
    if provider in {"mercadolibre", "tiendanube"}:
        return "marketplace_sync"
    if provider == "whatsapp":
        return "whatsapp_business_platform"
    return "marketplace_sync"


def _integration_plan_required_response(
    tenant: TenantProfile,
    feature_id: str = "marketplace_sync",
):
    return (
        jsonify(
            integration_plan_required_payload(
                tenant,
                feature_id,
                render_as="integration_locked_state",
            )
        ),
        403,
    )


@integrations_bp.route('/<provider>/connect', methods=['POST'])
def connect(provider):
    """Reject the legacy OAuth flow before auth, database or provider calls.

    The retired implementation placed a plain tenant id in OAuth ``state``.
    That is not an authorization boundary and provided neither expiry nor
    one-time replay protection, so it cannot be safely retained as a fallback.
    """

    return legacy_integration_transport_disabled_response(surface="oauth_connect")

@integrations_bp.route('/<provider>/callback', methods=['GET'])
def callback(provider):
    """Reject raw or replayable OAuth state without resolving a tenant."""

    if provider == "whatsapp":
        return (
            jsonify(
                {
                    "contract_version": "tenant.integration.callback.deprecated.v1",
                    "error": "deprecated_whatsapp_callback",
                    "reason_code": "use_twilio_tech_provider_flow",
                    "message": "WhatsApp productivo se conecta desde el flujo Twilio Tech Provider / Meta Embedded Signup.",
                    "retryable": False,
                    "next_action": "open_twilio_tech_provider_onboarding",
                    "replacement_endpoints": {
                        "contract": "/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider",
                        "embedded_signup_completion": "/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider/embedded-signup",
                        "provider_status": "/api/v2/tenants/{tenant_slug}/integrations/whatsapp/status",
                    },
                }
            ),
            410,
        )

    return legacy_integration_transport_disabled_response(surface="oauth_callback")

@integrations_bp.route('/webhooks/<provider>', methods=['POST'])
def webhook(provider):
    """Reject unsigned legacy events without parsing or mutating their body."""

    return legacy_integration_transport_disabled_response(
        surface="unsigned_webhook",
        replacement_endpoint="/api/omnichannel/adapters/{connection_id}/inbound",
    )


@integrations_bp.route('/<provider>/preview', methods=['GET'])
@token_requerido
def preview_integration(current_user, provider):
    """
    Returns a preview of the integration mapping status.
    This prevents 404s on the frontend and provides user feedback.
    """
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({"error": "Tenant required"}), 400
    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant, _provider_feature_id(provider))

    # Validate provider
    if provider not in ['mercadolibre', 'tiendanube']:
        return jsonify({"error": "Provider not supported"}), 400

    # Check connection status
    account = IntegrationAccount.query.filter_by(
        tenant_id=tenant.id,
        type=provider,
        status="active"
    ).first()

    if not account:
        access = integration_access_payload(tenant)
        return jsonify({
            "status": "disconnected",
            "message": f"Conectá tu cuenta de {provider} para sincronizar productos.",
            "preview_data": [],
            "mapped_count": 0,
            "error_count": 0,
            "access": access,
            "frontend_contract": {
                "render_as": "marketplace_integration_preview",
                "feature_id": _provider_feature_id(provider),
                "connect_enabled": bool(access.get("enabled")),
            },
        })

    # TODO: Implement real-time fetch of remote items for comparison
    # For now, return a 'connected' state with existing catalog stats to satisfy the preview UI

    # Simple heuristic: Count items with the provider's metadata/ID if stored,
    # or just total items eligible for sync.
    from models import CatalogoItem
    total_items = CatalogoItem.query.filter_by(tenant_id=tenant.id).count()

    # Mock preview data for the UI
    preview_sample = []
    if total_items > 0:
        item = CatalogoItem.query.filter_by(tenant_id=tenant.id).first()
        preview_sample.append({
            "local_sku": item.sku or "SKU-UNK",
            "local_title": item.nombre,
            "remote_status": "pending_sync", # Default state
            "message": "Listo para publicar"
        })

    access = integration_access_payload(tenant)
    return jsonify({
        "status": "connected",
        "message": f"Cuenta vinculada. {total_items} productos listos para sincronizar.",
        "preview_data": preview_sample,
        "mapped_count": 0, # To be implemented with real sync logic
        "pending_count": total_items,
        "error_count": 0,
        "access": access,
        "frontend_contract": {
            "render_as": "marketplace_integration_preview",
            "feature_id": _provider_feature_id(provider),
            "connect_enabled": bool(access.get("enabled")),
        },
    })
