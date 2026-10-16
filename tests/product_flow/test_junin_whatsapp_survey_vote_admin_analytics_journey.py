from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from database import db
from models import (
    AuditEvent,
    EncEncuesta,
    EncRespuesta,
    SurveyResponseEffect,
    SurveyResponseReceipt,
    TenantProfile,
    User,
)
from services.constants import CONTEXTO_MUNICIPIO
from services.encuestas_service import EncuestaError, save_respuesta as real_save_respuesta
from services.whatsapp_survey_conversation import (
    WHATSAPP_SURVEY_FLOW_STATE_KEY,
    handle_whatsapp_survey_flow_turn,
    start_whatsapp_survey_flow,
)
from tests.junin_product_flow_support import (
    JUNIN_QA_ADDRESS,
    JUNIN_QA_LAT,
    JUNIN_QA_LNG,
    publish_governed_junin_survey,
)
from utils.auth_helpers import generar_token


CITIZEN_PHONE = "+5492634123456"


def _action(payload: dict, prefix: str, *, label: str | None = None) -> str:
    for option in payload.get("options_list") or []:
        action_id = str(option.get("action_id") or "")
        if action_id.startswith(prefix) and (
            label is None
            or label.casefold() in str(option.get("texto") or "").casefold()
        ):
            return action_id
    raise AssertionError(f"No se encontró la acción {prefix!r} ({label!r})")


def _admin_headers(owner: User, tenant: TenantProfile) -> dict[str, str]:
    token = generar_token(
        owner.id,
        owner.rol,
        owner.tipo_chat,
        owner.municipio_id,
        owner.pyme_id,
        extra_claims={"tenant_id": tenant.id, "tenant_slug": tenant.slug},
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant": tenant.slug,
        "X-Tenant-Slug": tenant.slug,
    }


def _conversation_context(
    tenant: TenantProfile,
    citizen: User,
) -> dict:
    return {
        "tenant_profile": tenant,
        "tenant_id": tenant.id,
        "viewer_user_obj": citizen,
        "anon_id": CITIZEN_PHONE,
        "channel": "whatsapp",
        "municipio_config_actual": {
            "slug": tenant.slug,
            "encuestas_enabled": True,
            "encuestas_base_url": "https://www.chatboc.ar",
        },
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "contacto_usuario": {"telefono": CITIZEN_PHONE},
            }
        },
    }


@pytest.mark.usefixtures("block_external_network_by_default")
def test_junin_institutional_vote_from_admin_to_whatsapp_analytics_and_csv(
    client,
    monkeypatch,
):
    app = client.application
    monkeypatch.setitem(app.config, "ENABLE_DEMO_MODE", False)

    import routes.encuestas_analytics as analytics_routes

    monkeypatch.setattr(analytics_routes, "FEATURE_ENCUESTAS", True)

    owner = User(
        name="Administración municipal de Junín QA",
        email="admin-junin-vote@example.test",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="junin",
    )
    owner.set_password("secret123")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junín QA",
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
        is_active=True,
        configuracion={"encuestas_enabled": True},
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    citizen = User(
        name="Ciudadana de prueba",
        email="citizen-junin-vote@example.test",
        telefono=CITIZEN_PHONE,
        rol="usuario",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )
    citizen.set_password("secret123")
    db.session.add(citizen)
    db.session.commit()
    headers = _admin_headers(owner, tenant)

    now = datetime.now(timezone.utc)
    created = client.post(
        "/api/v2/surveys",
        json={
            "title": "Prioridad institucional Junín 2026",
            "description": (
                "Consulta ciudadana no vinculante sobre la prioridad de inversión "
                "municipal para el próximo trimestre."
            ),
            "survey_type": "votacion",
            "channel": "whatsapp",
            "live_vote": True,
            "show_live_results": True,
            "opens_at": (now - timedelta(days=1)).isoformat(),
            "closes_at": (now + timedelta(days=7)).isoformat(),
            "uniqueness_policy": "por_phone",
            "allow_anonymous": True,
            "questions": [
                {
                    "type": "single_choice",
                    "logical_ref": "vote:junin-priority-2026",
                    "label": "¿Qué inversión municipal debería priorizarse?",
                    "required": True,
                    "order_index": 1,
                    "options": [
                        {"label": "Luminarias", "value": "luminarias"},
                        {"label": "Bacheo", "value": "bacheo"},
                    ],
                },
                {
                    "type": "single_choice",
                    "logical_ref": "demographic:city",
                    "label": "¿En qué distrito vivís?",
                    "required": True,
                    "order_index": 2,
                    "options": [
                        {"label": "Junín", "value": "junin"},
                        {"label": "La Colonia", "value": "la_colonia"},
                    ],
                },
            ],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    created_payload = created.get_json()
    survey_id = int(created_payload["id"])
    assert created_payload["estado"] == "borrador"
    assert created_payload["tipo"] == "votacion"

    publication = publish_governed_junin_survey(
        client,
        survey_id=survey_id,
        tenant=tenant,
        reviewer_user_id=owner.id,
        headers=headers,
        idempotency_prefix=f"junin-vote-golden-{survey_id}",
    )
    assert publication.public_payload["public_state"]["status"] == "live"
    assert publication.public_payload["instrument_revision"] >= 1
    assert publication.public_payload["governance"]["mode"] == "governed_release"
    assert publication.public_payload["governance"]["active_release"][
        "release_id"
    ] == publication.release_id

    context = _conversation_context(tenant, citizen)
    start = start_whatsapp_survey_flow(context, publication.public_token)
    assert start["contract_version"] == "municipio.whatsapp_survey.v1"

    location_reprompt = handle_whatsapp_survey_flow_turn(
        {
            **context,
            "es_ubicacion": True,
            "ubicacion_usuario": {
                "latitude": JUNIN_QA_LAT,
                "longitude": JUNIN_QA_LNG,
                "address": JUNIN_QA_ADDRESS,
            },
        },
        text="",
    )
    assert location_reprompt["fuente"] == "encuesta_whatsapp_respuesta_ambigua_v1"
    assert EncRespuesta.query.filter_by(encuesta_id=survey_id).count() == 0
    state = context["chat_db_context_data"][CONTEXTO_MUNICIPIO][
        WHATSAPP_SURVEY_FLOW_STATE_KEY
    ]
    assert state["shared_location"] == {
        "lat": JUNIN_QA_LAT,
        "lng": JUNIN_QA_LNG,
    }

    vote_question = handle_whatsapp_survey_flow_turn(
        context,
        text="",
        action_id=_action(
            location_reprompt,
            "encuesta_wa::consent_accept::",
        ),
    )
    district_question = handle_whatsapp_survey_flow_turn(
        context,
        text="",
        action_id=_action(
            vote_question,
            "encuesta_wa::answer::",
            label="Luminarias",
        ),
    )
    district_action = _action(
        district_question,
        "encuesta_wa::answer::",
        label="Junín",
    )

    def commit_then_report_uncertain(*args, **kwargs):
        real_save_respuesta(*args, **kwargs)
        raise EncuestaError(
            "Confirmación incierta controlada",
            status_code=503,
            payload={"reason_code": "provider_confirmation_uncertain"},
        )

    with patch(
        "services.encuestas_service.emit_survey_response_update",
        return_value=True,
    ) as emit_update, patch(
        "services.whatsapp_survey_conversation.save_respuesta",
        side_effect=commit_then_report_uncertain,
    ):
        uncertain = handle_whatsapp_survey_flow_turn(
            context,
            text="",
            action_id=district_action,
        )

    assert uncertain["retryable"] is True
    assert emit_update.call_count == 1
    assert EncRespuesta.query.filter_by(encuesta_id=survey_id).count() == 1
    assert SurveyResponseReceipt.query.filter_by(survey_id=survey_id).count() == 1
    effects_before_retry = SurveyResponseEffect.query.filter_by(
        survey_id=survey_id
    ).all()
    assert {effect.effect_type for effect in effects_before_retry} == {
        SurveyResponseEffect.EFFECT_ANALYTICS,
        SurveyResponseEffect.EFFECT_REALTIME,
    }
    assert {effect.status for effect in effects_before_retry} == {
        SurveyResponseEffect.STATUS_SUCCEEDED
    }
    retry_action = _action(uncertain, "encuesta_wa::retry::")

    with patch(
        "services.encuestas_service.emit_survey_response_update",
        return_value=True,
    ) as replay_emit:
        receipt = handle_whatsapp_survey_flow_turn(
            context,
            text="",
            action_id=retry_action,
        )

    assert receipt["success"] is True
    assert receipt["replayed"] is True
    assert receipt["idempotency"]["disposition"] == "replayed"
    assert replay_emit.call_count == 0
    assert EncRespuesta.query.filter_by(encuesta_id=survey_id).count() == 1
    assert SurveyResponseReceipt.query.filter_by(survey_id=survey_id).count() == 1
    assert SurveyResponseEffect.query.filter_by(survey_id=survey_id).count() == 2
    assert WHATSAPP_SURVEY_FLOW_STATE_KEY not in context["chat_db_context_data"][
        CONTEXTO_MUNICIPIO
    ]

    saved = EncRespuesta.query.filter_by(encuesta_id=survey_id).one()
    assert saved.tenant_id == tenant.id
    assert saved.canal == "whatsapp_chat"
    assert saved.response_origin == "real"
    assert saved.phone == CITIZEN_PHONE
    assert saved.ciudad == "junin"
    assert saved.lat == pytest.approx(JUNIN_QA_LAT)
    assert saved.lng == pytest.approx(JUNIN_QA_LNG)
    assert saved.governance_release_id == publication.release_id
    assert len(saved.detalles) == 2

    live = client.get(
        f"/api/v2/public/surveys/{publication.public_token}/live-results",
        query_string={"include_heatmap": "1"},
    )
    assert live.status_code == 200, live.get_json()
    live_payload = live.get_json()
    assert live_payload["total_respuestas"] == 1
    assert live_payload["preguntas"][0]["total_votos"] == 1
    assert live_payload["heatmap"]["points"] == []
    assert live_payload["heatmap"]["cells"] == []
    assert live_payload["heatmap"]["metadata"]["minimum_cell_size"] == 5
    assert live_payload["heatmap"]["metadata"]["raw_points_redacted"] is True
    etag = live.headers.get("ETag")
    assert etag
    assert client.get(
        f"/api/v2/public/surveys/{publication.public_token}/live-results",
        headers={"If-None-Match": etag},
    ).status_code == 304

    summary = client.get(
        f"/api/encuestas/{survey_id}/analytics/summary",
        headers=headers,
    )
    assert summary.status_code == 200, summary.get_json()
    assert summary.get_json()["total_respuestas"] == 1

    heatmap = client.get(
        f"/api/encuestas/{survey_id}/analytics/heatmap",
        headers=headers,
    )
    assert heatmap.status_code == 200, heatmap.get_json()
    heatmap_payload = heatmap.get_json()
    assert heatmap_payload["render_contract"]["state"] == "ready"
    assert len(heatmap_payload["points"]) == 1
    assert heatmap_payload["points"][0]["lat"] == pytest.approx(JUNIN_QA_LAT)
    assert heatmap_payload["points"][0]["lng"] == pytest.approx(JUNIN_QA_LNG)
    assert heatmap_payload["points"][0]["containment_verified"] is True
    assert heatmap_payload["points"][0]["coordinate_jurisdiction_status"] == "within"

    exported = client.get(
        f"/api/encuestas/{survey_id}/analytics/export.csv",
        headers={**headers, "X-Request-Id": "req-junin-vote-golden-csv"},
    )
    assert exported.status_code == 200
    assert exported.mimetype == "text/csv"
    assert exported.headers["Content-Disposition"] == (
        f"attachment; filename=encuesta-{survey_id}.csv"
    )
    rows = list(csv.DictReader(io.StringIO(exported.get_data(as_text=True))))
    assert len(rows) == 1
    assert rows[0]["canal"] == "whatsapp_chat"
    assert float(rows[0]["lat"]) == pytest.approx(JUNIN_QA_LAT)
    assert float(rows[0]["lng"]) == pytest.approx(JUNIN_QA_LNG)
    question_columns = [key for key in rows[0] if key.startswith("pregunta_")]
    assert [rows[0][key] for key in question_columns] == ["Luminarias", "Junín"]

    export_audits = AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="survey.analytics.export_requested",
        resource_id=str(survey_id),
    ).all()
    assert len(export_audits) == 1
    assert export_audits[0].details["format"] == "csv"
    audit_json = json.dumps(export_audits[0].details, ensure_ascii=False)
    assert CITIZEN_PHONE not in audit_json
    assert JUNIN_QA_ADDRESS not in audit_json

    persisted_survey = db.session.get(EncEncuesta, survey_id)
    assert persisted_survey.estado == "publicada"
