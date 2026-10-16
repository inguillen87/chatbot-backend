"""Offline regression for one availability authority across private surfaces."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import current_app

from database import db
from models import EncLink
from services import encuestas_analytics_service as analytics
from services.encuestas_service import _public_schedule_now
from tests.test_survey_admin_lifecycle_contract import (
    _government_tenant, _headers, _response, _survey, _tenant,
)
from tests.junin_product_flow_support import JUNIN_JURISDICTION_REF, JUNIN_JURISDICTION_EVIDENCE_REF


@pytest.mark.parametrize("case,receiving", [
    ("observe", True), ("blocked", False), ("scheduled", False),
    ("closed", False), ("draft", False), ("window_ended", False), ("conflict", False),
])
def test_private_survey_surfaces_use_the_admin_public_guard(client, monkeypatch, case, receiving):
    owner, tenant = _government_tenant(f"availability-{case}")
    state = "cerrada" if case == "closed" else "borrador" if case == "draft" else "publicada"
    survey = _survey(tenant, slug=f"availability-{case}-vote", state=state, voting=True)
    # Persisted titles, including legacy references, never infer tenant or kind.
    survey.titulo = "Votación histórica Tierra del Fuego"
    if case in {"closed", "draft"}:
        db.session.add(EncLink(encuesta_id=survey.id, slug_publico=survey.slug, canal="web"))
    if case == "scheduled":
        survey.inicio_at = _public_schedule_now() + timedelta(days=1)
    if case == "window_ended":
        survey.fin_at = _public_schedule_now() - timedelta(hours=1)
    if case == "blocked":
        monkeypatch.setitem(current_app.config, "SURVEY_JURISDICTION_GATE_MODE", "enforce_visibility")
        monkeypatch.setitem(current_app.config, "SURVEY_JURISDICTION_GATE_TENANT_IDS", str(tenant.id))
    if case == "conflict":
        tenant.jurisdiction_ref = JUNIN_JURISDICTION_REF
        tenant.jurisdiction_evidence_ref = JUNIN_JURISDICTION_EVIDENCE_REF
        tenant.jurisdiction_verified_by_user_id = owner.id
        tenant.jurisdiction_verified_at = datetime.now(timezone.utc)
        tenant.jurisdiction_status = "verified"
        survey.jurisdiction_ref = "ar:tf:ushuaia"
    titled_only = _survey(tenant, slug=f"availability-{case}-opinion", state="borrador")
    titled_only.titulo = "Votación en el título, instrumento de opinión"
    foreign_owner, foreign_tenant = _tenant(f"availability-foreign-{case}")
    foreign_survey = _survey(foreign_tenant, slug=f"foreign-{case}-vote", voting=True)
    db.session.commit()
    headers = _headers(owner, tenant)

    admin = client.get("/api/admin/encuestas", headers=headers)
    assert admin.status_code == 200, admin.get_json()
    listed = next(row for row in admin.get_json()["encuestas"] if row["id"] == survey.id)
    lifecycle = listed["admin_lifecycle"]
    assert lifecycle["accepts_responses"] is receiving
    assert admin.get_json()["resumen"]["accepting_responses"] == int(receiving)

    dashboard_response = client.get("/api/v2/analytics/operations/dashboard", headers=headers)
    assert dashboard_response.status_code == 200, dashboard_response.get_json()
    dashboard = dashboard_response.get_json()
    summary = dashboard["surveys"]["summary"]
    room = dashboard["surveys"]["live_control_room"]
    monitor = room["monitors"][0]
    assert summary["encuestas"] == 2
    assert summary["active"] == summary["accepting_responses"] == int(receiving)
    assert summary["votaciones_live"] == dashboard["summary"]["live_votes"] == int(receiving)
    assert summary["configured_votations"] == room["summary"]["configured_monitors"] == 1
    assert summary["published"] == int(state == "publicada")
    assert room["summary"]["accepting_responses"] == room["summary"]["live_surveys"] == int(receiving)
    assert monitor["id"] == survey.id and monitor["id"] != foreign_survey.id
    assert monitor["title"] == survey.titulo
    assert monitor["public_access"] == listed["public_access"]
    assert monitor["phase"] == lifecycle["phase"]
    assert monitor["accepts_responses"] is receiving
    assert monitor["can_share"] is lifecycle["capabilities"]["can_share"]
    assert monitor["admin_url"].startswith(f"/admin/encuestas/{survey.id}/analytics")
    assert room["realtime"]["enabled"] is receiving
    for field in ("public_url", "live_results_endpoint", "heatmap_endpoint"):
        assert bool(monitor[field]) is receiving
    trend_keys = {item["key"] for item in dashboard["trends"]["items"]}
    assert "live_votes" not in trend_keys
    assert {"key": "live_votes", "reason_code": "current_availability_not_historical_snapshot"} in dashboard["trends"]["unavailable"]

    tenant_response = client.get("/api/v2/tenant/admin-experience", headers=headers)
    assert tenant_response.status_code == 200, tenant_response.get_json()
    overview = tenant_response.get_json()["surveys_votings"]
    assert overview["summary"]["active"] == overview["summary"]["live_votes"] == int(receiving)
    assert overview["summary"]["configured_votations"] == 1
    overview_item = next(row for row in overview["items"] if row["id"] == survey.id)
    assert overview_item["public_access"] == listed["public_access"]
    assert overview_item["accepts_responses"] is receiving
    assert foreign_survey.id not in {row["id"] for row in overview["items"]}

    backoffice_response = client.get("/api/app/backoffice/summary", headers=headers)
    assert backoffice_response.status_code == 200, backoffice_response.get_json()
    backoffice = backoffice_response.get_json()["surveys_overview"]
    assert backoffice["active_surveys"] == backoffice["accepting_responses"] == int(receiving)
    assert backoffice["published_surveys"] == int(state == "publicada")

    actions_response = client.get("/api/v2/analytics/operations/action-center", headers=headers)
    assert actions_response.status_code == 200, actions_response.get_json()
    promote = [row for row in actions_response.get_json()["items"] if row["id"] == "promote_live_vote"]
    assert bool(promote) is receiving

    publication = analytics._build_survey_publication_contract(survey.id)
    assert publication["public_access"] == listed["public_access"]
    assert publication["is_published"] is (state == "publicada")
    assert publication["has_public_link"] is True
    assert publication["can_share"] is receiving
    assert bool(publication["links"]) is receiving
    if not receiving:
        assert not {"copy_public_link", "download_qr", "open_live_results"}.intersection(row["id"] for row in publication["actions"])
    public_response = client.get(f"/api/v2/public/surveys/{survey.slug}?tenant_slug={tenant.slug}")
    # V2 keeps the existing read-only closed view. Reception remains disabled.
    assert (public_response.status_code == 200) is (receiving or case in {"closed", "conflict"})
    private_response = client.get(f"/api/v2/surveys/{survey.id}/analytics", headers=headers)
    assert private_response.status_code == 200, private_response.get_json()
    denied = client.get(f"/api/v2/surveys/{survey.id}/analytics", headers=_headers(foreign_owner, foreign_tenant))
    assert denied.status_code in {403, 404}


def test_configured_vote_total_is_not_capped_by_monitor_limit(client):
    owner, tenant = _tenant("availability-monitor-limit")
    for index in range(12):
        _survey(tenant, slug=f"availability-monitor-{index}", voting=True)
    response = client.get("/api/v2/analytics/operations/dashboard", headers=_headers(owner, tenant))
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    room = payload["surveys"]["live_control_room"]
    assert payload["summary"]["live_votes"] == 12
    assert room["summary"]["configured_monitors"] == room["summary"]["accepting_responses"] == 12
    assert room["summary"]["returned_monitors"] == len(room["monitors"]) == 10


def test_public_paths_bind_owner_tenant_when_durable_public_token_is_shared(client):
    owner, tenant = _tenant("availability-owner-links")
    survey = _survey(tenant, slug="availability-owner-survey", voting=True)
    survey.titulo = "Legacy Tierra del Fuego; propiedad del tenant persistido"
    _, foreign_tenant = _tenant("availability-foreign-links")
    foreign = _survey(foreign_tenant, slug="availability-foreign-survey", voting=True)
    shared_token = "availability-shared-public-token"
    for row in (survey, foreign):
        db.session.add(EncLink(encuesta_id=row.id, slug_publico=shared_token, canal="web"))
    db.session.commit()
    response = client.get("/api/v2/analytics/operations/dashboard", headers=_headers(owner, tenant))
    assert response.status_code == 200, response.get_json()
    surveys = response.get_json()["surveys"]
    monitor = surveys["live_control_room"]["monitors"][0]
    live_item = surveys["live_items"][0]
    publication = analytics._build_survey_publication_contract(survey.id)
    assert monitor["public_token"] == shared_token
    assert monitor["title"] == survey.titulo
    urls = [
        monitor["public_url"], monitor["live_results_endpoint"], monitor["heatmap_endpoint"],
        live_item["public_url"], live_item["live_results_endpoint"], live_item["heatmap_endpoint"],
        publication["links"]["public_page_path"], publication["links"]["copy_url"],
        publication["links"]["share_url"], publication["links"]["public_api_endpoint"],
        publication["links"]["respond_endpoint"], publication["links"]["live_results_endpoint"],
        publication["links"]["legacy_public_api_endpoint"],
        publication["links"]["legacy_live_results_endpoint"], publication["links"]["qr_endpoint"],
    ]
    for url in urls:
        assert parse_qs(urlsplit(url).query)["tenant_slug"] == [tenant.slug]
    assert parse_qs(urlsplit(monitor["heatmap_endpoint"]).query)["include_heatmap"] == ["1"]
    assert publication["links"]["public_page_path"] in publication["links"]["copy_text"]
    for endpoint in (monitor["live_results_endpoint"], monitor["heatmap_endpoint"], publication["links"]["public_api_endpoint"]):
        resolved = client.get(endpoint)
        assert resolved.status_code == 200, resolved.get_json()
        data = resolved.get_json()
        assert data.get("encuesta_id", data.get("id")) == survey.id
    resolved_foreign = client.get(f"/api/v2/public/surveys/{shared_token}?tenant_slug={foreign_tenant.slug}")
    assert resolved_foreign.status_code == 200
    assert resolved_foreign.get_json()["id"] == foreign.id


def test_cached_live_prompts_do_not_survive_a_changed_operational_veto(client):
    owner, tenant = _tenant("availability-live-cache")
    survey = _survey(tenant, slug="availability-live-cache", voting=True)
    survey.jurisdiction_ref = "ar:tf:ushuaia"
    db.session.commit()
    active = analytics.calculate_live_results(survey.slug, include_heatmap=False)
    assert active["empty_state"]["action_hint"] == "share_survey"
    assert any(row["id"] == "share_survey_now" for row in active["operator_recommendations"])
    # Only the tenant evidence changes; survey.updated_at/result_version stay.
    tenant.jurisdiction_ref = JUNIN_JURISDICTION_REF
    tenant.jurisdiction_evidence_ref = JUNIN_JURISDICTION_EVIDENCE_REF
    tenant.jurisdiction_verified_by_user_id = owner.id
    tenant.jurisdiction_verified_at = datetime.now(timezone.utc)
    tenant.jurisdiction_status = "verified"
    db.session.commit()
    blocked = analytics.calculate_live_results(survey.slug, include_heatmap=False)
    assert blocked["empty_state"]["action_hint"] is None
    assert blocked["operator_recommendations"] == []


def test_hidden_results_keep_share_availability_without_results_links(client):
    _, tenant = _tenant("availability-hidden-results")
    survey = _survey(tenant, slug="availability-hidden-results", voting=True)
    survey.mostrar_resultados_envivo = False
    db.session.commit()
    publication = analytics._build_survey_publication_contract(survey.id)
    assert publication["can_share"] is True
    assert publication["links"]["public_url"]
    for key in ("live_results_endpoint", "results_endpoint", "legacy_live_results_endpoint"):
        assert publication["links"][key] is None
    assert "open_live_results" not in {row["id"] for row in publication["actions"]}


def test_closed_history_keeps_observed_metrics_without_participation_prompts(client):
    owner, tenant = _tenant("availability-closed-history")
    survey = _survey(tenant, slug="availability-closed-history", state="cerrada", voting=True)
    db.session.add(EncLink(encuesta_id=survey.id, slug_publico=survey.slug, canal="web"))
    db.session.commit()
    _response(survey, "closed-history-real-response")
    bundle = analytics.get_dashboard_bundle(survey.id, fast_mode=True)
    assert bundle["modules"]["summary"]["total_respuestas"] == 1
    assert bundle["modules"]["summary"]["tasa_completitud"] == 100
    brief = bundle["executive_summary"]
    assert "100.0%" in brief["headline"]
    assert "finalizado" in brief["one_liner"]
    assert "operación en curso" not in brief["one_liner"]
    assert bundle["survey_publication"]["public_state"] == "closed"
    assert bundle["survey_publication"]["links"] == {}
    live = analytics.calculate_live_results(survey.slug, allow_closed_for_read=True, include_heatmap=False)
    assert live["total_respuestas"] == 1
    assert live["empty_state"]["action_hint"] is None
    assert live["operator_recommendations"] == []
    assert not any("difusi" in text or "distribuci" in text or "recordatorios" in text for text in live["ai_insights"])
    response = client.get("/api/app/backoffice/summary", headers=_headers(owner, tenant))
    assert response.status_code == 200, response.get_json()
    # This existing counter counts responses, unlike operations.live_votes.
    assert response.get_json()["surveys_overview"]["live_votes"] == 1
    assert response.get_json()["surveys_overview"]["active_surveys"] == 0


@pytest.mark.parametrize("phase,word", [("draft", "borrador"), ("scheduled", "programado")])
def test_non_receiving_narrative_has_no_current_operation(phase, word):
    brief = analytics._build_executive_summary_text(
        {"total_respuestas": 1, "tasa_completitud": 100},
        {"projected_total": 8, "projected_additional": 7}, {"alerts": []}, {},
        availability={"admin_lifecycle": {"phase": phase, "accepts_responses": False}},
    )
    assert word in brief["one_liner"]
    assert "operación en curso" not in brief["one_liner"]
    assert "100.0%" in brief["headline"]
