"""Synthetic SQLite/Flask acceptance, with network blocked by conftest.

These fixtures reproduce counts and shapes, never import production records.
Concurrent SQLite checks exercise the real no-op write locks; PostgreSQL locks
use the same statements but need the separate PostgreSQL CI gate.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading

import pytest

from app import create_app
from config import TestingConfig
from database import db
from models import (AuditEvent, EncEncuesta, EncLink, EncOpcion, EncPregunta,
                    EncRespuesta, EncRespuestaDetalle, EncSegmento, TenantProfile, User)
from models_survey_jurisdiction import SurveyContentReceipt
from services.auth_session_lifecycle import issue_token
from utils.auth_helpers import auth_session_version
from services.encuestas_service import (EncuestaError, _acquire_encuesta_response_guard,
    _ensure_locked_public_encuesta, get_public_encuesta, list_encuestas_page)
from services.operational_intelligence import (_survey_metrics, build_operational_freshness,
    build_operational_heatmap)
import services.survey_editorial_relocation as relocation
from services.survey_jurisdiction import record_content_receipt, survey_content_sha256


def _seed(counts=(101, 0, 200, 100, 100)):
    actor = User(id=901, name="Synthetic SuperAdmin", email="relocation-sa@test.local", rol="super_admin", password_hash="synthetic")
    ordinary = User(id=902, name="Synthetic administrator", email="relocation-admin@test.local", rol="admin", password_hash="synthetic")
    source = TenantProfile(id=71, slug="relocation-source", nombre="Synthetic source", tipo="municipio", municipio_id=ordinary.id, plan="full")
    target = TenantProfile(id=72, slug="relocation-target", nombre="Synthetic target", tipo="municipio", municipio_id=actor.id, plan="full")
    db.session.add_all([actor, ordinary, source, target])
    db.session.flush()
    ordinary.tenant_id = source.id
    ordinary.tenant_slug = source.slug
    surveys = []
    for index, count in enumerate(counts):
        survey = EncEncuesta(
            id=801 + index, tenant_id=source.id, slug=f"relocation-source-{index}",
            titulo=f"Synthetic instrument {index}", descripcion="Editorial fixture", tipo="votacion",
            estado="borrador" if index == 1 else "cerrada" if index == 0 else "publicada",
            content_origin="manual", document_ref=f"synthetic-source:{index}", created_by=ordinary.id,
            structure_locked_at=datetime.now(timezone.utc) if count else None,
            inicio_at=datetime.now(timezone.utc) - timedelta(days=1),
            requiere_identidad=False, politica_unicidad="libre", anonimo_permitido=True,
            es_votacion_envivo=True, mostrar_resultados_envivo=True, permitir_comentarios=True,
        )
        first = EncPregunta(orden=1, logical_ref="q-first", tipo="opcion_unica", texto="Synthetic choice", obligatoria=True)
        first.opciones.extend([EncOpcion(orden=1, logical_ref="opt-a", texto="A", valor="a"), EncOpcion(orden=2, logical_ref="opt-b", texto="B", valor="b")])
        survey.preguntas.extend([first, EncPregunta(orden=2, logical_ref="q-followup", tipo="abierta", texto="Synthetic detail",
            logica_condicional={"version": 2, "show_if": {"kind": "group", "operator": "and", "children": [{"kind": "option_selected", "question_ref": "q-first", "option_ref": "opt-a"}]}})])
        survey.segmentos.append(EncSegmento(clave="tag", valor="editorial-fixture"))
        survey.links.append(EncLink(slug_publico=f"synthetic-public-{index}"))
        db.session.add(survey)
        db.session.flush()
        for row in range(count):
            response = EncRespuesta(encuesta_id=survey.id, tenant_id=source.id,
                response_origin="real" if row == 0 else "synthetic_demo", metadata_payload={"fixture": row}, canal="web")
            response.detalles.append(EncRespuestaDetalle(pregunta_id=first.id, opcion_id=first.opciones[0].id, texto_libre="Synthetic preserved answer"))
            db.session.add(response)
        surveys.append(survey)
    record_content_receipt(surveys[1], event_type="created", decision="recorded", actor_user_id=ordinary.id, reason_code="synthetic_fixture")
    db.session.commit()
    return actor, ordinary, source, target, surveys


@pytest.fixture
def context(client):
    with client.application.app_context():
        yield _seed()


def _preview(context):
    actor, _, source, target, surveys = context
    return relocation.preview_relocation(actor, source.id, target.id, target.slug, [survey.id for survey in surveys])


def _request(context, key="synthetic-relocation-0001"):
    preview = _preview(context)
    return {"source_tenant_id": preview["source_tenant"]["id"], "target_tenant_id": preview["target_tenant"]["id"],
        "target_tenant_slug": preview["target_tenant"]["slug"], "idempotency_key": key,
        "confirmation": "archive_originals_create_drafts", "surveys": [{
            "survey_id": item["survey_id"], "expected_state": item["state"],
            "expected_structure_revision": item["structure_revision"], "expected_editorial_sha256": item["editorial_sha256"],
            "expected_response_count_all_time": item["response_count_all_time"],
        } for item in preview["items"]]}


def _apply(context, payload=None):
    actor, _, source, _, _ = context
    payload = payload or _request(context)
    return relocation.apply_relocation(actor, source.id, payload, payload["idempotency_key"])


def _unchanged(context):
    assert [survey.estado for survey in context[-1]] == ["cerrada", "borrador", "publicada", "publicada", "publicada"]
    assert EncEncuesta.query.filter_by(tenant_id=context[3].id).count() == 0
    assert AuditEvent.query.filter_by(event_type=relocation.EVENT_TYPE).count() == 0
    assert EncRespuesta.query.count() == 501


def test_preview_uses_all_time_all_origins_and_complete_canonical_scope(context):
    preview = _preview(context)
    assert [item["response_count_all_time"] for item in preview["items"]] == [101, 0, 200, 100, 100]
    assert preview["response_count_all_time"] == 501
    assert preview["can_apply"] is True
    assert preview["ui"]["confirm_label"] and preview["ui"]["preservation_notice"]
    _unchanged(context)


def test_atomic_five_copies_preserve_all_original_history_and_only_create_drafts(context):
    originals = context[-1]
    original_rows = [(s.id, s.tenant_id, s.created_by, s.structure_revision, s.structure_locked_at,
                      survey_content_sha256(s), [q.id for q in s.preguntas], [(link.id, link.slug_publico) for link in s.links]) for s in originals]
    response_rows = [(r.id, r.encuesta_id, r.tenant_id, r.metadata_payload, [(d.id, d.pregunta_id, d.opcion_id, d.texto_libre) for d in r.detalles]) for r in EncRespuesta.query.all()]
    receipt_rows = [(r.id, r.receipt_sha256, r.receipt_json) for r in SurveyContentReceipt.query.all()]
    result, replayed = _apply(context)
    assert replayed is False and result["copied_responses"] is False
    assert len(result["items"]) == 5 and result["response_count_all_time"] == 501
    assert original_rows == [(s.id, s.tenant_id, s.created_by, s.structure_revision, s.structure_locked_at,
                             survey_content_sha256(s), [q.id for q in s.preguntas], [(link.id, link.slug_publico) for link in s.links]) for s in originals]
    assert response_rows == [(r.id, r.encuesta_id, r.tenant_id, r.metadata_payload, [(d.id, d.pregunta_id, d.opcion_id, d.texto_libre) for d in r.detalles]) for r in EncRespuesta.query.all()]
    assert receipt_rows == [(r.id, r.receipt_sha256, r.receipt_json) for r in SurveyContentReceipt.query.filter(SurveyContentReceipt.survey_id.in_([s.id for s in originals])).all()]
    assert all(s.estado == "archivada" for s in originals)
    for source, item in zip(originals, result["items"]):
        clone = db.session.get(EncEncuesta, item["destination_survey_id"])
        assert clone.tenant_id == context[3].id and clone.estado == "borrador"
        assert clone.respuestas.count() == 0 and clone.links == [] and clone.snapshots == []
        assert clone.structure_locked_at is None and clone.structure_revision == 1
        assert clone.jurisdiction_ref is None and clone.document_ref != source.document_ref
        for field in relocation._COPY_FIELDS:
            assert getattr(clone, field) == getattr(source, field)
        assert [(q.logical_ref, q.texto, q.logica_condicional, [(o.logical_ref, o.valor) for o in q.opciones]) for q in clone.preguntas] == [(q.logical_ref, q.texto, q.logica_condicional, [(o.logical_ref, o.valor) for o in q.opciones]) for q in source.preguntas]
        assert [(s.clave, s.valor) for s in clone.segmentos] == [(s.clave, s.valor) for s in source.segmentos]
        assert "survey:" + str(source.id) in clone.content_origin_ref
    event = AuditEvent.query.filter_by(event_type=relocation.EVENT_TYPE).one()
    assert event.actor_user_id == context[0].id and event.details["receipt"]["operation_id"] == result["operation_id"]


def test_same_intention_replays_without_new_copies_or_audit_and_status_is_read_only(context):
    payload = _request(context)
    first, _ = _apply(context, payload)
    repeated, replayed = _apply(context, payload)
    assert repeated == first and replayed is True
    assert relocation.relocation_status(context[0], context[2].id, payload["idempotency_key"]) == first
    assert EncEncuesta.query.filter_by(tenant_id=context[3].id).count() == 5
    assert AuditEvent.query.filter_by(event_type=relocation.EVENT_TYPE).count() == 1


def test_unknown_status_does_not_create_or_authorize_replay(context):
    with pytest.raises(EncuestaError) as error:
        relocation.relocation_status(context[0], context[2].id, "unknown-intention-0001")
    assert error.value.status_code == 404 and error.value.payload["retryable"] is False
    _unchanged(context)


@pytest.mark.parametrize("mutation", ["state", "revision", "editorial", "count", "owner", "target_slug"])
def test_canonical_compare_and_swap_rejects_stale_preview_before_any_clone(context, mutation):
    payload = _request(context)
    source = context[-1][-1]
    if mutation == "state": source.estado = "borrador"
    elif mutation == "revision": source.structure_revision += 1
    elif mutation == "editorial": source.preguntas[0].opciones[0].texto = "Changed editorial content"
    elif mutation == "count": db.session.add(EncRespuesta(encuesta_id=source.id, tenant_id=source.tenant_id, response_origin="real"))
    elif mutation == "owner": source.tenant_id = context[3].id
    else: payload["target_tenant_slug"] = "wrong-target"
    db.session.commit()
    with pytest.raises(EncuestaError): _apply(context, payload)
    assert EncEncuesta.query.count() == 5
    assert AuditEvent.query.filter_by(event_type=relocation.EVENT_TYPE).count() == 0
    assert all(s.estado != "archivada" for s in context[-1])


def test_non_superadmin_and_unpersisted_actor_cannot_preview_or_write(context):
    payload = _request(context)
    for actor in (context[1], type("ForgedActor", (), {"id": 99099, "rol": "super_admin"})()):
        with pytest.raises(EncuestaError) as denied:
            relocation.apply_relocation(actor, context[2].id, payload, payload["idempotency_key"])
        assert denied.value.status_code == 403
    _unchanged(context)


def test_revoked_or_disabled_superadmin_is_rechecked_before_writes(context):
    payload = _request(context)
    context[0].accesibilidad = {"auth": {"disabled": True}}
    db.session.commit()
    with pytest.raises(EncuestaError) as error: _apply(context, payload)
    assert error.value.status_code == 403
    _unchanged(context)


@pytest.mark.parametrize("field,value", [
    ("source_tenant_id", 99), ("target_tenant_id", 71), ("target_tenant_id", True),
    ("confirmation", "publish"), ("idempotency_key", "short"),
])
def test_invalid_or_conflicting_request_has_no_effect(context, field, value):
    payload = _request(context)
    payload[field] = value
    with pytest.raises(EncuestaError): _apply(context, payload)
    _unchanged(context)


def test_ordinary_tenant_admin_does_not_authorize_cross_tenant_action(context):
    with pytest.raises(EncuestaError) as denied:
        relocation.preview_relocation(context[1], context[2].id, context[3].id, context[3].slug, [801])
    assert denied.value.status_code == 403
    assert relocation.action_descriptor(context[1]) is None
    _unchanged(context)


def test_governance_release_cannot_be_bypassed_by_the_archive_operation(context, monkeypatch):
    payload = _request(context)
    monkeypatch.setattr(relocation, "has_governance_release", lambda survey: survey.id == 805)
    assert _preview(context)["can_apply"] is False
    with pytest.raises(EncuestaError) as error: _apply(context, payload)
    assert error.value.payload["reason_code"] == "relocation_governance_archive_policy_required"
    _unchanged(context)


def test_concurrent_reparent_after_initial_check_is_rejected_under_survey_lock(context, monkeypatch):
    payload = _request(context)
    original_guard = relocation._acquire_encuesta_write_guard
    def changed(identifier):
        if identifier == 805:
            db.session.execute(db.text("UPDATE enc_encuesta SET tenant_id = :target WHERE id = :id"), {"target": context[3].id, "id": identifier})
        return original_guard(identifier)
    monkeypatch.setattr(relocation, "_acquire_encuesta_write_guard", changed)
    with pytest.raises(EncuestaError) as error: _apply(context, payload)
    assert error.value.status_code == 403
    _unchanged(context)


def test_audit_failure_rolls_back_all_clones_archives_and_new_receipts(context, monkeypatch):
    payload = _request(context)
    original_add = db.session.add
    def fail_audit(row, *args, **kwargs):
        if isinstance(row, AuditEvent): raise RuntimeError("synthetic audit failure")
        return original_add(row, *args, **kwargs)
    monkeypatch.setattr(db.session, "add", fail_audit)
    with pytest.raises(RuntimeError): _apply(context, payload)
    _unchanged(context)
    assert SurveyContentReceipt.query.count() == 1


def test_late_clone_failure_rolls_back_the_earlier_clones_and_archives(context, monkeypatch):
    payload = _request(context)
    original_clone = relocation._clone
    count = 0
    def fail_third(*args):
        nonlocal count
        count += 1
        if count == 3: raise RuntimeError("synthetic late clone failure")
        return original_clone(*args)
    monkeypatch.setattr(relocation, "_clone", fail_third)
    with pytest.raises(RuntimeError): _apply(context, payload)
    _unchanged(context)
    assert SurveyContentReceipt.query.count() == 1


def test_idempotency_key_conflict_and_new_key_for_archived_sources_cannot_copy_twice(context):
    payload = _request(context)
    _apply(context, payload)
    changed = deepcopy(payload)
    changed["idempotency_key"] = "synthetic-other-intention"
    with pytest.raises(EncuestaError): _apply(context, changed)
    conflicted = deepcopy(payload)
    conflicted["surveys"][0]["expected_response_count_all_time"] += 1
    with pytest.raises(EncuestaError) as error: _apply(context, conflicted)
    assert error.value.payload["reason_code"] == "relocation_idempotency_conflict"
    assert EncEncuesta.query.count() == 10


def test_archived_surveys_disappear_from_default_list_operational_metrics_and_public(context):
    _apply(context)
    source = context[2]
    assert list_encuestas_page(source.id)["pagination"]["total_items"] == 0
    assert len(list_encuestas_page(source.id, include_archived=True)["items"]) == 5
    assert len(list_encuestas_page(context[3].id)["items"]) == 5
    metrics = _survey_metrics(source, datetime.now(timezone.utc) - timedelta(days=2), datetime.now(timezone.utc) + timedelta(days=1))
    assert metrics["summary"]["encuestas"] == 0 and metrics["summary"]["responses"] == 0
    assert metrics["live_items"] == []
    assert EncRespuesta.query.count() == 501
    for survey in context[-1]:
        with pytest.raises(EncuestaError):
            get_public_encuesta(survey.slug, preferred_tenant_id=source.id, allow_closed_for_read=True)


def test_stale_public_reader_cannot_write_after_archive_guard_wins(context):
    survey = context[-1][-1]
    assert survey.estado == "publicada"
    _apply(context)
    locked = _acquire_encuesta_response_guard(survey.id)
    with pytest.raises(EncuestaError) as error: _ensure_locked_public_encuesta(locked)
    assert error.value.payload["reason_code"] == "survey_not_published"
    assert EncRespuesta.query.count() == 501


def test_archives_leave_no_operational_freshness_or_heatmap_rows(context):
    response = EncRespuesta.query.filter_by(encuesta_id=805, response_origin="real").first()
    response.lat, response.lng = -33.13, -68.49
    db.session.commit()
    _apply(context)
    start, end = datetime.now(timezone.utc) - timedelta(days=2), datetime.now(timezone.utc) + timedelta(days=1)
    freshness = build_operational_freshness(context[2], start, end, viewer=context[0])
    surveys_source = next(item for item in freshness["sources"] if item["key"] == "surveys")
    assert surveys_source["period_count"] == 0
    heatmap = build_operational_heatmap(context[2], start, end, viewer=context[0], ticket_records=[], commerce_records=[], include_ai=False)
    assert not any(point.get("source") in {"surveys", "survey"} for point in heatmap["points"])
    assert heatmap["response_provenance"]["real_responses_included"] == 0
    assert EncRespuesta.query.count() == 501


def test_runtime_seed_configuration_is_not_copied_and_source_remains_unchanged(context):
    context[-1][0].segmentos.append(EncSegmento(clave="auto_seed_demo", valor='{"enabled":true}'))
    db.session.commit()
    receipt, _ = _apply(context)
    clone = db.session.get(EncEncuesta, receipt["items"][0]["destination_survey_id"])
    assert not any(segment.clave == "auto_seed_demo" for segment in clone.segmentos)
    assert any(segment.clave == "auto_seed_demo" for segment in context[-1][0].segmentos)
    assert receipt["items"][0]["excluded_runtime_segment_keys"] == ["auto_seed_demo"]


def _restore_request(context, archive, key="synthetic-restore-originals-0001"):
    preview = relocation.preview_restore(context[0], context[2].id, archive["idempotency_key"])
    return {"source_tenant_id": context[2].id, "archive_idempotency_key": archive["idempotency_key"],
        "idempotency_key": key, "confirmation": "restore_originals_without_publishing", "surveys": [{
            "survey_id": item["survey_id"], "expected_state": item["state"],
            "expected_structure_revision": item["structure_revision"], "expected_editorial_sha256": item["editorial_sha256"],
            "expected_response_count_all_time": item["response_count_all_time"],
        } for item in preview["items"]]}


def test_restore_preserves_original_responses_receipts_instrument_and_destination_drafts(context):
    content_before = [(s.id, survey_content_sha256(s), [q.id for q in s.preguntas]) for s in context[-1]]
    original_receipt = SurveyContentReceipt.query.one().receipt_json
    archive, _ = _apply(context)
    payload = _restore_request(context, archive)
    restored, replayed = relocation.restore_originals(context[0], context[2].id, payload, payload["idempotency_key"])
    assert replayed is False and restored["published"] is False and restored["preserved_responses"] is True
    assert [s.estado for s in context[-1]] == ["cerrada", "borrador", "cerrada", "cerrada", "cerrada"]
    assert content_before == [(s.id, survey_content_sha256(s), [q.id for q in s.preguntas]) for s in context[-1]]
    assert SurveyContentReceipt.query.filter_by(survey_id=802).one().receipt_json == original_receipt
    assert EncRespuesta.query.count() == 501
    assert all(clone.estado == "borrador" and clone.respuestas.count() == 0 for clone in EncEncuesta.query.filter_by(tenant_id=context[3].id))
    assert relocation.archive_history_metadata(context[2].id, context[-1]) == {}
    for survey in (context[-1][0], context[-1][-1]):
        with pytest.raises(EncuestaError): _ensure_locked_public_encuesta(survey)


def test_restore_cannot_reopen_or_publish_an_original(context):
    archive, _ = _apply(context)
    payload = _restore_request(context, archive)
    payload["confirmation"] = "restore_and_publish"
    with pytest.raises(EncuestaError) as error:
        relocation.restore_originals(context[0], context[2].id, payload, payload["idempotency_key"])
    assert error.value.payload["reason_code"] == "restore_confirmation_required"
    payload = _restore_request(context, archive)
    payload["surveys"][0]["restore_state"] = "publicada"
    with pytest.raises(EncuestaError):
        relocation.restore_originals(context[0], context[2].id, payload, payload["idempotency_key"])
    assert all(s.estado == "archivada" for s in context[-1])


def test_restore_stale_count_rolls_back_the_entire_batch(context):
    archive, _ = _apply(context)
    payload = _restore_request(context, archive)
    db.session.add(EncRespuesta(encuesta_id=805, tenant_id=context[2].id, response_origin="real"))
    db.session.commit()
    with pytest.raises(EncuestaError) as error:
        relocation.restore_originals(context[0], context[2].id, payload, payload["idempotency_key"])
    assert error.value.payload["reason_code"] == "restore_precondition_failed"
    assert all(s.estado == "archivada" for s in context[-1])
    assert EncRespuesta.query.count() == 502


def test_restore_is_idempotent_and_manual_status_returns_the_same_result(context):
    archive, _ = _apply(context)
    payload = _restore_request(context, archive)
    first, replayed = relocation.restore_originals(context[0], context[2].id, payload, payload["idempotency_key"])
    second, repeated = relocation.restore_originals(context[0], context[2].id, payload, payload["idempotency_key"])
    assert replayed is False and repeated is True and first == second
    assert relocation.relocation_status(context[0], context[2].id, payload["idempotency_key"]) == first
    assert AuditEvent.query.filter_by(event_type=relocation.RESTORE_EVENT_TYPE).count() == 1
    assert EncEncuesta.query.count() == 10 and EncRespuesta.query.count() == 501


def test_restore_requires_original_source_scope_superadmin_and_audit_commit(context, monkeypatch):
    archive, _ = _apply(context)
    payload = _restore_request(context, archive)
    for actor in (context[1],):
        with pytest.raises(EncuestaError) as error:
            relocation.restore_originals(actor, context[2].id, payload, payload["idempotency_key"])
        assert error.value.status_code == 403
    with pytest.raises(EncuestaError):
        relocation.preview_restore(context[0], context[3].id, archive["idempotency_key"])
    original_add = db.session.add
    def fail_restore_audit(row, *args, **kwargs):
        if isinstance(row, AuditEvent) and row.event_type == relocation.RESTORE_EVENT_TYPE:
            raise RuntimeError("synthetic restore audit failure")
        return original_add(row, *args, **kwargs)
    monkeypatch.setattr(db.session, "add", fail_restore_audit)
    with pytest.raises(RuntimeError):
        relocation.restore_originals(context[0], context[2].id, payload, payload["idempotency_key"])
    assert all(s.estado == "archivada" for s in context[-1])
    assert EncRespuesta.query.count() == 501


def test_unsupported_database_id_conditional_encoding_is_rejected_before_archival(context):
    payload = _request(context)
    context[-1][0].preguntas[1].logica_condicional = {"version": 2, "show_if": {
        "kind": "group", "operator": "and", "children": [{"kind": "option_selected", "question_id": 1, "option_id": 1}]}}
    db.session.commit()
    with pytest.raises(EncuestaError) as error: _apply(context, payload)
    assert error.value.payload["reason_code"] == "survey_conditional_logic_invalid"
    _unchanged(context)


def _headers(actor, source):
    claims = {"user_id": actor.id, "rol": actor.rol, "tenant_slug": source.slug,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    if actor.rol == "super_admin":
        claims.update(auth_provider="clerk", session_kind="clerk", clerk_sid="synthetic-relocation-clerk-session", sv=auth_session_version(actor))
    token = issue_token(claims)
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": source.slug}


def test_authenticated_http_contract_enforces_superadmin_and_explicit_history(client, monkeypatch):
    with client.application.app_context():
        context = _seed()
        actor, ordinary, source, target, surveys = context
        payload = _request(context)
        authorized = _headers(actor, source)
        denied = _headers(ordinary, source)
        query = {"survey_ids": ",".join(str(s.id) for s in surveys), "target_tenant_id": target.id, "target_tenant_slug": target.slug}
    assert client.get("/api/v2/surveys/editorial-relocations/preview", query_string=query, headers=denied).status_code == 403
    assert client.post("/api/v2/surveys/editorial-relocations", json=payload, headers={**denied, "Idempotency-Key": payload["idempotency_key"]}).status_code == 403
    preview = client.get("/api/v2/surveys/editorial-relocations/preview", query_string=query, headers=authorized)
    assert preview.status_code == 200 and preview.json["can_apply"] is True
    result = client.post("/api/v2/surveys/editorial-relocations", json=payload, headers={**authorized, "Idempotency-Key": payload["idempotency_key"]})
    assert result.status_code == 201 and result.json["copied_responses"] is False
    status = client.get("/api/v2/surveys/editorial-relocations/status", query_string={"idempotency_key": payload["idempotency_key"]}, headers=authorized)
    assert status.status_code == 200 and status.json["operation_id"] == result.json["operation_id"]
    listing = client.get("/api/v2/surveys", headers=authorized)
    assert listing.status_code == 200 and listing.json["total"] == 0
    assert listing.json["editorial_relocation"]["can_preview"] is True
    assert client.get("/api/v2/surveys", query_string={"include_archived": "true"}, headers=denied).status_code == 403
    history = client.get("/api/v2/surveys", query_string={"include_archived": "true"}, headers=authorized)
    assert history.status_code == 200 and history.json["total"] == 5
    assert client.get("/api/v2/surveys", query_string={"include_archived": "1"}, headers=authorized).json["total"] == 5
    import routes.encuestas_admin as legacy
    monkeypatch.setattr(legacy, "FEATURE_ENCUESTAS", True)
    current_list = client.get("/api/admin/encuestas", headers=authorized)
    assert current_list.status_code == 200 and current_list.json["editorial_relocation"]["can_preview"] is True
    assert current_list.json["pagination"]["total_items"] == 0
    history = client.get("/api/admin/encuestas", query_string={"include_archived": "1"}, headers=authorized)
    assert history.status_code == 200 and history.json["pagination"]["total_items"] == 5
    assert set(history.json["archived_editorial_relocations"]) == {"801", "802", "803", "804", "805"}
    assert client.get("/api/admin/encuestas", query_string={"include_archived": "1"}, headers=denied).status_code == 403
    assert client.get("/api/admin/encuestas", headers=denied).json["editorial_relocation"] is None
    archive_key = payload["idempotency_key"]
    preview = client.get("/api/v2/surveys/editorial-relocations/restore-preview", query_string={"idempotency_key": archive_key}, headers=authorized)
    assert preview.status_code == 200 and preview.json["can_apply"] is True
    restore_body = {"source_tenant_id": payload["source_tenant_id"], "archive_idempotency_key": archive_key,
        "idempotency_key": "http-restore-0001", "confirmation": "restore_originals_without_publishing", "surveys": [{
            "survey_id": item["survey_id"], "expected_state": item["state"], "expected_structure_revision": item["structure_revision"],
            "expected_editorial_sha256": item["editorial_sha256"], "expected_response_count_all_time": item["response_count_all_time"],
        } for item in preview.json["items"]]}
    assert client.post("/api/v2/surveys/editorial-relocations/restore", json=restore_body,
        headers={**denied, "Idempotency-Key": restore_body["idempotency_key"]}).status_code == 403
    restored = client.post("/api/v2/surveys/editorial-relocations/restore", json=restore_body,
        headers={**authorized, "Idempotency-Key": restore_body["idempotency_key"]})
    assert restored.status_code == 201 and restored.json["published"] is False


def test_two_concurrent_same_intentions_produce_one_batch_without_new_migration(tmp_path):
    config = type("EditorialRelocationConcurrencyConfig", (TestingConfig,), {
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'editorial-concurrency.sqlite3').as_posix()}",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False, "timeout": 5}},
    })
    app = create_app(config)
    with app.app_context():
        db.create_all()
        context = _seed((1, 0, 1, 1, 1))
        payload = _request(context)
        actor_id, source_id = context[0].id, context[2].id
        db.session.remove()
    barrier = threading.Barrier(2)
    outcomes, errors = [], []
    def worker():
        with app.app_context():
            try:
                actor = db.session.get(User, actor_id)
                barrier.wait(timeout=5)
                outcomes.append(relocation.apply_relocation(actor, source_id, deepcopy(payload), payload["idempotency_key"]))
            except Exception as exc: errors.append(type(exc).__name__)
            finally: db.session.remove()
    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(outcomes) == 2 and sorted(replayed for _, replayed in outcomes) == [False, True]
        assert outcomes[0][0] == outcomes[1][0]
        with app.app_context():
            assert EncEncuesta.query.count() == 10
            assert EncRespuesta.query.count() == 4
            assert AuditEvent.query.filter_by(event_type=relocation.EVENT_TYPE).count() == 1
    finally:
        with app.app_context():
            db.session.remove()
            db.drop_all()
