from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import db
from models import (
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    MessageTemplateRegistry,
    MunicipioTicket,
    Order,
    OrderItem,
    ProviderSender,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    User,
    WhatsAppFlowInteraction,
)
from services.meta_flow_data_exchange import (
    MetaFlowActionError,
    MetaFlowConfigurationError,
    MetaFlowRequestContext,
)
from services.meta_flow_runtime import (
    CLAIM_FLOW_ID,
    ORDER_FLOW_ID,
    SURVEY_FLOW_ID,
    MetaFlowRuntime,
    authorize_order_context,
    authorize_survey_context,
)
from services.whatsapp_flow_security import (
    WhatsAppFlowTokenError,
    issue_whatsapp_flow_token,
)


PRIVATE_KEY_ESCAPED = (
    "-----BEGIN PRIVATE KEY-----\\n"
    "test-private-key-material\\n"
    "-----END PRIVATE KEY-----"
)


def _env_for(waba_id: str, *, explicit: bool = False) -> dict[str, str]:
    if explicit:
        return {
            "TEST_FLOW_PRIVATE_KEY": PRIVATE_KEY_ESCAPED,
            "TEST_FLOW_PASSPHRASE": "passphrase",
            "TEST_FLOW_APP_SECRET": "app-secret-value",
            "TEST_FLOW_PREVIOUS_APP_SECRET": "previous-app-secret-value",
            "TEST_FLOW_TOKEN_KEY": "token-key-value-with-more-than-thirty-two-bytes",
        }
    suffix = "".join(character if character.isalnum() else "_" for character in waba_id).upper()
    prefix = f"META_FLOW_WABA_{suffix}"
    return {
        f"{prefix}_PRIVATE_KEY_PEM": PRIVATE_KEY_ESCAPED,
        f"{prefix}_APP_SECRET": "app-secret-value",
        f"{prefix}_FLOW_TOKEN_KEY_V1": "token-key-value-with-more-than-thirty-two-bytes",
    }


def _owner(email: str, *, tipo_chat: str) -> User:
    user = User(email=email, name=email.split("@", 1)[0], rol="tenant_admin", tipo_chat=tipo_chat)
    user.set_password("test-pass")
    db.session.add(user)
    db.session.flush()
    return user


def _tenant_with_sender(
    *,
    slug: str,
    waba_id: str,
    endpoint_alias: str,
    explicit_refs: bool = False,
) -> tuple[TenantProfile, ProviderSender]:
    owner = _owner(f"{slug}@example.test", tipo_chat="municipio")
    data_exchange = {
        "endpoint_aliases": [endpoint_alias],
    }
    if explicit_refs:
        data_exchange.update(
            {
                "private_key_ref": "env:TEST_FLOW_PRIVATE_KEY",
                "private_key_passphrase_ref": {"env": "TEST_FLOW_PASSPHRASE"},
                "app_secret_ref": "TEST_FLOW_APP_SECRET",
                "previous_app_secret_ref": "env:TEST_FLOW_PREVIOUS_APP_SECRET",
                "flow_token_key_ref": "env:TEST_FLOW_TOKEN_KEY",
            }
        )
    tenant = TenantProfile(
        slug=slug,
        nombre=slug.title(),
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
        is_active=True,
        configuracion={"meta_platform": {"data_exchange": data_exchange}},
    )
    db.session.add(tenant)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        channel="whatsapp",
        phone_number=f"+1555{tenant.id:07d}",
        sender_id=f"whatsapp:+1555{tenant.id:07d}",
        waba_id=waba_id,
        status="active",
        metadata_json={"meta_flow_data_exchange_endpoint_id": endpoint_alias},
    )
    db.session.add(sender)
    db.session.commit()
    return tenant, sender


def _verified(
    *,
    tenant_id: int,
    sender_id: int,
    flow_id: str,
    interaction_id: int = 101,
):
    def verify(token, **scope):
        assert token == "signed-flow-token"
        assert scope["tenant_id"] == tenant_id
        assert flow_id in scope["allowed_flow_ids"]
        assert "secret" in scope
        interaction = db.session.get(WhatsAppFlowInteraction, interaction_id)
        if interaction is None:
            registry = MessageTemplateRegistry(
                tenant_id=tenant_id,
                provider="twilio",
                channel="whatsapp",
                name=f"runtime-{flow_id}-{interaction_id}",
                language="es",
                status="approved",
                content_sid=f"HXRUNTIME{interaction_id}",
                external_template_id="123456789012345",
            )
            db.session.add(registry)
            db.session.flush()
            interaction = WhatsAppFlowInteraction(
                id=interaction_id,
                tenant_id=tenant_id,
                template_registry_id=registry.id,
                provider_sender_id=sender_id,
                flow_id=flow_id,
                meta_flow_id="123456789012345",
                content_sid=registry.content_sid,
                recipient_hash="a" * 64,
                recipient_hint="***1234",
                token_digest=f"{interaction_id:064x}"[-64:],
                idempotency_key=f"runtime-verified-{flow_id}-{interaction_id}",
                status="sent",
                data_contract=[],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            db.session.add(interaction)
            db.session.commit()
        return {
            "interaction_id": interaction_id,
            "tenant_id": tenant_id,
            "provider_sender_id": sender_id,
            "flow_id": flow_id,
            "meta_flow_id": "123456789012345",
            "recipient_hash": "a" * 64,
            "data_contract": [],
        }

    return verify


def _context(config, action: str) -> MetaFlowRequestContext:
    return MetaFlowRequestContext(
        request_id="runtime-test-request",
        endpoint_id=config.endpoint_id,
        tenant_id=config.tenant_id,
        waba_id=config.waba_id,
        action=action,
    )


def _payload(*, action: str, screen: str | None, data: dict | None = None) -> dict:
    payload = {
        "version": "3.0",
        "action": action,
        "flow_token": "signed-flow-token",
        "data": data or {},
    }
    if screen is not None:
        payload["screen"] = screen
    return payload


def _flow_interaction(
    *,
    tenant: TenantProfile,
    sender: ProviderSender,
    metadata: dict,
    flow_id: str = ORDER_FLOW_ID,
) -> WhatsAppFlowInteraction:
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=f"{flow_id}-flow-{tenant.id}",
        language="es",
        status="approved",
        content_sid=f"HXORDER{tenant.id}",
        external_template_id="123456789012345",
    )
    db.session.add(registry)
    db.session.flush()
    interaction = WhatsAppFlowInteraction(
        tenant_id=tenant.id,
        template_registry_id=registry.id,
        provider_sender_id=sender.id,
        flow_id=flow_id,
        meta_flow_id="123456789012345",
        content_sid=registry.content_sid,
        recipient_hash=f"{tenant.id:064x}"[-64:],
        recipient_hint="***1234",
        token_digest=f"{tenant.id + 100:064x}"[-64:],
        idempotency_key=f"runtime-{flow_id}-{tenant.id}",
        status="sent",
        data_contract=["order_id"],
        metadata_json=metadata,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.session.add(interaction)
    db.session.commit()
    return interaction


def _published_quick_vote(tenant: TenantProfile, *, slug: str) -> EncEncuesta:
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug=slug,
        titulo="Prioridades del barrio",
        estado="publicada",
        tipo="votacion",
        es_votacion_envivo=True,
        mostrar_resultados_envivo=True,
        politica_unicidad="por_cookie",
    )
    question = EncPregunta(
        orden=1,
        tipo="opcion_unica",
        texto="Que mejora deberia priorizarse?",
        obligatoria=True,
    )
    question.opciones = [
        EncOpcion(orden=1, texto="Iluminacion"),
        EncOpcion(orden=2, texto="Arreglo de calles"),
    ]
    survey.preguntas = [question]
    db.session.add(survey)
    db.session.commit()
    return survey


def test_resolver_binds_alias_to_one_waba_and_tenant(client):
    first_tenant, first_sender = _tenant_with_sender(
        slug="runtime-junin",
        waba_id="waba-junin",
        endpoint_alias="junin-flow-endpoint",
    )
    second_tenant, _ = _tenant_with_sender(
        slug="runtime-commerce",
        waba_id="waba-commerce",
        endpoint_alias="commerce-flow-endpoint",
    )
    environ = {**_env_for("waba-junin"), **_env_for("waba-commerce")}
    runtime = MetaFlowRuntime(
        environ=environ,
        token_verifier=_verified(
            tenant_id=first_tenant.id,
            sender_id=first_sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )

    config = runtime.resolve("junin-flow-endpoint")

    assert config is not None
    assert config.tenant_id == str(first_tenant.id)
    assert config.waba_id == "waba-junin"
    assert config.tenant_id != str(second_tenant.id)
    assert runtime.resolve("unknown-endpoint") is None


def test_resolver_rejects_alias_collision_across_wabas(client):
    _, first_sender = _tenant_with_sender(
        slug="runtime-alias-one",
        waba_id="waba-one",
        endpoint_alias="shared-flow-endpoint",
    )
    _, second_sender = _tenant_with_sender(
        slug="runtime-alias-two",
        waba_id="waba-two",
        endpoint_alias="second-flow-endpoint",
    )
    second_sender.metadata_json = {
        "meta_flow_data_exchange_endpoint_aliases": ["shared-flow-endpoint"]
    }
    db.session.commit()
    runtime = MetaFlowRuntime(
        environ={**_env_for("waba-one"), **_env_for("waba-two")},
        token_verifier=lambda *args, **kwargs: {},
    )

    with pytest.raises(MetaFlowConfigurationError) as error:
        runtime.resolve("shared-flow-endpoint")

    assert error.value.code == "endpoint_scope_ambiguous"
    assert first_sender.tenant_id != second_sender.tenant_id


def test_secret_refs_and_deterministic_waba_fallback_are_normalized(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-secrets",
        waba_id="waba-secret",
        endpoint_alias="secret-flow-endpoint",
        explicit_refs=True,
    )
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-secret", explicit=True),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )

    config = runtime.resolve("secret-flow-endpoint")

    assert config is not None
    assert "\\n" not in config.private_key_pem
    assert "\n" in config.private_key_pem
    assert config.private_key_passphrase == "passphrase"
    assert config.app_secret == "app-secret-value"
    assert config.previous_app_secret == "previous-app-secret-value"
    rendered = repr(config)
    assert "app-secret-value" not in rendered
    assert "test-private-key-material" not in rendered


def test_global_flow_token_key_takes_precedence_to_match_send_and_webhook(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-global-token",
        waba_id="waba-global-token",
        endpoint_alias="global-token-endpoint",
    )
    captured = {}

    def verify(token, *, secret, tenant_id, allowed_flow_ids):
        captured["secret"] = secret
        return _verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=CLAIM_FLOW_ID,
        )(token, secret=secret, tenant_id=tenant_id, allowed_flow_ids=allowed_flow_ids)

    environ = {
        **_env_for("waba-global-token"),
        "WHATSAPP_FLOW_TOKEN_KEY_V1": "global-flow-token-key-with-more-than-thirty-two-bytes",
    }
    runtime = MetaFlowRuntime(environ=environ, token_verifier=verify)
    config = runtime.resolve("global-token-endpoint")
    assert config is not None

    config.handlers["init"](
        _payload(action="INIT", screen=None),
        _context(config, "init"),
    )

    assert captured["secret"] == environ["WHATSAPP_FLOW_TOKEN_KEY_V1"]


def test_authorize_order_context_is_tenant_bound(client):
    tenant, _ = _tenant_with_sender(
        slug="runtime-order-owner",
        waba_id="waba-order-owner",
        endpoint_alias="order-owner-endpoint",
    )
    other_tenant, _ = _tenant_with_sender(
        slug="runtime-order-other",
        waba_id="waba-order-other",
        endpoint_alias="order-other-endpoint",
    )
    order = Order(
        id="tenant-owned-order",
        tenant_id=tenant.id,
        status="created",
        total=100,
    )
    db.session.add(order)
    db.session.commit()

    assert authorize_order_context(
        tenant.id,
        {"kind": "orders", "id": order.id},
    ) == {"kind": "order", "id": order.id}
    with pytest.raises(MetaFlowActionError) as error:
        authorize_order_context(
            other_tenant.id,
            {"kind": "order", "id": order.id},
        )
    assert error.value.code == "order_context_unavailable"


def test_authorize_survey_context_is_published_tenant_bound_and_native_compatible(client):
    tenant, _ = _tenant_with_sender(
        slug="runtime-survey-owner",
        waba_id="waba-survey-owner",
        endpoint_alias="survey-owner-endpoint",
    )
    other_tenant, _ = _tenant_with_sender(
        slug="runtime-survey-other",
        waba_id="waba-survey-other",
        endpoint_alias="survey-other-endpoint",
    )
    survey = _published_quick_vote(tenant, slug="prioridades-barrio")

    assert authorize_survey_context(
        tenant.id,
        {"survey_slug": survey.slug},
    ) == {
        "id": str(survey.id),
        "slug": survey.slug,
        "instrument_revision": 1,
    }
    with pytest.raises(MetaFlowActionError) as cross_tenant:
        authorize_survey_context(
            other_tenant.id,
            {"survey_slug": survey.slug},
        )
    assert cross_tenant.value.code == "survey_context_unavailable"

    survey.preguntas[0].tipo = "abierta"
    db.session.commit()
    with pytest.raises(MetaFlowActionError) as incompatible:
        authorize_survey_context(
            tenant.id,
            {"survey_slug": survey.slug},
        )
    assert incompatible.value.code == "survey_question_type_unsupported"


def test_authorize_survey_context_accepts_valid_adaptive_survey(client):
    tenant, _ = _tenant_with_sender(
        slug="runtime-conditional-survey",
        waba_id="waba-conditional-survey",
        endpoint_alias="conditional-survey-endpoint",
    )
    survey = _published_quick_vote(tenant, slug="conditional-web-only")
    conditional = EncPregunta(
        orden=2,
        tipo="opcion_unica",
        texto="Pregunta de seguimiento",
        obligatoria=True,
        logica_condicional={
            "version": 1,
            "show_if": {"question_order": 1, "option_order": 1},
        },
    )
    conditional.opciones = [
        EncOpcion(orden=1, texto="Si"),
        EncOpcion(orden=2, texto="No"),
    ]
    survey.preguntas.append(conditional)
    db.session.commit()

    assert authorize_survey_context(
        tenant.id,
        {"survey_slug": survey.slug},
    ) == {
        "id": str(survey.id),
        "slug": survey.slug,
        "instrument_revision": 1,
    }


@pytest.mark.parametrize(
    ("policy", "anonymous", "expected_code"),
    [
        ("por_dni", True, "survey_identity_policy_requires_webview"),
        ("por_ip", True, "survey_identity_policy_requires_webview"),
        ("por_usuario", True, "survey_authentication_requires_webview"),
        ("por_cookie", False, "survey_authentication_requires_webview"),
    ],
)
def test_authorize_survey_context_gates_identity_policies_native_cannot_satisfy(
    client,
    policy,
    anonymous,
    expected_code,
):
    tenant, _ = _tenant_with_sender(
        slug=f"runtime-survey-policy-{policy}-{anonymous}",
        waba_id=f"waba-survey-policy-{policy}-{anonymous}",
        endpoint_alias=f"survey-policy-{policy}-{anonymous}",
    )
    survey = _published_quick_vote(tenant, slug=f"policy-{policy}-{anonymous}")
    survey.politica_unicidad = policy
    survey.anonimo_permitido = anonymous
    db.session.commit()

    with pytest.raises(MetaFlowActionError) as incompatible:
        authorize_survey_context(tenant.id, {"survey_slug": survey.slug})

    assert incompatible.value.code == expected_code
    assert incompatible.value.status_code == 409


def test_survey_runtime_hydrates_question_and_stages_valid_answer_without_voting(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-survey",
        waba_id="waba-survey",
        endpoint_alias="survey-flow-endpoint",
    )
    survey = _published_quick_vote(tenant, slug="runtime-quick-vote")
    interaction = _flow_interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={
            "survey_context": {"id": str(survey.id), "slug": survey.slug}
        },
    )
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-survey"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=SURVEY_FLOW_ID,
            interaction_id=interaction.id,
        ),
    )
    config = runtime.resolve("survey-flow-endpoint")
    assert config is not None

    initial = config.handlers["init"](
        _payload(action="INIT", screen=None),
        _context(config, "init"),
    )
    assert initial["screen"] == "SURVEY_QUESTION_ONE"
    assert initial["data"]["survey_title"] == survey.titulo
    assert initial["data"]["progress_label"] == "Pregunta 1 de 1"
    assert initial["data"]["options"] == [
        {"id": str(option.id), "title": option.texto}
        for option in survey.preguntas[0].opciones
    ]

    selected = survey.preguntas[0].opciones[0]
    confirmation = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="SURVEY_QUESTION_ONE",
            data={"selected_option": str(selected.id)},
        ),
        _context(config, "data_exchange"),
    )
    db.session.refresh(interaction)
    assert confirmation["screen"] == "SURVEY_CONFIRM"
    assert confirmation["data"]["answer_summary"] == "1 respuesta lista para enviar."
    assert interaction.metadata_json["survey_staged_answers"] == {
        str(survey.preguntas[0].id): selected.id
    }
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0


def test_survey_runtime_rejects_restricted_eligibility_before_staging(
    client, monkeypatch
):
    tenant, sender = _tenant_with_sender(
        slug="runtime-survey-restricted",
        waba_id="waba-survey-restricted",
        endpoint_alias="survey-restricted-endpoint",
    )
    survey = _published_quick_vote(tenant, slug="runtime-restricted-vote")
    original_metadata = {
        "survey_context": {"id": str(survey.id), "slug": survey.slug},
        "correlation_marker": "must-remain-unchanged",
    }
    interaction = _flow_interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata=original_metadata,
    )
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-survey-restricted"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=SURVEY_FLOW_ID,
            interaction_id=interaction.id,
        ),
    )
    config = runtime.resolve("survey-restricted-endpoint")
    assert config is not None

    monkeypatch.setattr(
        "services.survey_governance.survey_governance_contract",
        lambda *_args, **_kwargs: {
            "eligibility": {
                "credential_required": True,
                "transport": "http_header_only",
            }
        },
    )

    with pytest.raises(MetaFlowActionError) as blocked:
        config.handlers["data_exchange"](
            _payload(
                action="data_exchange",
                screen="SURVEY_QUESTION_ONE",
                data={
                    "selected_option": str(survey.preguntas[0].opciones[0].id)
                },
            ),
            _context(config, "data_exchange"),
        )

    assert blocked.value.code == "survey_eligibility_transport_unsupported"
    assert blocked.value.status_code == 409
    db.session.expire_all()
    reloaded = db.session.get(WhatsAppFlowInteraction, interaction.id)
    assert reloaded is not None
    assert reloaded.metadata_json == original_metadata
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0


def test_survey_runtime_adapts_forward_and_back_and_prunes_hidden_branch(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-survey-adaptive",
        waba_id="waba-survey-adaptive",
        endpoint_alias="survey-adaptive-endpoint",
    )
    survey = _published_quick_vote(tenant, slug="runtime-adaptive-vote")
    follow_up = EncPregunta(
        orden=2,
        tipo="opcion_unica",
        texto="Que zona necesita esa mejora?",
        obligatoria=True,
        logica_condicional={
            "version": 1,
            "show_if": {"question_order": 1, "option_order": 1},
        },
    )
    follow_up.opciones = [
        EncOpcion(orden=1, texto="Centro"),
        EncOpcion(orden=2, texto="Barrios"),
    ]
    survey.preguntas.append(follow_up)
    db.session.commit()
    survey_context = authorize_survey_context(
        tenant.id,
        {"survey_slug": survey.slug},
    )
    interaction = _flow_interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={"survey_context": survey_context},
    )
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-survey-adaptive"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=SURVEY_FLOW_ID,
            interaction_id=interaction.id,
        ),
    )
    config = runtime.resolve("survey-adaptive-endpoint")
    assert config is not None

    initial = config.handlers["init"](
        _payload(action="INIT", screen=None),
        _context(config, "init"),
    )
    assert initial["screen"] == "SURVEY_QUESTION_ONE"
    assert initial["data"]["progress_label"] == "Pregunta 1"

    branch_answer = survey.preguntas[0].opciones[0]
    second = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="SURVEY_QUESTION_ONE",
            data={"selected_option": str(branch_answer.id)},
        ),
        _context(config, "data_exchange"),
    )
    assert second["screen"] == "SURVEY_QUESTION_TWO"
    assert second["data"]["question_text"] == follow_up.texto
    assert second["data"]["progress_label"] == "Pregunta 2 - 1 respuesta guardada"

    follow_up_answer = follow_up.opciones[0]
    confirmation = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="SURVEY_QUESTION_TWO",
            data={"selected_option": str(follow_up_answer.id)},
        ),
        _context(config, "data_exchange"),
    )
    assert confirmation["screen"] == "SURVEY_CONFIRM"
    assert confirmation["data"]["answer_summary"] == "2 respuestas listas para enviar."

    previous = config.handlers["back"](
        _payload(action="BACK", screen="SURVEY_CONFIRM"),
        _context(config, "back"),
    )
    assert previous["screen"] == "SURVEY_QUESTION_TWO"
    first_again = config.handlers["back"](
        _payload(action="BACK", screen="SURVEY_QUESTION_TWO"),
        _context(config, "back"),
    )
    assert first_again["screen"] == "SURVEY_QUESTION_ONE"

    skip_answer = survey.preguntas[0].opciones[1]
    skipped = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="SURVEY_QUESTION_ONE",
            data={"selected_option": str(skip_answer.id)},
        ),
        _context(config, "data_exchange"),
    )
    db.session.refresh(interaction)
    assert skipped["screen"] == "SURVEY_CONFIRM"
    assert skipped["data"]["answer_summary"] == "1 respuesta lista para enviar."
    assert interaction.metadata_json["survey_staged_answers"] == {
        str(survey.preguntas[0].id): skip_answer.id,
    }
    assert interaction.metadata_json["survey_navigation"] == {
        "instrument_revision": 1,
        "visible_question_ids": [survey.preguntas[0].id],
        "answered_question_ids": [survey.preguntas[0].id],
    }

    unpinned_metadata = dict(interaction.metadata_json)
    unpinned_context = dict(unpinned_metadata["survey_context"])
    unpinned_context.pop("instrument_revision")
    unpinned_metadata["survey_context"] = unpinned_context
    interaction.metadata_json = unpinned_metadata
    db.session.commit()
    with pytest.raises(MetaFlowActionError) as unpinned:
        config.handlers["init"](
            _payload(action="INIT", screen=None),
            _context(config, "init"),
        )
    assert unpinned.value.code == "survey_instrument_revision_required"


def test_survey_runtime_rejects_instrument_changed_after_send(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-survey-revision",
        waba_id="waba-survey-revision",
        endpoint_alias="survey-revision-endpoint",
    )
    survey = _published_quick_vote(tenant, slug="runtime-revision-vote")
    context = authorize_survey_context(tenant.id, {"survey_slug": survey.slug})
    interaction = _flow_interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={"survey_context": context},
    )
    survey.structure_revision = 2
    db.session.commit()
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-survey-revision"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=SURVEY_FLOW_ID,
            interaction_id=interaction.id,
        ),
    )
    config = runtime.resolve("survey-revision-endpoint")
    assert config is not None

    with pytest.raises(MetaFlowActionError) as stale:
        config.handlers["init"](
            _payload(action="INIT", screen=None),
            _context(config, "init"),
        )

    assert stale.value.code == "survey_structure_changed"
    assert stale.value.status_code == 409


def test_survey_runtime_rejects_option_from_another_question(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-survey-option-scope",
        waba_id="waba-survey-option-scope",
        endpoint_alias="survey-option-scope-endpoint",
    )
    survey = _published_quick_vote(tenant, slug="runtime-option-scope")
    other = _published_quick_vote(tenant, slug="runtime-other-option")
    interaction = _flow_interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={"survey_context": {"id": str(survey.id), "slug": survey.slug}},
    )
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-survey-option-scope"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=SURVEY_FLOW_ID,
            interaction_id=interaction.id,
        ),
    )
    config = runtime.resolve("survey-option-scope-endpoint")
    assert config is not None

    with pytest.raises(MetaFlowActionError) as error:
        config.handlers["data_exchange"](
            _payload(
                action="data_exchange",
                screen="SURVEY_QUESTION_ONE",
                data={"selected_option": str(other.preguntas[0].opciones[0].id)},
            ),
            _context(config, "data_exchange"),
        )
    assert error.value.code == "survey_option_invalid"
    assert EncRespuesta.query.count() == 0


def test_invalid_secret_reference_fails_closed(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-bad-secret-ref",
        waba_id="waba-bad-ref",
        endpoint_alias="bad-ref-endpoint",
        explicit_refs=True,
    )
    current = tenant.configuracion
    tenant.configuracion = {
        **current,
        "meta_platform": {
            **current["meta_platform"],
            "data_exchange": {
                **current["meta_platform"]["data_exchange"],
                "app_secret_ref": "../../secret",
            },
        },
    }
    db.session.commit()
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-bad-ref", explicit=True),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )

    with pytest.raises(MetaFlowConfigurationError) as error:
        runtime.resolve("bad-ref-endpoint")

    assert error.value.code == "secret_reference_invalid"


def test_invalid_flow_token_fails_closed_before_claim_lookup(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-token",
        waba_id="waba-token",
        endpoint_alias="token-flow-endpoint",
    )

    def reject(*args, **kwargs):
        raise WhatsAppFlowTokenError("invalid_flow_token")

    runtime = MetaFlowRuntime(environ=_env_for("waba-token"), token_verifier=reject)
    config = runtime.resolve("token-flow-endpoint")
    assert config is not None
    handler = config.handlers["data_exchange"]

    with pytest.raises(MetaFlowActionError) as error:
        handler(
            _payload(
                action="data_exchange",
                screen="CLAIM_LOOKUP",
                data={"ticket_number": "M-100001", "access_pin": "900144"},
            ),
            _context(config, "data_exchange"),
        )

    assert error.value.code == "invalid_flow_token"
    assert error.value.status_code == 427
    assert error.value.safe_message == "This message is no longer available."
    assert MunicipioTicket.query.count() == 0


def test_verified_token_sender_must_belong_to_resolved_waba(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-token-sender",
        waba_id="waba-token-sender",
        endpoint_alias="token-sender-endpoint",
    )
    _, other_sender = _tenant_with_sender(
        slug="runtime-other-token-sender",
        waba_id="waba-other-token-sender",
        endpoint_alias="other-token-sender-endpoint",
    )
    runtime = MetaFlowRuntime(
        environ={
            **_env_for("waba-token-sender"),
            **_env_for("waba-other-token-sender"),
        },
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=other_sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )
    config = runtime.resolve("token-sender-endpoint")
    assert config is not None

    with pytest.raises(MetaFlowActionError) as error:
        config.handlers["init"](
            _payload(action="INIT", screen=None),
            _context(config, "init"),
        )

    assert error.value.code == "flow_token_scope_invalid"
    assert error.value.status_code == 427
    assert error.value.safe_message == "This message is no longer available."
    assert sender.id != other_sender.id


def test_missing_endpoint_token_verifier_is_explicit_and_fail_closed(client):
    tenant, _ = _tenant_with_sender(
        slug="runtime-no-verifier",
        waba_id="waba-no-verifier",
        endpoint_alias="no-verifier-endpoint",
    )
    runtime = MetaFlowRuntime(environ=_env_for("waba-no-verifier"))
    runtime._token_verifier = None
    config = runtime.resolve("no-verifier-endpoint")
    assert config is not None

    with pytest.raises(MetaFlowActionError) as error:
        config.handlers["init"](
            _payload(action="INIT", screen=None),
            _context(config, "init"),
        )

    assert error.value.code == "flow_token_endpoint_verifier_unavailable"
    assert error.value.status_code == 503
    assert tenant.is_active is True


def test_runtime_uses_real_endpoint_safe_token_verifier(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-real-verifier",
        waba_id="waba-real-verifier",
        endpoint_alias="real-verifier-endpoint",
    )
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        nro_ticket="500005",
        consulta_pin="556677",
        pregunta="Reclamo verificado",
        categoria="Alumbrado",
        estado="nuevo",
    )
    db.session.add(ticket)
    db.session.flush()
    env = _env_for("waba-real-verifier")
    token_secret = env["META_FLOW_WABA_WABA_REAL_VERIFIER_FLOW_TOKEN_KEY_V1"]
    issued = issue_whatsapp_flow_token(
        secret=token_secret,
        tenant_id=tenant.id,
        recipient="+5491112345678",
        flow_id=CLAIM_FLOW_ID,
        meta_flow_id="123456789012345",
        provider_sender_id=sender.id,
    )
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="claim-flow-real-verifier",
        language="es",
        status="approved",
        content_sid="HXCLAIMREAL",
        external_template_id="123456789012345",
    )
    db.session.add(registry)
    db.session.flush()
    interaction = WhatsAppFlowInteraction(
        tenant_id=tenant.id,
        template_registry_id=registry.id,
        provider_sender_id=sender.id,
        flow_id=CLAIM_FLOW_ID,
        meta_flow_id="123456789012345",
        content_sid=registry.content_sid,
        recipient_hash=issued.recipient_hash,
        recipient_hint=issued.recipient_hint,
        token_digest=issued.token_digest,
        idempotency_key="runtime-real-token-001",
        status="sent",
        data_contract=["ticket_number", "access_pin"],
        expires_at=issued.expires_at,
    )
    db.session.add(interaction)
    db.session.commit()
    runtime = MetaFlowRuntime(environ=env)
    assert runtime.token_verifier_ready is True
    config = runtime.resolve("real-verifier-endpoint")
    assert config is not None
    payload = _payload(
        action="data_exchange",
        screen="CLAIM_LOOKUP",
        data={"ticket_number": "M-500005", "access_pin": "556677"},
    )
    payload["flow_token"] = issued.token

    response = config.handlers["data_exchange"](
        payload,
        _context(config, "data_exchange"),
    )

    db.session.refresh(interaction)
    assert response["screen"] == "CLAIM_RESULT"
    assert response["data"]["status"] == "Recibido"
    assert interaction.consumed_at is None
    assert interaction.metadata_json["claim_context"] == {
        "kind": "municipio",
        "id": str(ticket.id),
        "ticket_number": "M-500005",
    }


def test_claim_lookup_is_tenant_bound_and_returns_no_pii(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-claim",
        waba_id="waba-claim",
        endpoint_alias="claim-flow-endpoint",
    )
    other_tenant, _ = _tenant_with_sender(
        slug="runtime-other-claim",
        waba_id="waba-other-claim",
        endpoint_alias="other-claim-endpoint",
    )
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        nro_ticket="100001",
        consulta_pin="900144",
        pregunta="La luminaria frente a mi domicilio no funciona",
        categoria="Luminaria",
        estado="en_proceso",
        nombre_vecino="Persona Privada",
        telefono_vecino="+5491111111111",
        direccion="Calle privada 123",
    )
    other_ticket = MunicipioTicket(
        tenant_id=other_tenant.id,
        municipio_id=other_tenant.municipio_id,
        nro_ticket="200002",
        consulta_pin="900144",
        pregunta="Otro reclamo",
        categoria="Arbolado",
    )
    db.session.add_all([ticket, other_ticket])
    db.session.commit()
    runtime = MetaFlowRuntime(
        environ={**_env_for("waba-claim"), **_env_for("waba-other-claim")},
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )
    config = runtime.resolve("claim-flow-endpoint")
    assert config is not None
    before = (ticket.estado, MunicipioTicket.query.count())

    response = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="CLAIM_LOOKUP",
            data={"ticket_number": "M-100001", "access_pin": "900144"},
        ),
        _context(config, "data_exchange"),
    )

    serialized = str(response)
    assert response["screen"] == "CLAIM_RESULT"
    assert response["data"]["status"] == "En proceso"
    assert response["data"]["summary"] == "Tu reclamo continua en estado en proceso."
    assert "Persona Privada" not in serialized
    assert "+5491111111111" not in serialized
    assert "Calle privada" not in serialized
    assert (ticket.estado, MunicipioTicket.query.count()) == before
    interaction = db.session.get(WhatsAppFlowInteraction, 101)
    assert interaction.metadata_json["claim_context"] == {
        "kind": "municipio",
        "id": str(ticket.id),
        "ticket_number": "M-100001",
    }

    with pytest.raises(MetaFlowActionError) as error:
        config.handlers["data_exchange"](
            _payload(
                action="data_exchange",
                screen="CLAIM_LOOKUP",
                data={"ticket_number": "M-200002", "access_pin": "900144"},
            ),
            _context(config, "data_exchange"),
        )
    assert error.value.code == "claim_not_found"


def test_claim_lookup_supports_tenant_and_pyme_tickets_with_tenant_scope(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-claim-models",
        waba_id="waba-claim-models",
        endpoint_alias="claim-models-endpoint",
    )
    pyme_ticket = PymeTicket(
        tenant_id=tenant.id,
        nro_ticket=300003,
        consulta_pin="112233",
        pregunta="Consulta de entrega",
        categoria="Entrega",
    )
    tenant_ticket = TenantTicket(
        tenant_id=tenant.id,
        descripcion="Consulta general",
        categoria="General",
        datos_extra={"ticket_number": "T-77", "access_pin": "778899"},
    )
    db.session.add_all([pyme_ticket, tenant_ticket])
    db.session.commit()
    tenant_ticket.datos_extra = {"ticket_number": f"T-{tenant_ticket.id}", "access_pin": "778899"}
    db.session.commit()
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-claim-models"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )
    config = runtime.resolve("claim-models-endpoint")
    assert config is not None

    pyme_response = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="CLAIM_LOOKUP",
            data={"ticket_number": "P-300003", "access_pin": "112233"},
        ),
        _context(config, "data_exchange"),
    )
    tenant_response = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="CLAIM_LOOKUP",
            data={"ticket_number": f"T-{tenant_ticket.id}", "access_pin": "778899"},
        ),
        _context(config, "data_exchange"),
    )

    assert pyme_response["screen"] == "CLAIM_RESULT"
    assert pyme_response["data"]["summary"] == "Tu reclamo continua en estado recibido."
    assert tenant_response["screen"] == "CLAIM_RESULT"
    assert tenant_response["data"]["summary"] == "Tu reclamo continua en estado recibido."


def test_claim_prefix_selects_one_tenant_bound_ticket_model(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-claim-prefix",
        waba_id="waba-claim-prefix",
        endpoint_alias="claim-prefix-endpoint",
    )
    municipal = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        nro_ticket="440044",
        consulta_pin="445566",
        pregunta="Reclamo municipal",
        estado="en_proceso",
    )
    pyme = PymeTicket(
        tenant_id=tenant.id,
        nro_ticket=440044,
        consulta_pin="445566",
        pregunta="Consulta comercial",
        estado="resuelto",
    )
    db.session.add_all([municipal, pyme])
    db.session.commit()
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-claim-prefix"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )
    config = runtime.resolve("claim-prefix-endpoint")
    assert config is not None

    municipal_response = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="CLAIM_LOOKUP",
            data={"ticket_number": "M-440044", "access_pin": "445566"},
        ),
        _context(config, "data_exchange"),
    )
    pyme_response = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="CLAIM_LOOKUP",
            data={"ticket_number": "P-440044", "access_pin": "445566"},
        ),
        _context(config, "data_exchange"),
    )

    assert municipal_response["data"]["status"] == "En proceso"
    assert pyme_response["data"]["status"] == "Resuelto"


def test_data_exchange_rejects_result_screen_as_an_input_source(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-screen-source",
        waba_id="waba-screen-source",
        endpoint_alias="screen-source-endpoint",
    )
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-screen-source"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )
    config = runtime.resolve("screen-source-endpoint")
    assert config is not None

    with pytest.raises(MetaFlowActionError) as error:
        config.handlers["data_exchange"](
            _payload(
                action="data_exchange",
                screen="CLAIM_RESULT",
                data={"ticket_number": "M-100001", "access_pin": "900144"},
            ),
            _context(config, "data_exchange"),
        )

    assert error.value.code == "flow_screen_invalid"
    assert error.value.status_code == 400


def test_order_confirmation_uses_server_context_and_has_no_side_effects(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-order",
        waba_id="waba-order",
        endpoint_alias="order-flow-endpoint",
    )
    order = Order(
        id="order-authoritative-001",
        tenant_id=tenant.id,
        status="confirmed",
        channel="whatsapp",
        currency="ARS",
        subtotal=25000,
        total=25000,
    )
    order.items = [
        OrderItem(title="Clavos", quantity=2, unit_price=5000, total_price=10000),
        OrderItem(title="Chapas", quantity=1, unit_price=15000, total_price=15000),
    ]
    db.session.add(order)
    db.session.commit()
    interaction = _flow_interaction(
        tenant=tenant,
        sender=sender,
        metadata={"order_context": {"kind": "order", "id": order.id}},
    )
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-order"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=ORDER_FLOW_ID,
            interaction_id=interaction.id,
        ),
    )
    config = runtime.resolve("order-flow-endpoint")
    assert config is not None
    before = SimpleNamespace(
        status=order.status,
        orders=Order.query.count(),
        interactions=WhatsAppFlowInteraction.query.count(),
        consumed_at=interaction.consumed_at,
    )

    response = config.handlers["data_exchange"](
        _payload(
            action="data_exchange",
            screen="ORDER_DETAILS",
            data={
                "full_name": "Cliente",
                "phone": "+5491112345678",
                "delivery_address": "Direccion informada por el cliente",
                "delivery_notes": "Entregar por la tarde",
                "order_id": "attacker-order",
                "amount": "1",
                "products": ["Producto falso"],
            },
        ),
        _context(config, "data_exchange"),
    )

    db.session.refresh(order)
    db.session.refresh(interaction)
    assert response == {
        "screen": "ORDER_CONFIRM",
        "data": {
            "order_summary": "3 productos en 2 renglones",
            "total_display": "$ 25.000,00",
        },
    }
    assert order.status == before.status
    assert Order.query.count() == before.orders
    assert WhatsAppFlowInteraction.query.count() == before.interactions
    assert interaction.consumed_at == before.consumed_at
    assert not db.session.new
    assert not db.session.deleted


def test_order_without_persisted_context_returns_safe_error(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-order-missing",
        waba_id="waba-order-missing",
        endpoint_alias="order-missing-endpoint",
    )
    interaction = _flow_interaction(tenant=tenant, sender=sender, metadata={})
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-order-missing"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=ORDER_FLOW_ID,
            interaction_id=interaction.id,
        ),
    )
    config = runtime.resolve("order-missing-endpoint")
    assert config is not None

    with pytest.raises(MetaFlowActionError) as error:
        config.handlers["data_exchange"](
            _payload(
                action="data_exchange",
                screen="ORDER_DETAILS",
                data={
                    "full_name": "Cliente",
                    "phone": "+5491112345678",
                    "delivery_address": "Calle 123",
                    "order_id": "client-controlled-order",
                    "amount": "999999",
                },
            ),
            _context(config, "data_exchange"),
        )

    assert error.value.code == "order_context_missing"
    assert error.value.status_code == 409


def test_init_back_and_error_handlers_return_only_flow_contract_data(client):
    tenant, sender = _tenant_with_sender(
        slug="runtime-actions",
        waba_id="waba-actions",
        endpoint_alias="actions-endpoint",
    )
    runtime = MetaFlowRuntime(
        environ=_env_for("waba-actions"),
        token_verifier=_verified(
            tenant_id=tenant.id,
            sender_id=sender.id,
            flow_id=CLAIM_FLOW_ID,
        ),
    )
    config = runtime.resolve("actions-endpoint")
    assert config is not None

    init_response = config.handlers["init"](
        _payload(action="INIT", screen=None),
        _context(config, "init"),
    )
    back_response = config.handlers["back"](
        _payload(action="BACK", screen="CLAIM_RESULT"),
        _context(config, "back"),
    )
    error_response = config.handlers["error"](
        {
            "version": "3.0",
            "action": "data_exchange",
            "flow_token": "signed-flow-token",
            "screen": "CLAIM_LOOKUP",
            "data": {"error": "client_render_failure", "sensitive": "not echoed"},
        },
        _context(config, "error"),
    )

    assert init_response == {"screen": "CLAIM_LOOKUP", "data": {}}
    assert back_response == {"screen": "CLAIM_LOOKUP", "data": {}}
    assert error_response == {"data": {"acknowledged": True}}
