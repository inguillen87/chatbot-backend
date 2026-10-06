"""Persistent, bounded technical rehearsals; never official survey responses.

This service does not relax Preview demo gates or government publication.
The fixed instrument and its authority live in a signed creation AuditEvent;
votes use the existing append-only interactive_demo table exclusively.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import uuid

from flask import current_app
from sqlalchemy import text, update
from sqlalchemy.engine import make_url

from database import db
from models import AuditEvent, DemoSurveyParticipation, TenantProfile, User
from global_writer_authority import configured_writer_runtime, global_writer_authority_enabled
from services.global_writer_authority import global_writer_authority_lease
from services.plan_access import plan_allows_integration_feature
from services.auth_session_lifecycle import request_auth_session_active
from services.auth_assurance_service import AuthAssuranceError, STRICT_MFA, require_request_auth_assurance
from utils.auth_helpers import is_user_auth_disabled
from utils.roles import is_authorized_superadmin_user
from utils.tenant_admin_access import can_manage_tenant_control_plane, resolve_consistent_user_tenant

CONTRACT = "surveys.production_rehearsal.v1"
CREATED = "survey.production_rehearsal.created.v1"
ACCEPTED = "survey.production_rehearsal.accepted.v1"
RESOURCE = "production_survey_rehearsal"
MAX_RESPONSES = 20
MAX_ACTIVE_RUNS = 3
MAX_BODY_BYTES = 2048
EXPECTED_WRITER_EPOCH = 6
QUESTION = {"id": "technical_form_v1", "type": "single", "label": "¿Pudiste utilizar este formulario de prueba?",
            "options": [{"id": "yes", "label": "Sí"}, {"id": "no", "label": "No"}]}
WARNING = "Prueba técnica, sin valor de consulta oficial"
_RUN = re.compile(r"^rehearsal_[a-f0-9]{32}$")


class RehearsalError(Exception):
    def __init__(self, reason: str, status: int = 409):
        self.reason_code, self.status_code = reason, status
        super().__init__(reason)

    def to_dict(self):
        return {"contract_version": CONTRACT, "ok": False, "reason_code": self.reason_code,
                "message": "No se pudo completar esta prueba técnica.", "official": False,
                "retryable": self.status_code == 503}


def _now():
    return datetime.now(timezone.utc)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _hmac(domain, value):
    secret = str(current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    if len(secret) < 32:
        raise RehearsalError("rehearsal_signing_unavailable", 503)
    key = hmac.new(secret, (CONTRACT + ":" + domain).encode("ascii"), hashlib.sha256).digest()
    return hmac.new(key, _canonical(value).encode("utf-8"), hashlib.sha256).hexdigest()


def require_production_runtime():
    """No testing/dev/Render/Preview fallback and no credentials in diagnostics."""
    if (str(os.getenv("VERCEL") or "").lower() not in {"1", "true"}
        or os.getenv("VERCEL_ENV") != "production"
        or str(os.getenv("RENDER") or "").lower() in {"1", "true"}
        or current_app.config.get("TESTING")
        or str(os.getenv("TESTING") or "").lower() in {"1", "true"}
        or not global_writer_authority_enabled(current_app.config)
        or configured_writer_runtime(current_app.config) != "vercel"):
        raise RehearsalError("rehearsal_production_runtime_required", 503)
    try:
        uri = make_url(current_app.config.get("SQLALCHEMY_DATABASE_URI") or "")
        valid = (uri.get_backend_name() == "postgresql"
                 and str(uri.host or "").endswith(".neon.tech")
                 and uri.query.get("sslmode") == "verify-full")
    except Exception:
        valid = False
    if not valid:
        raise RehearsalError("rehearsal_neon_tls_required", 503)


@contextmanager
def _writer():
    require_production_runtime()
    with global_writer_authority_lease(current_app.config) as lease:
        if not lease.decision.allowed or not lease.decision.enabled or lease.decision.epoch != EXPECTED_WRITER_EPOCH:
            raise RehearsalError("rehearsal_writer_authority_unavailable", 503)
        yield lease


def _commit(lease, actor_id, tenant, *, superadmin=False):
    decision = lease.revalidate(current_app.config)
    if not decision.allowed or not decision.enabled or decision.epoch != EXPECTED_WRITER_EPOCH:
        raise RehearsalError("rehearsal_writer_authority_unavailable", 503)
    actor = _actor(actor_id)
    if superadmin:
        if not is_authorized_superadmin_user(actor):
            raise RehearsalError("rehearsal_superadmin_required", 403)
    else:
        _participation_scope(actor, tenant)
    db.session.commit()


def _actor(actor_id):
    user = db.session.get(User, actor_id, populate_existing=True)
    if user is None or is_user_auth_disabled(user) or not request_auth_session_active(user.id):
        raise RehearsalError("rehearsal_auth_session_required", 401)
    return user


def _participation_scope(user, tenant):
    if is_authorized_superadmin_user(user):
        return
    # One server-resolved primary/direct/legacy membership; request selectors
    # and signed historical tenant claims do not grant a different tenant.
    membership = resolve_consistent_user_tenant(user)
    if membership is None or membership.id != tenant.id:
        raise RehearsalError("rehearsal_tenant_scope_forbidden", 403)


def _tenant(slug, *, lock=False):
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", slug):
        raise RehearsalError("rehearsal_not_found", 404)
    query = TenantProfile.query.filter_by(slug=slug).populate_existing()
    tenant = (query.with_for_update(read=True) if lock else query).one_or_none()
    if tenant is None:
        raise RehearsalError("rehearsal_not_found", 404)
    if tenant.is_active is not True or not plan_allows_integration_feature(tenant, "surveys_votings"):
        raise RehearsalError("rehearsal_license_required", 403)
    return tenant


def _lock(tenant_id):
    dialect = db.session.get_bind().dialect.name
    if dialect == "postgresql":
        db.session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                           {"scope": f"{CONTRACT}:{tenant_id}"})
    elif dialect == "sqlite":
        # A real local SQLite writer lock, not a Python process mutex.
        db.session.execute(update(TenantProfile).where(TenantProfile.id == tenant_id)
                           .values(id=TenantProfile.id).execution_options(synchronize_session=False))
    else:
        raise RehearsalError("rehearsal_storage_unsupported", 503)


def _proof(event, tenant, *, active=True):
    details = event.details if isinstance(event.details, dict) else {}
    body = details.get("authority")
    expected_keys = {"contract_version", "run_id", "tenant_id", "tenant_slug", "instrument_sha256",
                     "created_at", "expires_at", "mode", "max_responses"}
    if (not isinstance(body, dict) or set(body) != expected_keys
        or body.get("contract_version") != CONTRACT
        or body.get("tenant_id") != tenant.id or body.get("tenant_slug") != tenant.slug
        or body.get("run_id") != event.resource_id or not _RUN.fullmatch(str(body.get("run_id") or ""))
        or event.tenant_id != tenant.id or event.event_type != CREATED or event.resource_type != RESOURCE
        or body.get("mode") != "technical_rehearsal" or body.get("max_responses") != MAX_RESPONSES
        or body.get("instrument_sha256") != _digest(QUESTION)
        or not hmac.compare_digest(str(details.get("authority_hmac") or ""), _hmac("authority", body))):
        raise RehearsalError("rehearsal_authority_invalid", 503)
    creator = db.session.get(User, event.actor_user_id, populate_existing=True)
    if creator is None or is_user_auth_disabled(creator) or not is_authorized_superadmin_user(creator):
        raise RehearsalError("rehearsal_authority_invalid", 503)
    try:
        created, expires = (datetime.fromisoformat(body[field]) for field in ("created_at", "expires_at"))
        valid_dates = created.tzinfo is not None and expires.tzinfo is not None and expires - created == timedelta(hours=24)
    except (ValueError, TypeError):
        valid_dates = False
    event_created = event.created_at
    if event_created is not None and event_created.tzinfo is None:
        event_created = event_created.replace(tzinfo=timezone.utc)
    if not valid_dates or created > _now() or event_created != created:
        raise RehearsalError("rehearsal_authority_invalid", 503)
    if active and expires <= _now():
        raise RehearsalError("rehearsal_expired", 410)
    return body


def _run(tenant, run_id):
    if not _RUN.fullmatch(str(run_id or "")):
        raise RehearsalError("rehearsal_not_found", 404)
    events = AuditEvent.query.filter_by(tenant_id=tenant.id, resource_id=run_id,
                                      resource_type=RESOURCE, event_type=CREATED).limit(2).all()
    if not events:
        raise RehearsalError("rehearsal_not_found", 404)
    if len(events) != 1:
        raise RehearsalError("rehearsal_authority_invalid", 503)
    return _proof(events[0], tenant)


def _metrics(body):
    rows = DemoSurveyParticipation.query.filter_by(survey_slug=body["run_id"]).limit(MAX_RESPONSES + 1).all()
    counts = {"yes": 0, "no": 0}
    for row in rows:
        if (row.option_id not in counts or row.tenant_slug != body["tenant_slug"]
            or row.instrument_sha256 != body["instrument_sha256"] or row.response_origin != "interactive_demo"
            or row.sector != "technical_rehearsal" or row.question_id != QUESTION["id"] or row.instrument_revision != 1):
            raise RehearsalError("rehearsal_storage_inconsistent", 503)
        counts[row.option_id] += 1
    if sum(counts.values()) > MAX_RESPONSES:
        raise RehearsalError("rehearsal_storage_inconsistent", 503)
    return {"total_responses": sum(counts.values()),
            "options": [{"option_id": option, "count": count} for option, count in counts.items()]}


def _metadata(body):
    base = f"/api/v2/public/tenants/{body['tenant_slug']}/survey-rehearsals/{body['run_id']}"
    metrics = _metrics(body)
    return {"contract_version": CONTRACT, "mode": "technical_rehearsal", "run_id": body["run_id"],
            "tenant_slug": body["tenant_slug"], "instrument_sha256": body["instrument_sha256"],
            "expires_at": body["expires_at"], "max_responses": MAX_RESPONSES,
            "authentication_required": True, "one_account_per_run": True, "unique_person_certified": False,
            "official": False, "result_certified": False, "seeded_responses": 0, "persisted": True,
            "response_origin": "interactive_demo", "question": deepcopy(QUESTION),
            "branding": {"tenant_slug": body["tenant_slug"], "display_name": _tenant(body["tenant_slug"]).nombre},
            "ui": {"title": "Prueba técnica", "label": "Formulario de prueba persistente", "warning": WARNING,
                   "description": "Una participación por cuenta. Las respuestas se conservan aparte de las consultas oficiales.",
                   "submit_label": "Guardar participación de prueba", "login_label": "Iniciar sesión para participar",
                   "refresh_label": "Actualizar resultados", "results_label": "Resultados de la prueba",
                   "total_label": "Participaciones guardadas", "expires_label": "Vigente hasta",
                   "read_at_label": "Última lectura de resultados",
                   "limit_label": "Límite de participaciones", "check_status_label": "Consultar mi participación",
                   "uncertain_message": "La confirmación está pendiente. Consultá el estado sin volver a enviar.",
                   "error_message": "No se pudo completar esta prueba. Conservá tu intención y consultá su estado."},
            "links": {"metadata_api": base, "respond_api": base + "/respond", "results_api": base + "/results"},
            "metrics": metrics, "result_version": _digest({"run_id": body["run_id"], "metrics": metrics}),
            "refresh": {"polling_enabled": True, "interval_ms": 5000, "socket_delivery_proven": False}}


def read_rehearsal(slug, run_id):
    require_production_runtime()
    return _metadata(_run(_tenant(slug), run_id))


def list_rehearsals(slug, actor):
    require_production_runtime()
    actor = _actor(actor.id)
    tenant = _tenant(slug)
    if not can_manage_tenant_control_plane(actor, tenant):
        raise RehearsalError("rehearsal_tenant_scope_forbidden", 403)
    rows = (AuditEvent.query.filter_by(tenant_id=tenant.id, event_type=CREATED, resource_type=RESOURCE)
            .filter(AuditEvent.created_at >= _now() - timedelta(hours=24)).order_by(AuditEvent.id.desc()).limit(4).all())
    items = []
    for row in rows:
        body = _proof(row, tenant, active=False)
        if datetime.fromisoformat(body["expires_at"]) > _now():
            items.append(_metadata(body))
    if len(items) > MAX_ACTIVE_RUNS:
        raise RehearsalError("rehearsal_storage_inconsistent", 503)
    is_superadmin = is_authorized_superadmin_user(actor)
    mfa_ready = False
    if is_superadmin:
        try:
            require_request_auth_assurance(STRICT_MFA)
            mfa_ready = True
        except AuthAssuranceError:
            pass
    can_create = is_superadmin and mfa_ready and len(items) < MAX_ACTIVE_RUNS
    blocked = ("rehearsal_superadmin_required" if not is_superadmin else
               "step_up_required" if not mfa_ready else
               "rehearsal_active_run_limit" if len(items) >= MAX_ACTIVE_RUNS else None)
    return {"contract_version": CONTRACT, "items": items, "max_active_runs": MAX_ACTIVE_RUNS,
            "source_tenant": {"slug": tenant.slug, "display_name": tenant.nombre, "canonical": True},
            "create_action": {"contract_version": "surveys.production_rehearsal.create_action.v1",
                "can_create": can_create, "requires_strict_mfa": True, "method": "POST",
                "api_path": f"/api/v2/tenants/{tenant.slug}/survey-rehearsals",
                "blocked_reason_code": blocked,
                "ui": {"label": "Crear prueba técnica", "description": "Verificá ambos factores para crear una prueba." if blocked == "step_up_required" else "Una pregunta genérica, hasta 20 participaciones y 24 horas de vigencia."}},
            "official": False, "ui": {"title": "Pruebas técnicas", "warning": WARNING,
                "open_label": "Abrir formulario de prueba", "refresh_label": "Actualizar pruebas",
                "check_status_label": "Consultar la prueba solicitada",
                "uncertain_message": "La confirmación está pendiente. Consultá el estado sin crear otra prueba.",
                "error_message": "No se pudo completar esta prueba. Conservá tu intención y consultá su estado."}}


def _checked_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", key):
        raise RehearsalError("rehearsal_submission_id_required", 400)


def _created_by_key(tenant, actor, key):
    digest = _hmac("create-key", [tenant.id, actor.id, key])
    rows = (AuditEvent.query.filter_by(tenant_id=tenant.id, actor_user_id=actor.id,
                                      event_type=CREATED, resource_type=RESOURCE)
            .filter(AuditEvent.details["request_digest"].as_string() == digest).limit(2).all())
    if len(rows) > 1:
        raise RehearsalError("rehearsal_receipt_inconsistent", 503)
    return _proof(rows[0], tenant) if rows else None


def find_created_rehearsal(slug, actor, key):
    require_production_runtime()
    _checked_key(key)
    actor = _actor(actor.id)
    if not is_authorized_superadmin_user(actor):
        raise RehearsalError("rehearsal_superadmin_required", 403)
    body = _created_by_key(_tenant(slug), actor, key)
    if body is None:
        raise RehearsalError("rehearsal_submission_not_observed", 404)
    return {**_metadata(body), "submission_id": key, "replayed": True}


def create_rehearsal(slug, actor, key):
    _checked_key(key)
    if not is_authorized_superadmin_user(actor):
        raise RehearsalError("rehearsal_superadmin_required", 403)
    with _writer() as lease:
        tenant = _tenant(slug)
        _lock(tenant.id)
        tenant = _tenant(slug, lock=True)
        actor = _actor(actor.id)
        if not is_authorized_superadmin_user(actor):
            raise RehearsalError("rehearsal_superadmin_required", 403)
        existing = _created_by_key(tenant, actor, key)
        if existing is not None:
            return {**_metadata(existing), "submission_id": key, "replayed": True}
        request_digest = _hmac("create-key", [tenant.id, actor.id, key])
        rows = (AuditEvent.query.filter_by(tenant_id=tenant.id, event_type=CREATED, resource_type=RESOURCE)
                .filter(AuditEvent.created_at >= _now() - timedelta(hours=24)).all())
        active = 0
        for row in rows:
            body = _proof(row, tenant, active=False)
            active += datetime.fromisoformat(body["expires_at"]) > _now()
        if active >= MAX_ACTIVE_RUNS:
            raise RehearsalError("rehearsal_active_run_limit", 409)
        now = _now()
        body = {"contract_version": CONTRACT, "run_id": "rehearsal_" + uuid.uuid4().hex,
                "tenant_id": tenant.id, "tenant_slug": tenant.slug, "instrument_sha256": _digest(QUESTION),
                "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=24)).isoformat(),
                "mode": "technical_rehearsal", "max_responses": MAX_RESPONSES}
        db.session.add(AuditEvent(tenant_id=tenant.id, actor_user_id=actor.id, event_type=CREATED,
                                 resource_type=RESOURCE, resource_id=body["run_id"], created_at=now,
                                 details={"authority": body, "authority_hmac": _hmac("authority", body),
                                          "request_digest": request_digest}))
        db.session.flush()
        result = {**_metadata(body), "submission_id": key, "replayed": False}
        _commit(lease, actor.id, tenant, superadmin=True)
        return result


def submit_rehearsal(slug, run_id, actor, payload, key, *, rate_check):
    _checked_key(key)
    if (not isinstance(payload, dict) or set(payload) != {"submission_id", "option_id"}
        or payload.get("submission_id") != key or not isinstance(payload.get("option_id"), str)
        or payload.get("option_id") not in {"yes", "no"}):
        raise RehearsalError("rehearsal_invalid_payload", 400)
    with _writer() as lease:
        tenant = _tenant(slug)
        _lock(tenant.id)
        tenant = _tenant(slug, lock=True)
        body = _run(tenant, run_id)
        user = _actor(actor.id)
        _participation_scope(user, tenant)
        identity = _hmac("account", [tenant.id, run_id, user.id])
        request_digest = _hmac("submission-key", [tenant.id, run_id, identity, key])
        payload_hash = _digest(payload)
        existing = DemoSurveyParticipation.query.filter_by(survey_slug=run_id, submission_id_hash=identity).one_or_none()
        if existing is not None:
            events = AuditEvent.query.filter_by(tenant_id=tenant.id, event_type=ACCEPTED, resource_type=RESOURCE,
                                               resource_id=run_id).all()
            matching = [event for event in events if (event.details or {}).get("account_hmac") == identity]
            if len(matching) != 1:
                raise RehearsalError("rehearsal_receipt_inconsistent", 503)
            details = matching[0].details
            if details.get("request_digest") != request_digest:
                raise RehearsalError("rehearsal_account_already_participated", 409)
            if (details.get("payload_hash") != payload_hash or existing.payload_hash != payload_hash
                or existing.option_id != payload["option_id"]
                or existing.tenant_slug != tenant.slug or existing.instrument_sha256 != body["instrument_sha256"]):
                raise RehearsalError("rehearsal_submission_conflict", 409)
            return _ack(body, request_digest, payload_hash, key, existing.option_id, replayed=True)
        rate = rate_check(run_id)
        if not rate.get("available"):
            raise RehearsalError("rehearsal_rate_unavailable", 503)
        if not rate.get("allowed"):
            raise RehearsalError("rehearsal_rate_limited", 429)
        if _metrics(body)["total_responses"] >= MAX_RESPONSES:
            raise RehearsalError("rehearsal_response_limit", 409)
        db.session.add(DemoSurveyParticipation(survey_slug=run_id, tenant_slug=tenant.slug,
                       sector="technical_rehearsal", question_id=QUESTION["id"], option_id=payload["option_id"],
                       submission_id_hash=identity, payload_hash=payload_hash,
                       instrument_sha256=body["instrument_sha256"], instrument_revision=1,
                       response_origin="interactive_demo"))
        db.session.add(AuditEvent(tenant_id=tenant.id, actor_user_id=None, event_type=ACCEPTED,
                       resource_type=RESOURCE, resource_id=run_id,
                       details={"account_hmac": identity, "request_digest": request_digest, "payload_hash": payload_hash,
                                "instrument_sha256": body["instrument_sha256"], "response_origin": "interactive_demo"}))
        db.session.flush()
        result = _ack(body, request_digest, payload_hash, key, payload["option_id"], replayed=False)
        _commit(lease, user.id, tenant)
        return result


def _ack(body, request_digest, payload_hash, key, option, *, replayed):
    return {"contract_version": "surveys.production_rehearsal.response.v1", "run_id": body["run_id"],
            "tenant_slug": body["tenant_slug"], "persisted": True, "replayed": replayed,
            "response_origin": "interactive_demo", "official": False, "unique_person_certified": False,
            "receipt": {"payload_sha256": payload_hash,
                        "instrument_sha256": body["instrument_sha256"], "submission_id": key,
                        "run_id": body["run_id"], "tenant_slug": body["tenant_slug"], "option_id": option,
                        "verified_current_account": True}, "metrics": _metrics(body),
            "ui": {"label": "Participación guardada", "warning": WARNING}}


def find_rehearsal_submission(slug, run_id, actor, key):
    require_production_runtime()
    if key is not None:
        _checked_key(key)
    tenant = _tenant(slug)
    body = _run(tenant, run_id)
    user = _actor(actor.id)
    _participation_scope(user, tenant)
    identity = _hmac("account", [tenant.id, run_id, user.id])
    row = DemoSurveyParticipation.query.filter_by(survey_slug=run_id, submission_id_hash=identity).one_or_none()
    matches = [event for event in AuditEvent.query.filter_by(tenant_id=tenant.id, event_type=ACCEPTED,
               resource_type=RESOURCE, resource_id=run_id).all()
               if (event.details or {}).get("account_hmac") == identity]
    if row is None:
        if matches:
            raise RehearsalError("rehearsal_receipt_inconsistent", 503)
        if key is None:
            return _account_status(body, False)
        raise RehearsalError("rehearsal_submission_not_observed", 404)
    if (len(matches) != 1 or matches[0].details.get("payload_hash") != row.payload_hash
        or row.tenant_slug != tenant.slug or row.instrument_sha256 != body["instrument_sha256"]
        or row.response_origin != "interactive_demo" or row.question_id != QUESTION["id"]
        or row.option_id not in {"yes", "no"}):
        raise RehearsalError("rehearsal_receipt_inconsistent", 503)
    if key is None:
        return _account_status(body, True)
    request_digest = _hmac("submission-key", [tenant.id, run_id, identity, key])
    if matches[0].details.get("request_digest") != request_digest:
        raise RehearsalError("rehearsal_account_already_participated", 409)
    return _ack(body, request_digest, row.payload_hash, key, row.option_id, replayed=True)


def _account_status(body, participated):
    return {"contract_version": "surveys.production_rehearsal.account_status.v1",
            "tenant_slug": body["tenant_slug"], "run_id": body["run_id"],
            "verified_current_account": True, "participated": participated,
            "official": False, "unique_person_certified": False,
            "ui": {"label": "Esta cuenta ya participó" if participated else "Esta cuenta puede participar",
                   "description": "Una participación por cuenta en esta prueba. No acredita una persona única.",
                   "warning": WARNING}}


def storage_error():
    db.session.rollback()
    return RehearsalError("rehearsal_storage_unavailable", 503)
