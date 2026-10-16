"""HTTP deletion precondition, checked under the existing survey write lock.

An earlier UI snapshot is not permission to delete a now-live instrument. All
admin aliases invoke this guard before the existing audited-record protections.
The caller owns commit/rollback; this helper never commits or removes records.
"""
from database import db
from models import EncEncuesta, EncRespuesta
from services.encuestas_service import (
    EncuestaError, _acquire_encuesta_write_guard, _ensure_tenant_access,
)


def require_admin_deletable_survey(survey_id, actor):
    survey = db.session.get(EncEncuesta, survey_id)
    if survey is None:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    # Do not lock or expose a foreign instrument before authorization.
    _ensure_tenant_access(survey, actor)
    survey = _acquire_encuesta_write_guard(survey_id)
    _ensure_tenant_access(survey, actor)
    if survey.estado != "borrador":
        raise EncuestaError(
            "Sólo se puede eliminar un borrador sin participación registrada. "
            "El instrumento y sus respuestas se conservaron.",
            status_code=409,
            payload={"contract_version": "surveys.deletion_policy.v1",
                     "reason_code": "survey_delete_requires_draft",
                     "retryable": False, "action_hint": "refresh_survey_state"},
        )
    # Count no rows, and include every origin/tenant: even an inconsistent or
    # synthetic response must not silently disappear through this operation.
    has_response = db.session.query(EncRespuesta.id).filter(
        EncRespuesta.encuesta_id == survey.id,
    ).first() is not None
    if has_response:
        raise EncuestaError(
            "El borrador tiene participación registrada y no puede eliminarse. "
            "Revisá el instrumento; sus respuestas se conservaron.",
            status_code=409,
            payload={"contract_version": "surveys.deletion_policy.v1",
                     "reason_code": "survey_delete_has_responses",
                     "retryable": False, "action_hint": "review_recorded_participation"},
        )
