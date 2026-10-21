"""Audited editorial copies and reversible archival; never move participation.

The caller supplies an exact preview and a stable intention key. Tenant locks
serialize the audit-key namespace without a new table; survey write guards also
serialize archival with public responders. All effects share one transaction.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timezone
import hashlib
import json
import re
import uuid

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

from database import db
from models import AuditEvent, EncEncuesta, EncOpcion, EncPregunta, EncRespuesta, EncSegmento, TenantProfile, User
from services.encuestas_service import (
    EncuestaError, _acquire_encuesta_write_guard, _encuesta_jurisdiction_error, _enforce_survey_tenant_quota,
    _generate_unique_slug, _persisted_question_payload, _validate_instrument_size_limits,
    _validate_persisted_instrument,
)
from services.survey_governance import has_governance_release
from services.survey_jurisdiction import SurveyJurisdictionError, record_content_receipt, survey_content_document
from services.plan_access import plan_allows_integration_feature
from utils.roles import is_authorized_superadmin_user

PREVIEW_VERSION = "surveys.editorial_relocation_preview.v1"
RECEIPT_VERSION = "surveys.editorial_relocation_receipt.v1"
RESTORE_PREVIEW_VERSION = "surveys.editorial_restore_preview.v1"
RESTORE_RECEIPT_VERSION = "surveys.editorial_restore_receipt.v1"
EVENT_TYPE = "survey_editorial_relocation"
RESTORE_EVENT_TYPE = "survey_editorial_restore"
ARCHIVE_ITEM_EVENT_TYPE = "survey_editorial_archive_item"
MAX_SURVEYS = 5
_STATES = {"borrador", "publicada", "cerrada"}
_COPY_FIELDS = (
    "titulo", "descripcion", "tipo", "puntos_recompensa", "inicio_at", "fin_at",
    "requiere_identidad", "politica_unicidad", "anonimo_permitido", "privacy_mode",
    "privacy_policy_version", "privacy_policy_url", "privacy_consent_required",
    "response_retention_days", "es_votacion_envivo", "mostrar_resultados_envivo",
    "permitir_comentarios",
)
UI = {
    "title": "Reubicar contenido de encuestas",
    "description": "Crea borradores en otra organización y archiva los originales conservando su historia.",
    "confirm_label": "Crear borradores y archivar originales",
    "preservation_notice": "Las respuestas y los recibos originales se conservan en el historial de la organización de origen. No se copian participaciones.",
    "draft_notice": "Los nuevos borradores necesitan revisión institucional antes de su publicación.",
    "published_warning": "Archivar retira las encuestas de los listados operativos y bloquea nuevas respuestas públicas.",
    "response_count_label": "Respuestas históricas totales",
    "source_history_label": "Respuestas conservadas en origen",
    "destination_count_label": "Respuestas en borrador",
    "selection_label": "Encuestas de origen",
    "source_label": "Organización de origen",
    "target_label": "Organización de destino",
    "preview_label": "Revisar traslado",
    "cancel_label": "Cancelar",
    "loading_label": "Consultando información actual",
    "history_label": "Consultar historial archivado",
    "restore_label": "Restaurar originales al origen",
    "restore_title": "Restaurar encuestas archivadas",
    "restore_confirm_label": "Restaurar sin publicar",
    "restore_notice": "La restauración conserva respuestas y borradores de destino. Los originales vuelven como cerrados o borradores; no se publica ninguna encuesta.",
    "restore_success_message": "Los originales se restauraron en la organización de origen sin volver a publicarse. Se conserva su historia y los borradores de destino.",
    "check_status_label": "Consultar resultado",
    "uncertain_message": "El resultado todavía no está confirmado. Conservá esta operación y consultá su estado; no repitas el envío.",
    "conflict_message": "La información cambió. Revisá nuevamente las encuestas antes de confirmar una nueva operación.",
    "success_message": "Los originales quedaron archivados y los nuevos borradores están disponibles para revisión.",
    "directory_error": "No se pudo verificar la organización de destino.",
    "preview_error": "No se pudo verificar la información de las encuestas.",
}


def _error(message, reason, status=409):
    return EncuestaError(message, status_code=status, payload={
        "contract_version": RECEIPT_VERSION, "reason_code": reason, "retryable": False,
    })


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _integer(value, *, allow_text=False, zero=False):
    if allow_text and isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        value = int(value)
    if type(value) is not int or value < (0 if zero else 1):
        raise _error("La referencia numérica no es válida.", "relocation_request_invalid", 400)
    return value


def require_superadmin(actor):
    """Also guard app-service callers; route authentication remains mandatory."""
    actor_id = getattr(actor, "id", None)
    user = db.session.get(User, actor_id) if type(actor_id) is int and actor_id > 0 else None
    if user is not None:
        db.session.refresh(user)
    from utils.auth_helpers import is_user_auth_disabled
    if user is None or is_user_auth_disabled(user) or not is_authorized_superadmin_user(user):
        raise _error("Esta operación requiere una sesión SuperAdmin autorizada.", "relocation_superadmin_required", 403)
    return user


def action_descriptor(actor):
    if not is_authorized_superadmin_user(actor):
        return None
    return {
        "contract_version": "surveys.editorial_relocation_action.v1",
        "can_preview": True, "max_surveys": MAX_SURVEYS,
        "label": "Reubicar contenido", "description": UI["description"], "ui": deepcopy(UI),
    }


def _tenants(source_id, target_id, target_slug):
    source_id = _integer(source_id)
    target_id = _integer(target_id, allow_text=True)
    source = db.session.get(TenantProfile, source_id)
    target = db.session.get(TenantProfile, target_id)
    if (source is None or target is None or source.is_active is not True
            or target.is_active is not True or source_id == target_id
            or not isinstance(target_slug, str) or target.slug != target_slug):
        raise _error("Las organizaciones canónicas no coinciden con la selección.", "relocation_tenant_conflict", 409)
    if not all(plan_allows_integration_feature(tenant, "surveys_votings") for tenant in (source, target)):
        raise _error("El módulo de encuestas no está habilitado para las organizaciones seleccionadas.", "relocation_module_unavailable", 403)
    return source, target


def _tenant_payload(tenant):
    return {"id": int(tenant.id), "slug": tenant.slug, "nombre": tenant.nombre}


def _ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_SURVEYS:
        raise _error("Seleccioná entre una y cinco encuestas.", "relocation_request_invalid", 400)
    ids = [_integer(value) for value in values]
    if len(set(ids)) != len(ids):
        raise _error("La selección contiene encuestas repetidas.", "relocation_request_invalid", 400)
    return sorted(ids)


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
        raise _error("La clave de operación no es válida.", "relocation_request_invalid", 400)
    return value


def _survey(survey_id, source_id):
    survey = db.session.get(EncEncuesta, survey_id)
    if survey is None or int(survey.tenant_id) != source_id:
        raise _error("La encuesta no pertenece a la organización de origen.", "relocation_source_scope_mismatch", 403)
    return survey


def _iso(value):
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


def editorial_hash(survey):
    # Include schedules and revision in addition to the content-receipt document.
    return _sha({
        "content": survey_content_document(survey), "inicio_at": _iso(survey.inicio_at),
        "fin_at": _iso(survey.fin_at), "structure_revision": survey.structure_revision,
    })


def _response_count(survey):
    # All time and all origins, including inconsistent legacy tenant hints.
    return int(db.session.query(func.count(EncRespuesta.id)).filter(
        EncRespuesta.encuesta_id == survey.id).scalar() or 0)


def _item(survey):
    reason = None
    if survey.estado not in _STATES:
        reason = "relocation_source_not_active"
    elif has_governance_release(survey):
        # Never bypass immutable release policy or its PostgreSQL triggers.
        reason = "relocation_governance_archive_policy_required"
    _validate_persisted_instrument(survey)
    return {
        "survey_id": int(survey.id), "title": survey.titulo, "state": survey.estado,
        "structure_revision": int(survey.structure_revision), "editorial_sha256": editorial_hash(survey),
        "response_count_all_time": _response_count(survey), "can_archive": reason is None,
        "reason_code": reason,
    }


def preview_relocation(actor, source_id, target_id, target_slug, survey_ids):
    require_superadmin(actor)
    source, target = _tenants(source_id, target_id, target_slug)
    items = [_item(_survey(identifier, source.id)) for identifier in _ids(survey_ids)]
    return {
        "contract_version": PREVIEW_VERSION, "source_tenant": _tenant_payload(source),
        "target_tenant": _tenant_payload(target), "items": items,
        "response_count_all_time": sum(item["response_count_all_time"] for item in items),
        "can_apply": all(item["can_archive"] for item in items), "ui": deepcopy(UI),
        "copied_responses": False, "destination_state": "borrador",
    }


def _audit(source_id, key):
    rows = AuditEvent.query.filter_by(tenant_id=source_id,
        resource_type="survey_batch", resource_id=hashlib.sha256(key.encode("utf-8")).hexdigest()).filter(
        AuditEvent.event_type.in_([EVENT_TYPE, RESTORE_EVENT_TYPE])).limit(2).all()
    if len(rows) > 1:
        raise _error("El recibo de la operación requiere revisión.", "relocation_receipt_integrity_failed")
    return rows[0] if rows else None


def _receipt(event):
    details = event.details
    result = details.get("receipt") if isinstance(details, dict) else None
    if not isinstance(result, dict) or details.get("receipt_sha256") != _sha(result) or result.get("contract_version") not in {RECEIPT_VERSION, RESTORE_RECEIPT_VERSION}:
        raise _error("El recibo de la operación requiere revisión.", "relocation_receipt_integrity_failed")
    return deepcopy(result)


def relocation_status(actor, source_id, key):
    require_superadmin(actor)
    event = _audit(_integer(source_id), _key(key))
    if event is None:
        raise _error("No hay un resultado confirmado para esta operación. Conservá la clave y consultá nuevamente su estado.", "relocation_receipt_not_found", 404)
    return _receipt(event)


def _preconditions(entries, states):
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise _error("Las precondiciones no son válidas.", "relocation_request_invalid", 400)
    _ids([entry.get("survey_id") for entry in entries])
    expected_fields = {"survey_id", "expected_state", "expected_structure_revision", "expected_editorial_sha256", "expected_response_count_all_time"}
    for entry in entries:
        if (set(entry) != expected_fields or not isinstance(entry.get("expected_state"), str)
                or entry["expected_state"] not in states
                or not isinstance(entry["expected_editorial_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["expected_editorial_sha256"])):
            raise _error("Las precondiciones no son válidas.", "relocation_request_invalid", 400)
        _integer(entry["expected_structure_revision"])
        _integer(entry["expected_response_count_all_time"], zero=True)
    return sorted(deepcopy(entries), key=lambda entry: entry["survey_id"])


def _request(payload, source_id):
    fields = {"source_tenant_id", "target_tenant_id", "target_tenant_slug", "surveys", "idempotency_key", "confirmation"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise _error("El pedido de traslado no es válido.", "relocation_request_invalid", 400)
    if _integer(payload["source_tenant_id"]) != source_id:
        raise _error("El origen no coincide con el tenant de la sesión.", "relocation_source_scope_mismatch", 403)
    if payload["confirmation"] != "archive_originals_create_drafts":
        raise _error("La confirmación de archivo y borradores es requerida.", "relocation_confirmation_required", 400)
    normalized = deepcopy(payload)
    normalized["surveys"] = _preconditions(payload["surveys"], _STATES)
    _key(payload["idempotency_key"])
    _integer(payload["target_tenant_id"])
    return normalized


def _clone(source, target, actor, operation_id):
    _enforce_survey_tenant_quota(target.id)
    _validate_instrument_size_limits([_persisted_question_payload(q) for q in source.preguntas])
    clone = EncEncuesta(
        **{field: deepcopy(getattr(source, field)) for field in _COPY_FIELDS},
        tenant_id=target.id, estado="borrador", created_by=actor.id,
        content_origin="duplicate", content_origin_ref=f"editorial-relocation:{operation_id}:survey:{source.id}",
        document_ref=f"survey-editorial-copy:{operation_id}:{source.id}", jurisdiction_ref=None,
        slug=_generate_unique_slug(f"survey-copy-{operation_id[:12]}-{source.id}"),
    )
    for question in source.preguntas:
        copied_question = EncPregunta(
            **{field: deepcopy(getattr(question, field)) for field in (
                "orden", "logical_ref", "tipo", "texto", "obligatoria", "min_selecciones", "max_selecciones", "logica_condicional")})
        for option in question.opciones:
            copied_question.opciones.append(EncOpcion(
                **{field: deepcopy(getattr(option, field)) for field in ("orden", "logical_ref", "texto", "valor")}))
        clone.preguntas.append(copied_question)
    for segment in source.segmentos:
        # Seed configuration is executable sandbox behavior, not editorial copy.
        if segment.clave != "auto_seed_demo":
            clone.segmentos.append(EncSegmento(clave=segment.clave, valor=deepcopy(segment.valor)))
    db.session.add(clone)
    record_content_receipt(clone, event_type="created", decision="recorded",
        actor_user_id=actor.id, reason_code="survey_editorial_relocation_copy")
    return clone


def _lock_tenants(identifiers):
    """Serialize operation keys without blocking a responder's tenant FK.

    Advisory locks serialize our tenant key namespace. FOR SHARE keeps the
    license/tenant state stable and permits the responder receipt's FK check.
    The PostgreSQL comparison also verifies preservation with the previous
    no-op UPDATE; it does not reproduce a deadlock for that statement.
    SQLite retains its existing real database writer lock.
    """
    dialect = db.session.get_bind().dialect.name
    for identifier in sorted(set(identifiers)):
        if dialect == "postgresql":
            db.session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": f"surveys.editorial-relocation.tenant.v1:{identifier}"})
            found = db.session.execute(text("SELECT id FROM tenant_profile WHERE id = :id FOR SHARE"),
                {"id": identifier}).scalar_one_or_none()
        elif dialect == "sqlite":
            result = db.session.execute(text("UPDATE tenant_profile SET id = id WHERE id = :id"),
                {"id": identifier})
            found = identifier if result.rowcount == 1 else None
        else:
            raise _error("El almacenamiento no admite este bloqueo seguro.", "relocation_storage_unsupported", 503)
        if found is None:
            raise _error("La organización no existe.", "relocation_tenant_conflict")
        db.session.expire(db.session.get(TenantProfile, identifier))


def apply_relocation(actor, source_id, payload, header_key):
    actor = require_superadmin(actor)
    source_id = _integer(source_id)
    normalized = _request(payload, source_id)
    key = _key(header_key)
    if key != normalized["idempotency_key"]:
        raise _error("Las claves de operación no coinciden.", "relocation_idempotency_conflict", 400)
    fingerprint = _sha(normalized)
    try:
        _lock_tenants({source_id, normalized["target_tenant_id"]})
        source, target = _tenants(source_id, normalized["target_tenant_id"], normalized["target_tenant_slug"])
        actor = require_superadmin(actor)
        previous = _audit(source_id, key)
        if previous is not None:
            if previous.details.get("request_sha256") != fingerprint:
                raise _error("La clave ya pertenece a otro pedido.", "relocation_idempotency_conflict")
            receipt = _receipt(previous)
            db.session.rollback()  # release lock-only transaction; no new effects
            return receipt, True
        locked = []
        for expected in normalized["surveys"]:
            _survey(expected["survey_id"], source_id)
            survey = _acquire_encuesta_write_guard(expected["survey_id"])
            _survey(survey.id, source_id)
            item = _item(survey)
            if not item["can_archive"]:
                raise _error("La encuesta requiere otra política de archivo.", item["reason_code"])
            if any(item[field] != expected[expected_field] for field, expected_field in (
                    ("state", "expected_state"), ("structure_revision", "expected_structure_revision"),
                    ("editorial_sha256", "expected_editorial_sha256"), ("response_count_all_time", "expected_response_count_all_time"))):
                raise _error("La encuesta cambió después de la revisión.", "relocation_precondition_failed")
            locked.append((survey, item))
        operation_id = str(uuid.uuid4())
        items = []
        for survey, item in locked:
            clone = _clone(survey, target, actor, operation_id)
            survey.estado = "archivada"
            items.append({
                "source_survey_id": int(survey.id), "destination_survey_id": int(clone.id),
                "original_previous_state": item["state"], "source_state": "archivada", "destination_state": "borrador",
                "source_response_count_all_time": item["response_count_all_time"], "destination_response_count_all_time": 0,
                "source_editorial_sha256": item["editorial_sha256"], "excluded_runtime_segment_keys": ["auto_seed_demo"] if any(s.clave == "auto_seed_demo" for s in survey.segmentos) else [],
            })
        receipt = {
            "contract_version": RECEIPT_VERSION, "state": "originals_archived_destination_drafts_created",
            "operation_id": operation_id, "idempotency_key": key, "source_tenant": _tenant_payload(source),
            "target_tenant": _tenant_payload(target), "items": items, "copied_responses": False,
            "response_count_all_time": sum(item["source_response_count_all_time"] for item in items),
            "ui": deepcopy(UI),
        }
        for item in items:
            lineage = {"archive_idempotency_key": key, "archive_operation_id": operation_id,
                "source_editorial_sha256": item["source_editorial_sha256"]}
            db.session.add(AuditEvent(tenant_id=source_id, actor_user_id=actor.id,
                event_type=ARCHIVE_ITEM_EVENT_TYPE, resource_type="survey", resource_id=str(item["source_survey_id"]),
                details={"lineage": lineage, "lineage_sha256": _sha(lineage)}))
        db.session.add(AuditEvent(tenant_id=source_id, actor_user_id=actor.id,
            event_type=EVENT_TYPE, resource_type="survey_batch", resource_id=hashlib.sha256(key.encode("utf-8")).hexdigest(),
            details={"request_sha256": fingerprint, "receipt": receipt, "receipt_sha256": _sha(receipt)}))
        db.session.commit()
        return receipt, False
    except SurveyJurisdictionError as exc:
        db.session.rollback()
        raise _encuesta_jurisdiction_error(exc) from exc
    except IntegrityError as exc:
        db.session.rollback()
        raise _error("No se pudo confirmar el lote completo.", "relocation_transaction_conflict") from exc
    except Exception:
        db.session.rollback()
        raise


def archive_history_metadata(source_id, surveys):
    """Bounded metadata for the explicit, authenticated archive list."""
    ids = {str(survey.id) for survey in surveys if survey.estado == "archivada"}
    if not ids:
        return {}
    events = AuditEvent.query.filter_by(tenant_id=source_id, event_type=ARCHIVE_ITEM_EVENT_TYPE,
        resource_type="survey").filter(AuditEvent.resource_id.in_(ids)).order_by(AuditEvent.id.desc()).all()
    result = {}
    for event in events:
        details = event.details if isinstance(event.details, dict) else {}
        lineage = details.get("lineage")
        if (event.resource_id not in result and isinstance(lineage, dict)
                and details.get("lineage_sha256") == _sha(lineage)):
            result[event.resource_id] = deepcopy(lineage)
    return result


def _original_archive(source_id, key):
    event = _audit(source_id, _key(key))
    if event is None:
        raise _error("No hay un archivo confirmado para esta operación.", "relocation_receipt_not_found", 404)
    receipt = _receipt(event)
    if receipt["contract_version"] != RECEIPT_VERSION or receipt["source_tenant"]["id"] != source_id:
        raise _error("El recibo no corresponde al archivo de origen.", "restore_archive_receipt_conflict")
    return receipt


def _restore_item(survey, original):
    count = _response_count(survey)
    return {
        "survey_id": int(survey.id), "title": survey.titulo, "state": survey.estado,
        "structure_revision": int(survey.structure_revision), "editorial_sha256": editorial_hash(survey),
        "response_count_all_time": count,
        # An original published survey is never reopened by recovery.
        "restore_state": "borrador" if original["original_previous_state"] == "borrador" and count == 0 else "cerrada",
        "can_restore": survey.estado == "archivada" and not has_governance_release(survey),
    }


def preview_restore(actor, source_id, archive_key):
    require_superadmin(actor)
    source_id = _integer(source_id)
    source = db.session.get(TenantProfile, source_id)
    if source is None or source.is_active is not True:
        raise _error("La organización de origen no está disponible.", "relocation_tenant_conflict")
    archive = _original_archive(source_id, archive_key)
    items = [_restore_item(_survey(item["source_survey_id"], source_id), item) for item in archive["items"]]
    return {"contract_version": RESTORE_PREVIEW_VERSION, "source_tenant": _tenant_payload(source),
        "archive_operation_id": archive["operation_id"], "archive_idempotency_key": archive_key,
        "items": items, "can_apply": all(item["can_restore"] for item in items), "ui": deepcopy(UI),
        "published": False, "preserved_responses": True}


def restore_originals(actor, source_id, payload, header_key):
    actor = require_superadmin(actor)
    source_id = _integer(source_id)
    fields = {"source_tenant_id", "archive_idempotency_key", "idempotency_key", "confirmation", "surveys"}
    if not isinstance(payload, dict) or set(payload) != fields or _integer(payload["source_tenant_id"]) != source_id:
        raise _error("Las referencias de restauración no coinciden.", "restore_source_scope_mismatch", 403)
    if payload["confirmation"] != "restore_originals_without_publishing":
        raise _error("La restauración nunca permite volver a publicar.", "restore_confirmation_required", 400)
    normalized = deepcopy(payload)
    normalized["surveys"] = _preconditions(payload["surveys"], {"archivada"})
    key = _key(header_key)
    if key != _key(payload["idempotency_key"]) or key == _key(payload["archive_idempotency_key"]):
        raise _error("Las claves de archivo y restauración no son válidas.", "relocation_idempotency_conflict", 400)
    fingerprint = _sha(normalized)
    try:
        _lock_tenants({source_id})
        source = db.session.get(TenantProfile, source_id)
        db.session.refresh(source)
        if source.is_active is not True:
            raise _error("La organización de origen no está disponible.", "relocation_tenant_conflict")
        actor = require_superadmin(actor)
        previous = _audit(source_id, key)
        if previous is not None:
            if previous.details.get("request_sha256") != fingerprint:
                raise _error("La clave pertenece a otra operación.", "relocation_idempotency_conflict")
            receipt = _receipt(previous)
            db.session.rollback()
            return receipt, True
        archive = _original_archive(source_id, payload["archive_idempotency_key"])
        original_by_id = {item["source_survey_id"]: item for item in archive["items"]}
        if set(original_by_id) != {entry["survey_id"] for entry in normalized["surveys"]}:
            raise _error("La restauración requiere el lote original completo.", "restore_archive_receipt_conflict")
        items = []
        for expected in normalized["surveys"]:
            _survey(expected["survey_id"], source_id)
            survey = _acquire_encuesta_write_guard(expected["survey_id"])
            _survey(survey.id, source_id)
            item = _restore_item(survey, original_by_id[survey.id])
            if not item["can_restore"] or any(item[field] != expected[expected_field] for field, expected_field in (
                    ("state", "expected_state"), ("structure_revision", "expected_structure_revision"),
                    ("editorial_sha256", "expected_editorial_sha256"), ("response_count_all_time", "expected_response_count_all_time"))):
                raise _error("El archivo cambió después de la revisión.", "restore_precondition_failed")
            survey.estado = item["restore_state"]
            items.append({"source_survey_id": survey.id, "source_state": item["restore_state"],
                "source_editorial_sha256": item["editorial_sha256"],
                "source_response_count_all_time": item["response_count_all_time"]})
        receipt = {"contract_version": RESTORE_RECEIPT_VERSION, "state": "originals_restored_without_publishing",
            "operation_id": str(uuid.uuid4()), "archive_operation_id": archive["operation_id"],
            "archive_idempotency_key": payload["archive_idempotency_key"],
            "idempotency_key": key, "source_tenant": _tenant_payload(source), "items": items,
            "preserved_responses": True, "published": False, "destination_drafts_unchanged": True, "ui": deepcopy(UI)}
        db.session.add(AuditEvent(tenant_id=source_id, actor_user_id=actor.id, event_type=RESTORE_EVENT_TYPE,
            resource_type="survey_batch", resource_id=hashlib.sha256(key.encode("utf-8")).hexdigest(),
            details={"request_sha256": fingerprint, "receipt": receipt, "receipt_sha256": _sha(receipt)}))
        db.session.commit()
        return receipt, False
    except Exception:
        db.session.rollback()
        raise
