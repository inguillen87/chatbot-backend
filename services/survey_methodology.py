"""Authorized, versioned methodology declarations with optimistic concurrency.

A declaration never grants statistical inference or changes questionnaire/results.
Storage requires an explicit migration and feature flag; no runtime DDL.
"""
from datetime import datetime, timezone
import os
import re

from flask import current_app
from database import db
from models import AuditEvent, SurveyMethodologyRevision, User
from services.encuestas_service import EncuestaError, get_encuesta, _ensure_tenant_access, _acquire_encuesta_write_guard
from services.survey_methodology_contract import (
    CONTRACT, UI, HISTORY_LIMIT, canonical_digest, blank_fields, coverage, schema,
    validate_fields, validate_write, MethodologyInputError,
)
from utils.roles import canonical_role, ROLE_TENANT_ADMIN, ROLE_SUPERADMIN
from cutover_writer_fence import cutover_writer_fence_enabled
from services.global_writer_authority import evaluate_global_writer_authority


def methodology_enabled():
    value=current_app.config.get('SURVEY_METHODOLOGY_ENABLED',os.environ.get('SURVEY_METHODOLOGY_ENABLED','false'))
    return value is True or isinstance(value,str) and value.strip().lower() in ('true','1')


def _error(code,message,status=409,**extra):
    return EncuestaError(message,status_code=status,payload={
        'contract_version':CONTRACT,'reason_code':code,'retryable':False,**extra})


def _authorize(survey_id,actor):
    if canonical_role(getattr(actor,'rol',None)) not in (ROLE_TENANT_ADMIN,ROLE_SUPERADMIN):
        raise _error('methodology_admin_required','La ficha requiere un administrador de esta organizaci\u00f3n.',403)
    return get_encuesta(survey_id,user=actor)


def _timestamp(value):
    if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _document(row):
    return {'survey_id':row.survey_id,'tenant_id':row.tenant_id,'revision':row.revision,
        'instrument_revision':row.instrument_revision,'fields':row.fields,'change_reason':row.change_reason,
        'actor_user_id':row.actor_user_id,'created_at':_timestamp(row.created_at),'previous_digest':row.previous_digest}


def _verified(row):
    try:
        doc=_document(row)
        if validate_fields(row.fields)!=row.fields or canonical_digest(doc)!=row.digest:
            raise ValueError()
        if row.revision<1 or row.instrument_revision<1 or (row.revision==1 and row.previous_digest is not None):
            raise ValueError()
        if row.revision>1 and not re.fullmatch(r'[a-f0-9]{64}',row.previous_digest or ''):
            raise ValueError()
        return {**doc,'digest':row.digest}
    except (ValueError,TypeError,AttributeError):
        raise _error('methodology_history_invalid','El historial metodol\u00f3gico no pudo verificarse.',503) from None


def _query(survey):
    return SurveyMethodologyRevision.query.filter_by(tenant_id=survey.tenant_id,survey_id=survey.id)


def _payload(survey,*,requested_revision=None,available=True):
    latest=0;rows=[];selected=None
    if available:
        rows=_query(survey).order_by(SurveyMethodologyRevision.revision.desc()).limit(HISTORY_LIMIT+1).all()
        if rows:
            latest=rows[0].revision
            for index,row in enumerate(rows):
                _verified(row)
                if index and (rows[index-1].revision!=row.revision+1 or rows[index-1].previous_digest!=row.digest):
                    raise _error('methodology_history_invalid','El historial de versiones est\u00e1 incompleto.',503)
            selected=rows[0]
        if requested_revision is not None:
            selected=_query(survey).filter_by(revision=requested_revision).first()
            if selected is None:
                raise _error('methodology_revision_not_found','La versi\u00f3n solicitada no existe para esta encuesta.',404)
    doc=_verified(selected) if selected is not None else {
        'survey_id':survey.id,'tenant_id':survey.tenant_id,'revision':0,'instrument_revision':None,
        'fields':blank_fields(),'change_reason':None,'actor_user_id':None,'created_at':None,'previous_digest':None,'digest':None}
    current_revision=int(survey.structure_revision or 1)
    history=[{key:value for key,value in _verified(row).items() if key in ('revision','instrument_revision','created_at','actor_user_id','change_reason','digest')}
             for row in rows[:HISTORY_LIMIT]]
    return {'contract_version':CONTRACT,'available':available,'scope':{'survey_id':survey.id,'tenant_id':survey.tenant_id},
        'current_instrument_revision':current_revision,'latest_revision':latest,'profile':doc,
        'linked_instrument_changed':doc['instrument_revision'] is not None and doc['instrument_revision']!=current_revision,
        'capabilities':{'can_edit':bool(available and doc['revision']==latest and survey.estado!='archivada')},
        'coverage':coverage(doc['fields']),'history':history,'history_has_more':len(rows)>HISTORY_LIMIT,
        'schema':schema(),'ui':dict(UI),'inference_authorized':False,'result_changes_applied':False}


def get_methodology(survey_id,actor,revision=None):
    survey=_authorize(survey_id,actor)
    if revision is not None and (type(revision) is not int or revision<1 or revision>=2147483647):
        raise _error('methodology_revision_invalid','La versi\u00f3n solicitada no es v\u00e1lida.',400)
    return _payload(survey,requested_revision=revision,available=methodology_enabled())


def save_methodology(survey_id,actor,data):
    survey=_authorize(survey_id,actor)
    if not methodology_enabled():
        raise _error('methodology_not_enabled',UI['disabled'],503)
    if cutover_writer_fence_enabled(current_app.config) or not evaluate_global_writer_authority(current_app.config).allowed:
        raise _error('methodology_writer_unavailable','Las escrituras est\u00e1n suspendidas en este entorno.',503)
    try: payload=validate_write(data)
    except MethodologyInputError as err:
        raise _error(err.code,str(err),400,field=err.field) from None
    survey=_acquire_encuesta_write_guard(survey_id)
    current_actor=db.session.get(User,actor.id)
    if current_actor is None:
        raise _error('methodology_admin_required','La identidad administradora no est\u00e1 disponible.',403)
    db.session.refresh(current_actor)
    if canonical_role(current_actor.rol) not in (ROLE_TENANT_ADMIN,ROLE_SUPERADMIN):
        raise _error('methodology_admin_required','La identidad ya no tiene permiso para editar.',403)
    _ensure_tenant_access(survey,current_actor)
    if survey.estado=='archivada':
        raise _error('methodology_archived',UI['archive'])
    instrument_revision=int(survey.structure_revision or 1)
    if payload['expected_instrument_revision']!=instrument_revision:
        raise _error('methodology_instrument_conflict',UI['conflict'],409,current_instrument_revision=instrument_revision)
    latest=_query(survey).order_by(SurveyMethodologyRevision.revision.desc()).first()
    if latest is not None: _verified(latest)
    revision=latest.revision if latest else 0
    op_hash=canonical_digest({'survey_id':survey.id,'tenant_id':survey.tenant_id,'actor_user_id':current_actor.id,'request':payload})
    if payload['expected_revision']!=revision:
        if latest and latest.revision==payload['expected_revision']+1 and latest.operation_digest==op_hash:
            result={**_payload(survey),'replayed':True,'unchanged':False}
            db.session.rollback()  # Release the lock; do not create another version/audit.
            return result
        raise _error('methodology_revision_conflict',UI['conflict'],409,current_revision=revision)
    if latest and latest.fields==payload['fields'] and latest.instrument_revision==instrument_revision:
        result={**_payload(survey),'unchanged':True,'replayed':False}
        db.session.rollback()
        return result
    row=SurveyMethodologyRevision(tenant_id=survey.tenant_id,survey_id=survey.id,revision=revision+1,
        instrument_revision=instrument_revision,fields=payload['fields'],change_reason=payload['change_reason'],
        actor_user_id=current_actor.id,created_at=datetime.now(timezone.utc),previous_digest=latest.digest if latest else None,
        operation_digest=op_hash)
    row.digest=canonical_digest(_document(row))
    db.session.add(row)
    db.session.add(AuditEvent(tenant_id=survey.tenant_id,actor_user_id=current_actor.id,
        event_type='survey.methodology.revision_saved',resource_type='survey_methodology',resource_id=str(survey.id),
        details={'contract_version':CONTRACT,'revision':row.revision,'instrument_revision':instrument_revision,
                 'digest':row.digest,'previous_digest':row.previous_digest,'raw_fields_recorded':False},ip_address=None))
    db.session.flush()
    result={**_payload(survey),'replayed':False,'unchanged':False}
    db.session.commit()  # Declaration + audit are committed together, only after response construction succeeds.
    return result
