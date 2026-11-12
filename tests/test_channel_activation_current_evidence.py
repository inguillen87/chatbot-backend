"""Offline current-row report evidence; no provider, production or delivery QA."""
from datetime import datetime, timezone
import json

import pytest

from models import db, MessagingEventLedger, ProviderConnection, ProviderSender, TenantConfig, TenantProfile, User
from services import channel_activation as report
from services import meta_whatsapp_credentials as vault
from services import tdf_meta_sandbox as pilot
from services.institutional_assistant_content import digest
from tests.test_tdf_meta_sandbox import environment, TOKEN, SECRET


def configured_sandbox(env):
    env.tenant.plan = "full"
    env.sender.status = "connected"
    now = int(datetime.now(timezone.utc).timestamp())
    env.connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(connection=env.connection,
        tenant_id=46, access_token=TOKEN, revision=2, expires_at=now + 3600, now=now,
        app_config=env.app.config)}
    db.session.commit()


def test_published_canonical_corpus_is_ready_without_catalog_or_rag_receipt(environment):
    status, evidence, reason, hint = report._knowledge_content_status({}, environment.tenant)
    assert status == "ready" and reason is None
    assert environment.state["revision"] in " ".join(evidence)
    assert f"{len(environment.state['bundle']['nodes'])} contenidos canonicos" in evidence
    assert "no acredita indexacion RAG" in hint
    assert TOKEN not in json.dumps(evidence) and SECRET not in json.dumps(evidence)


def test_internal_or_corrupt_corpus_never_looks_published(environment):
    row = TenantConfig.query.filter_by(tenant_id=46, key="institutional_assistant").one()
    private = {**row.json_value, "visibility": "private"}
    private["revision"] = digest({k: private[k] for k in ("bundle_hash", "generation", "visibility")})
    row.json_value = private
    db.session.commit()
    status, evidence, reason, _ = report._knowledge_content_status({"catalog_items": 2}, environment.tenant)
    assert status == "pending" and reason == "knowledge_publication_required"
    assert private["revision"] not in " ".join(evidence)
    row.json_value = {**private, "bundle_hash": "0" * 64}
    db.session.commit()
    assert report._knowledge_content_status({"catalog_items": 2}, environment.tenant)[:3] == (
        "blocked", [], "knowledge_state_unavailable")


def test_corrupt_canonical_shape_is_not_catalog_ready(environment):
    row = TenantConfig.query.filter_by(tenant_id=46, key="institutional_assistant").one()
    state = {**row.json_value, "bundle": {**row.json_value["bundle"], "nodes": None}}
    state["bundle_hash"] = digest(state["bundle"])
    state["revision"] = digest({k: state[k] for k in ("bundle_hash", "generation", "visibility")})
    row.json_value = state
    db.session.commit()
    assert report._knowledge_content_status({"catalog_items": 2}, environment.tenant)[0] == "blocked"


def test_sandbox_is_visible_over_legacy_twilio_but_is_not_production_ready(environment, monkeypatch):
    configured_sandbox(environment)
    monkeypatch.setattr(pilot, "_graph_request", lambda *a, **k: pytest.fail("Report must not call Meta"))
    monkeypatch.setattr(vault, "open_token", lambda *a, **k: pytest.fail("Report must not open a token"))
    status, evidence, reason = report._whatsapp_status({"whatsapp_onboarding": {
        "provider": "twilio_tech_provider", "status": "online"}}, True, environment.tenant)
    assert status == "pending" and reason == "meta_sandbox_production_not_certified"
    assert "Meta Cloud API: entorno de prueba" in evidence
    assert "twilio_tech_provider" not in " ".join(evidence)
    assert "Servicio productivo y conversación actual no certificados" in evidence
    assert TOKEN not in json.dumps(evidence) and SECRET not in json.dumps(evidence)


@pytest.mark.parametrize("change,reason", [
    ("expired", "meta_sandbox_local_credential_expired"),
    ("wrong_waba", "meta_sandbox_binding_mismatch"),
    ("wrong_phone", "meta_sandbox_sender_not_unique"),
    ("disabled_runtime", "meta_sandbox_runtime_configuration_required"),
    ("wrong_reference", "meta_sandbox_credential_configuration_required"),
    ("disconnected", "meta_sandbox_configuration_pending"),
])
def test_sandbox_unavailable_states_remain_non_ready_and_secret_free(environment, change, reason):
    env = environment
    configured_sandbox(env)
    if change == "expired":
        envelope = env.connection.config[vault.PRIVATE_CONFIG_KEY]
        env.connection.config = {vault.PRIVATE_CONFIG_KEY: {**envelope, "expires_at": 1}}
    elif change == "wrong_waba": env.connection.external_account_id = "12345"
    elif change == "wrong_phone": env.sender.phone_number_id = "12345"
    elif change == "disabled_runtime": env.app.config["META_TDF_SANDBOX_ENABLED"] = False
    elif change == "wrong_reference": env.connection.credentials_ref = "legacy-secret-reference"
    elif change == "disconnected": env.connection.status = "disconnected"
    db.session.commit()
    status, evidence, code = report._meta_sandbox_status(env.tenant)
    assert status != "ready" and code == reason
    assert TOKEN not in json.dumps(evidence) and "legacy-secret-reference" not in json.dumps(evidence)


def test_receipt_counts_are_exact_scope_historical_and_do_not_certify_a_session(environment):
    env = environment
    configured_sandbox(env)
    for provider, connection_id, status in [(pilot.PROVIDER, env.connection.id, "accepted"),
            (pilot.PROVIDER, env.connection.id, "delivered"), ("foreign", env.connection.id, "delivered"),
            (pilot.PROVIDER, None, "delivered")]:
        db.session.add(MessagingEventLedger(tenant_id=46, provider_connection_id=connection_id,
            provider_sender_id=env.sender.id, channel="whatsapp", direction="outbound",
            event_type="tdf_sandbox_reply", provider=provider, external_status=status))
    db.session.commit()
    status, evidence, _ = report._meta_sandbox_status(env.tenant)
    assert status == "pending"
    assert "Histórico de prueba: 2 respuestas aceptadas; 1 con entrega registrada" in evidence
    assert "Servicio productivo y conversación actual no certificados" in evidence
    env.tenant.slug = "junin"
    assert report._meta_sandbox_status(env.tenant) is None


def test_plan_lock_precedes_test_connection_and_legacy_online_needs_current_verification(environment):
    configured_sandbox(environment)
    assert report._whatsapp_status({}, False, environment.tenant)[0] == "locked"
    status, _, reason = report._whatsapp_status({"whatsapp_onboarding": {"status": "online"}}, True)
    assert status == "pending" and reason == "whatsapp_legacy_runtime_verification_required"


@pytest.mark.parametrize("expired", [False, True])
def test_provider_summary_uses_exact_sender_metadata_even_when_expired(environment, expired):
    env = environment
    configured_sandbox(env)
    env.sender.phone_number = "+15551234567"  # Deliberately not the Meta sample number.
    if expired:
        envelope = env.connection.config[vault.PRIVATE_CONFIG_KEY]
        env.connection.config = {vault.PRIVATE_CONFIG_KEY: {**envelope, "expires_at": 1}}
    db.session.commit()
    payload = report.build_channel_activation_payload(env.tenant)
    result = payload["whatsapp_connection"]
    assert result["contract_version"] == "tenant.whatsapp_connection_summary.v1"
    assert result["provider"] == "meta" and result["environment"] == "sandbox"
    assert result["display_phone_number"] == "+15551234567"
    assert result["configuration_status"] == ("expired" if expired else "configured")
    assert result["production_ready"] is False and result["conversation_verified"] is False
    assert result["counts"] == {"sandbox_registered": 1, "production_registered": 0}
    assert TOKEN not in json.dumps(result) and SECRET not in json.dumps(result)
    assert "credentials_ref" not in result and "recipients" not in result


def test_provider_summary_is_tenant_scoped_and_does_not_guess_phone_or_legacy_provider(environment):
    env = environment
    configured_sandbox(env)
    env.sender.phone_number = "+15551234567"
    other_owner = User(name="Synthetic other owner", email="other@example.invalid", rol="admin",
                       password_hash="synthetic-not-a-login", tipo_chat="pyme")
    db.session.add(other_owner)
    db.session.flush()
    other = TenantProfile(id=47, slug="another-organization", nombre="Synthetic organization",
                          tipo="pyme", pyme_id=other_owner.id, plan="full")
    db.session.add(other)
    db.session.flush()
    connection = ProviderConnection(tenant_id=47, provider="meta", channel="whatsapp",
        environment="production", status="connected")
    db.session.add(connection)
    db.session.flush()
    db.session.add(ProviderSender(tenant_id=47, provider_connection_id=connection.id,
        channel="whatsapp", phone_number="unexpected-secret-marker", status="connected"))
    db.session.commit()
    scoped = report._whatsapp_connection_summary(other, {}, reason_code="connect_whatsapp")
    assert scoped["counts"] == {"sandbox_registered": 0, "production_registered": 1}
    assert scoped["environment"] == "production" and scoped["configuration_status"] == "unverified"
    assert scoped["display_phone_number"] is None
    assert "+15551234567" not in json.dumps(scoped) and "unexpected-secret-marker" not in json.dumps(scoped)
    assert report._whatsapp_connection_summary(None, {}, reason_code="connect_whatsapp")["provider"] is None
    assert report._whatsapp_status({"whatsapp_onboarding": {"provider": {"secret": TOKEN}}}, True)[1] == []


@pytest.mark.parametrize("security,reason", [("blocked", "turnstile_enforced_missing_config"),
                                           ("ready", "survey_public_flow_verification_required")])
def test_existing_surveys_do_not_prove_public_flow_or_metrics(security, reason):
    status, evidence, code, _ = report._survey_status({"surveys": 5}, security,
        None if security == "ready" else reason)
    assert status == "pending" and code == reason
    assert "5 encuestas/votaciones registradas" in evidence
    assert report._survey_status({}, "ready", None)[0] == "action_required"


def test_server_turnstile_configuration_does_not_infer_missing_frontend_key(environment, monkeypatch):
    for name in ("VITE_CLOUDFLARE_TURNSTILE_SITE_KEY", "NEXT_PUBLIC_CLOUDFLARE_TURNSTILE_SITE_KEY",
                 "CLOUDFLARE_TURNSTILE_SITE_KEY", "CLOUDFLARE_TURNSTILE_SECRET_KEY", "TURNSTILE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    environment.app.config.update(CLOUDFLARE_TURNSTILE_SECRET_KEY="synthetic-secret-never-expose",
                                 CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE=True)
    status, evidence, _, hint = report._public_intake_security_status()
    assert status == "ready"  # Server configuration, explicitly not frontend acceptance.
    assert "clave publica del frontend no evaluada desde el servidor" in evidence
    assert "frontend publicado" in hint and "synthetic-secret-never-expose" not in str((evidence, hint))


def test_high_configuration_percentage_never_declares_operational_readiness(environment, monkeypatch):
    configured_sandbox(environment)
    original = report._channel
    def configured_channels(channel_id, label, status, description, **kwargs):
        return original(channel_id, label, "blocked" if channel_id == "public_intake_security" else "ready",
                        description, **kwargs)
    monkeypatch.setattr(report, "_channel", configured_channels)
    payload = report.build_channel_activation_payload(environment.tenant)
    assert payload["summary"]["progress"] >= 85
    assert "Listo para operar" not in payload["summary"]["health_label"]
    assert payload["summary"]["progress_scope"] == "configuration_checklist"
    assert payload["summary"]["production_ready"] is False


def test_declared_domain_is_not_verified_or_required_for_chatboc_branding(environment):
    env = environment
    env.tenant.logo_url = "https://example.invalid/logo.svg"
    env.tenant.tema = {"primaryColor": "#111111"}
    env.tenant.dominio = "unverified.example.invalid"
    status, evidence, _, hint = report._institutional_branding_status(env.tenant)
    assert status == "ready"
    assert "dominio declarado; activacion DNS/HTTPS no inferida" in evidence
    assert "dominio propio es opcional" in hint
    assert "unverified.example.invalid" not in " ".join(evidence)
