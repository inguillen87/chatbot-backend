from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import uuid

import pytest

from database import db
from models import (
    AnalyticsEventV2,
    AuditEvent,
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    EncRespuestaDetalle,
    SurveyResponseEffect,
)
from services.encuestas_service import (
    EncuestaError,
    SURVEY_IDENTITY_FINGERPRINT_VERSION,
    SURVEY_PRIVACY_MODE_SOURCE_ANONYMOUS,
    _build_survey_response_analytics_event,
    build_unique_fingerprint,
    save_respuesta,
    serialize_public_encuesta,
    serialize_respuesta,
)
from services.survey_privacy import purge_expired_source_anonymous_responses
from services.encuestas_analytics_service import calculate_live_results


PRIVACY_VERSION = "privacy-2026-07"
PRIVACY_URL = "https://chatboc.ar/privacy/privacy-2026-07"
HMAC_SECRET = "survey-identity-test-secret-32-bytes-minimum"


def _source_anonymous_survey(*, policy: str = "por_cookie"):
    survey = EncEncuesta(
        tenant_id=4,
        slug=f"privacy-{uuid.uuid4().hex}",
        titulo="Participacion con anonimato de origen",
        tipo="opinion",
        estado="publicada",
        politica_unicidad=policy,
        anonimo_permitido=True,
        privacy_mode=SURVEY_PRIVACY_MODE_SOURCE_ANONYMOUS,
        privacy_policy_version=PRIVACY_VERSION,
        privacy_policy_url=PRIVACY_URL,
        privacy_consent_required=True,
        response_retention_days=365,
        puntos_recompensa=0,
    )
    question = EncPregunta(
        encuesta=survey,
        orden=1,
        logical_ref=f"q_{uuid.uuid4().hex}",
        tipo="opcion_unica",
        texto="Como evaluas el servicio?",
        obligatoria=True,
    )
    option = EncOpcion(
        pregunta=question,
        orden=1,
        logical_ref=f"o_{uuid.uuid4().hex}",
        texto="Muy bien",
        valor="muy_bien",
    )
    db.session.add_all([survey, question, option])
    db.session.commit()
    return survey, question, option


def _payload(question: EncPregunta, option: EncOpcion, **overrides):
    payload = {
        "dni": "32.876.543",
        "phone": "+54 9 236 400-0000",
        "privacy_consent": True,
        "privacy_policy_version": PRIVACY_VERSION,
        "utm_source": "personal-link",
        "utm_campaign": "citizen-123",
        "edad": 37,
        "anio_nacimiento": 1989,
        "respuestas": [
            {"pregunta_id": question.id, "opcion_ids": [option.id]},
        ],
        "metadata": {
            "contact_email": "citizen@example.com",
            "demographics": {
                "rangoEtario": "35-44",
                "ubicacion": {
                    "lat": -34.585,
                    "lng": -60.949,
                    "barrio": "Centro",
                    "ciudad": "Junin",
                    "provincia": "Buenos Aires",
                    "pais": "Argentina",
                },
            },
        },
    }
    payload.update(overrides)
    return payload


def _request_context():
    return {
        "ip": "203.0.113.77",
        "user_agent": "privacy-test-browser/1.0",
        "anon_id": "stable-browser-cookie",
        "canal": "web",
    }


def test_source_anonymous_requires_explicit_current_consent(client):
    with client.application.app_context():
        survey, question, option = _source_anonymous_survey()
        payload = _payload(question, option)
        payload.pop("privacy_consent")

        with pytest.raises(EncuestaError) as exc_info:
            save_respuesta(
                survey.slug,
                payload,
                _request_context(),
                commit=False,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.payload["reason_code"] == "survey_privacy_consent_required"
        assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0


def test_source_anonymous_rejects_stale_policy_version(client):
    with client.application.app_context():
        survey, question, option = _source_anonymous_survey()

        with pytest.raises(EncuestaError) as exc_info:
            save_respuesta(
                survey.slug,
                _payload(
                    question,
                    option,
                    privacy_policy_version="privacy-old",
                ),
                _request_context(),
                commit=False,
            )

        assert exc_info.value.status_code == 409
        assert (
            exc_info.value.payload["reason_code"]
            == "survey_privacy_policy_version_mismatch"
        )
        assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0


def test_source_anonymous_uniqueness_fails_closed_without_hmac_secret(client, monkeypatch):
    with client.application.app_context():
        monkeypatch.setitem(
            client.application.config,
            "SURVEY_IDENTITY_HMAC_SECRET_V1",
            "too-short",
        )
        survey, question, option = _source_anonymous_survey()

        with pytest.raises(EncuestaError) as exc_info:
            save_respuesta(
                survey.slug,
                _payload(question, option),
                _request_context(),
                commit=False,
            )

        assert exc_info.value.status_code == 503
        assert (
            exc_info.value.payload["reason_code"]
            == "survey_identity_hmac_secret_unavailable"
        )
        assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0


def test_source_anonymous_discards_direct_identifiers_before_flush(client, monkeypatch):
    with client.application.app_context():
        monkeypatch.setitem(
            client.application.config,
            "SURVEY_IDENTITY_HMAC_SECRET_V1",
            HMAC_SECRET,
        )
        survey, question, option = _source_anonymous_survey()

        forged_client_time = datetime.now(timezone.utc) + timedelta(days=3650)
        payload = _payload(question, option)
        payload["metadata"]["submittedAt"] = forged_client_time.isoformat()
        server_before = datetime.now(timezone.utc)

        response = save_respuesta(
            survey.slug,
            payload,
            _request_context(),
            commit=False,
            emit_realtime_update=False,
        )
        server_after = datetime.now(timezone.utc)

        assert response.huella_unica.startswith(
            f"{SURVEY_IDENTITY_FINGERPRINT_VERSION}:"
        )
        digest = response.huella_unica.split(":", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert response.user_id is None
        assert response.dni is None
        assert response.phone is None
        assert response.ip is None
        assert response.ua is None
        assert response.lat is None
        assert response.lng is None
        assert response.utm_source is None
        assert response.utm_campaign is None
        assert response.metadata_payload is None
        assert response.edad is None
        assert response.anio_nacimiento is None
        assert response.rango_etario == "35-44"
        assert response.barrio == "Centro"
        assert response.ciudad == "Junin"
        assert response.privacy_mode == SURVEY_PRIVACY_MODE_SOURCE_ANONYMOUS
        assert response.privacy_policy_version == PRIVACY_VERSION
        assert response.privacy_consent_recorded_at is not None
        assert response.retention_expires_at is not None

        submitted = response.submitted_at
        expires = response.retention_expires_at
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        consent_recorded = response.privacy_consent_recorded_at
        if consent_recorded.tzinfo is None:
            consent_recorded = consent_recorded.replace(tzinfo=timezone.utc)
        assert server_before <= submitted <= server_after
        assert submitted != forged_client_time
        assert consent_recorded == submitted
        assert (expires - submitted).days == 365

        serialized = serialize_respuesta(response)
        assert serialized["privacy"]["source_identifiers_persisted"] is False
        assert serialized["dni"] is None
        assert serialized["metadata"] is None
        analytics_event = _build_survey_response_analytics_event(
            survey,
            response,
            slug_publico=survey.slug,
            respuestas_payload=_payload(question, option)["respuestas"],
        )
        assert analytics_event["user_id"] is None
        assert analytics_event["anon_id"] is None
        assert analytics_event["session_id"] is None
        db.session.rollback()


def test_source_anonymous_public_live_results_hide_a_small_cohort(client, monkeypatch):
    with client.application.app_context():
        monkeypatch.setitem(
            client.application.config,
            "SURVEY_IDENTITY_HMAC_SECRET_V1",
            HMAC_SECRET,
        )
        survey, question, option = _source_anonymous_survey()
        survey.mostrar_resultados_envivo = True
        db.session.commit()

        def _reject_private_ai_provider_call(*_args, **_kwargs):
            raise AssertionError(
                "small-cell source-anonymous aggregates reached an AI provider"
            )

        monkeypatch.setattr(
            "services.encuestas_analytics_service.build_collection_ai_insights",
            _reject_private_ai_provider_call,
        )
        monkeypatch.setattr(
            "services.encuestas_analytics_service.build_map_ai_layers",
            _reject_private_ai_provider_call,
        )

        save_respuesta(
            survey.slug,
            _payload(question, option),
            _request_context(),
            emit_realtime_update=False,
        )

        live = calculate_live_results(
            survey.slug,
            preferred_tenant_id=survey.tenant_id,
            include_heatmap=False,
        )

        assert live["privacy"]["contract_version"] == "surveys.public_small_cell.v1"
        assert live["privacy"]["detailed_results_suppressed"] is True
        assert live["privacy"]["minimum_cell_size"] == 5
        assert live["total_respuestas"] is None
        assert live["total_respuestas_bucket"] == "<5"
        assert live["preguntas"][0]["total_votos"] is None
        assert live["preguntas"][0]["opciones"][0]["votos"] is None
        assert live["timeline_minute"] == []
        assert live["result_version"] is None
        assert live["snapshot_version"].startswith("private:")
        assert live["ai_signal"]["mode"] == "privacy_suppressed"
        assert live["ai_signal"]["provider_family"] == "none"


def test_source_anonymous_hmac_is_tenant_survey_and_secret_bound(client, monkeypatch):
    with client.application.app_context():
        survey, _, _ = _source_anonymous_survey(policy="por_phone")
        monkeypatch.setitem(
            client.application.config,
            "SURVEY_IDENTITY_HMAC_SECRET_V1",
            HMAC_SECRET,
        )
        first = build_unique_fingerprint(
            survey,
            survey.tenant_id,
            phone="+54 9 236 400-0000",
        )
        formatted_equivalent = build_unique_fingerprint(
            survey,
            survey.tenant_id,
            phone="5492364000000",
        )
        monkeypatch.setitem(
            client.application.config,
            "SURVEY_IDENTITY_HMAC_SECRET_V1",
            "different-survey-identity-secret-32-bytes",
        )
        rotated_secret = build_unique_fingerprint(
            survey,
            survey.tenant_id,
            phone="5492364000000",
        )

        assert first == formatted_equivalent
        assert first != rotated_secret
        assert "5492364000000" not in first


def test_public_contract_explains_consent_retention_and_discarded_fields(client):
    with client.application.app_context():
        survey, _, _ = _source_anonymous_survey()

        payload = serialize_public_encuesta(survey)

        privacy = payload["frontend_contract"]["privacy"]
        assert privacy["contract_version"] == "surveys.privacy.v1"
        assert privacy["mode"] == SURVEY_PRIVACY_MODE_SOURCE_ANONYMOUS
        assert privacy["policy_version"] == PRIVACY_VERSION
        assert privacy["policy_url"] == PRIVACY_URL
        assert privacy["consent_required"] is True
        assert privacy["retention_days"] == 365
        assert privacy["source_identifiers_persisted"] is False
        assert {"dni", "phone", "ip", "lat", "lng", "metadata"}.issubset(
            set(privacy["discarded_before_persist"])
        )


def test_retention_purge_waits_for_effects_then_deletes_response_evidence(
    client,
    monkeypatch,
):
    with client.application.app_context():
        monkeypatch.setitem(
            client.application.config,
            "SURVEY_IDENTITY_HMAC_SECRET_V1",
            HMAC_SECRET,
        )
        survey, question, option = _source_anonymous_survey()
        response = save_respuesta(
            survey.slug,
            _payload(question, option),
            _request_context(),
            commit=False,
            emit_realtime_update=False,
        )
        response.retention_expires_at = datetime.now(timezone.utc) - timedelta(
            minutes=1
        )
        db.session.commit()
        response_id = int(response.id)
        entity_ref = f"survey:{survey.id}:response:{response_id}"
        db.session.add(
            AnalyticsEventV2(
                id=str(uuid.uuid4()),
                tenant_id=survey.tenant_id,
                tenant_type="municipio",
                event_name="survey_answer_submitted",
                entity_ref=entity_ref,
                metadata_payload={"response_id": response_id},
            )
        )
        db.session.commit()

        blocked = purge_expired_source_anonymous_responses(
            now=datetime.now(timezone.utc),
            tenant_id=survey.tenant_id,
        )
        assert blocked["eligible"] == 0
        assert db.session.get(EncRespuesta, response_id) is not None

        effects = SurveyResponseEffect.query.filter_by(response_id=response_id).all()
        assert effects
        for effect in effects:
            effect.status = "succeeded"
            effect.processed_at = datetime.now(timezone.utc)
        db.session.commit()

        purged = purge_expired_source_anonymous_responses(
            now=datetime.now(timezone.utc),
            tenant_id=survey.tenant_id,
        )

        assert purged["eligible"] == 1
        assert purged["deleted"] == 1
        assert db.session.get(EncRespuesta, response_id) is None
        assert EncRespuestaDetalle.query.filter_by(respuesta_id=response_id).count() == 0
        assert SurveyResponseEffect.query.filter_by(response_id=response_id).count() == 0
        assert AnalyticsEventV2.query.filter_by(entity_ref=entity_ref).count() == 0
        audit = AuditEvent.query.filter_by(
            tenant_id=survey.tenant_id,
            event_type="survey_privacy_retention_purge",
        ).one()
        assert audit.details["deleted_count"] == 1
        assert audit.details["source_identifiers_in_audit"] is False
        assert "response_ids" not in audit.details
        assert "fingerprint" not in audit.details
