"""Tenant-scoped task commands with optimistic versions and transactional receipts."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from uuid import uuid4
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from database import db
from models import User
from models_memory import Contact
from models_crm_tasks import CrmTask, CrmTaskEvent
from utils.roles import canonical_role, is_authorized_superadmin_user

CONTRACT = 'crm.tasks.v1'
MANAGER_ROLES = {'admin', 'manager', 'supervisor'}
OPERATOR_ROLES = MANAGER_ROLES | {'empleado'}
STATUS_LABELS = {'todo': 'Pendiente', 'in_progress': 'En curso', 'done': 'Completada', 'cancelled': 'Cancelada'}
TRANSITIONS = {'todo': {'in_progress','done','cancelled'}, 'in_progress': {'todo','done','cancelled'}, 'done': {'todo'}, 'cancelled': {'todo'}}
PRIORITY_LABELS = {'normal':'Normal', 'high':'Alta', 'urgent':'Urgente'}


class TaskError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def manager(actor):
    return is_authorized_superadmin_user(actor) or canonical_role(actor.rol) in MANAGER_ROLES


def authorize(actor, tenant):
    if is_authorized_superadmin_user(actor):
        return
    owner_ids = {tenant.municipio_id, tenant.pyme_id} - {None}
    own = actor.tenant_id == tenant.id or (actor.tenant_id is None and actor.id in owner_ids)
    if not own or not actor.is_active or canonical_role(actor.rol) not in OPERATOR_ROLES:
        raise TaskError('task_access_denied', 'No tenés acceso a estas tareas.', 403)


def contact_for(tenant, contact_id):
    contact = Contact.query.filter_by(id=contact_id, tenant_id=tenant.id).first()
    if contact is None:
        raise TaskError('contact_not_found', 'Contacto no disponible.', 404)
    return contact


def eligible_users(tenant):
    owners = [value for value in [tenant.municipio_id, tenant.pyme_id] if value is not None]
    scope = or_(User.tenant_id == tenant.id, (User.tenant_id.is_(None) & User.id.in_(owners)))
    return [user for user in User.query.filter(scope).order_by(User.id).all()
            if canonical_role(user.rol) in OPERATOR_ROLES and user.is_active]


def assignee_for(tenant, value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value not in {u.id for u in eligible_users(tenant)}:
        raise TaskError('invalid_assignee', 'El responsable debe pertenecer a esta organización.')
    return value


def text_field(value, maximum, required=False):
    if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
        raise TaskError('invalid_text', f'El texto debe tener entre {1 if required else 0} y {maximum} caracteres.')
    return value.strip()


def parse_due(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise TaskError('invalid_due_at', 'La fecha debe incluir hora y zona horaria.')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except ValueError:
        raise TaskError('invalid_due_at', 'La fecha debe ser válida e incluir zona horaria.')


def iso(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat().replace('+00:00', 'Z') if value is not None else None


def snapshot(task):
    return {'id': task.id, 'tenant_id': task.tenant_id, 'contact_id': task.contact_id, 'title': task.title,
            'description': task.description, 'assignee_id': task.assignee_id, 'due_at': iso(task.due_at),
            'priority': task.priority, 'status': task.status, 'revision': task.revision,
            'created_by': task.created_by, 'updated_by': task.updated_by,
            'created_at': iso(task.created_at), 'updated_at': iso(task.updated_at)}


def permissions(actor, task):
    manage = manager(actor)
    allowed = TRANSITIONS[task.status] if manage else ({'in_progress','done'} & TRANSITIONS[task.status] if task.assignee_id == actor.id else set())
    return {'can_edit': manage and task.status in {'todo','in_progress'},
            'statuses': [{'value': status, 'label': STATUS_LABELS[status]} for status in STATUS_LABELS if status in allowed]}


def serialize(task, actor):
    return {**snapshot(task), 'permissions': permissions(actor, task)}


def receipt_parts(key, contact_id, task_id, payload):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
        raise TaskError('idempotency_key_required', 'La operación requiere una clave de idempotencia válida.')
    digest = sha256(key.encode()).hexdigest()
    try:
        content = json.dumps([contact_id, task_id, payload], sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError):
        raise TaskError('invalid_payload', 'La operación contiene valores JSON no admitidos.')
    return digest, sha256(content.encode()).hexdigest()


def replay(tenant, actor, key_hash, request_hash):
    found = CrmTaskEvent.query.filter_by(tenant_id=tenant.id, actor_id=actor.id, key_hash=key_hash).first()
    if found is None:
        return None
    if found.request_hash != request_hash:
        raise TaskError('idempotency_key_conflict', 'La clave ya fue utilizada para otra operación.', 409)
    return {'contract_version': CONTRACT, 'tenant_slug': tenant.slug, 'task': found.after_state,
            'receipt': {'event_id': found.id, 'revision': found.task_revision, 'key_hash': found.key_hash, 'replayed': True}}


def normalized_changes(tenant, actor, task, payload):
    if not isinstance(payload, dict) or set(payload) - {'title','description','assignee_id','due_at','priority','status','expected_revision','reason'}:
        raise TaskError('invalid_payload', 'La operación contiene campos no admitidos.')
    creating = task is None
    if creating and (not manager(actor) or 'status' in payload or 'expected_revision' in payload):
        raise TaskError('task_create_denied', 'No se permite crear esta tarea con esos campos.', 403)
    if not creating and (type(payload.get('expected_revision')) is not int or payload['expected_revision'] < 1):
        raise TaskError('revision_required', 'Se requiere la revisión exacta de la tarea.', 428)
    fields = set(payload) - {'expected_revision','reason'}
    if not creating:
        allowed = permissions(actor, task)
        if fields - {'status'} and not allowed['can_edit']:
            raise TaskError('task_edit_denied', 'No tenés permisos para editar estos campos.', 403)
        if 'status' in fields and (not isinstance(payload['status'], str) or payload['status'] not in {item['value'] for item in allowed['statuses']}):
            raise TaskError('invalid_transition', 'La transición de estado no está permitida.', 409)
    if not fields:
        raise TaskError('empty_update', 'No se recibieron cambios.')
    changes = {}
    for key, maximum, required in [('title',160,True),('description',3000,False)]:
        if key in payload or (creating and key == 'title'):
            changes[key] = text_field(payload.get(key), maximum, required)
    if 'assignee_id' in payload:
        changes['assignee_id'] = assignee_for(tenant, payload['assignee_id'])
    if 'due_at' in payload:
        changes['due_at'] = parse_due(payload['due_at'])
    if 'priority' in payload:
        if not isinstance(payload['priority'], str) or payload['priority'] not in PRIORITY_LABELS:
            raise TaskError('invalid_priority', 'Prioridad no permitida.')
        changes['priority'] = payload['priority']
    if 'status' in payload:
        changes['status'] = payload['status']
    return changes, text_field(payload.get('reason','Creación de tarea' if creating else ''),1200,True)


def mutate(tenant, actor, contact_id, payload, key, task_id=None):
    authorize(actor, tenant)
    contact_for(tenant, contact_id)
    task = None
    if task_id:
        task = CrmTask.query.filter_by(tenant_id=tenant.id, contact_id=contact_id, id=task_id).first()
        if task is None:
            raise TaskError('task_not_found', 'Tarea no disponible.', 404)
        if not manager(actor) and task.assignee_id != actor.id:
            raise TaskError('task_edit_denied', 'Sólo podés actualizar tus tareas asignadas.', 403)
    elif not manager(actor):
        raise TaskError('task_create_denied', 'Sólo un administrador o supervisor puede crear tareas.', 403)
    if not isinstance(payload, dict):
        raise TaskError('invalid_payload', 'Se requiere un objeto de datos.')
    key_hash, fingerprint = receipt_parts(key, contact_id, task_id, payload)
    previous = replay(tenant, actor, key_hash, fingerprint)
    if previous is not None:
        return previous
    # The loaded state must be the state the caller actually reviewed.
    # The conditional UPDATE below then detects writes racing after this check.
    if task is not None and type(payload.get('expected_revision')) is int and payload['expected_revision'] != task.revision:
        raise TaskError('stale_revision', 'La tarea cambió. Actualizá antes de volver a editar.', 409)
    changes, reason = normalized_changes(tenant, actor, task, payload)
    now = datetime.now(timezone.utc)
    before = snapshot(task) if task else None
    try:
        if task is None:
            task = CrmTask(id=str(uuid4()), tenant_id=tenant.id, contact_id=contact_id,
                created_by=actor.id, updated_by=actor.id, created_at=now, updated_at=now,
                status='todo', priority=changes.pop('priority','normal'), revision=1, **changes)
            db.session.add(task)
            db.session.flush()
        else:
            revision = payload['expected_revision']
            updates = {**changes, 'revision': revision + 1, 'updated_by': actor.id, 'updated_at': now}
            matched = CrmTask.query.filter_by(tenant_id=tenant.id, id=task.id, revision=revision).update(updates, synchronize_session=False)
            if matched != 1:
                db.session.rollback()
                recovered = replay(tenant, actor, key_hash, fingerprint)
                if recovered is not None:
                    return recovered
                raise TaskError('stale_revision', 'La tarea cambió. Actualizá antes de volver a editar.', 409)
            db.session.refresh(task)
        after = snapshot(task)
        event = CrmTaskEvent(id=str(uuid4()), tenant_id=tenant.id, task_id=task.id,
            task_revision=task.revision, actor_id=actor.id, operation='updated' if before else 'created',
            reason=reason, key_hash=key_hash, request_hash=fingerprint,
            before_state=before, after_state=after, created_at=now)
        append_event(event)
        receipt = {'event_id': event.id, 'revision': task.revision, 'key_hash': key_hash, 'replayed': False}
        db.session.commit()
        return {'contract_version': CONTRACT, 'tenant_slug': tenant.slug, 'task': after, 'receipt': receipt}
    except IntegrityError:
        db.session.rollback()
        recovered = replay(tenant, actor, key_hash, fingerprint)
        if recovered is not None:
            return recovered
        raise TaskError('task_transaction_conflict', 'La operación no se confirmó. Conservá la clave antes de reintentar.', 409)
    except Exception:
        db.session.rollback()
        raise


def task_list(tenant, actor, contact_id, cursor=None):
    authorize(actor, tenant)
    contact_for(tenant, contact_id)
    query = CrmTask.query.filter_by(tenant_id=tenant.id, contact_id=contact_id)
    from sqlalchemy import func
    grouped = query.with_entities(CrmTask.status, func.count(CrmTask.id)).group_by(CrmTask.status).all()
    counts = {status: 0 for status in STATUS_LABELS}
    counts.update(dict(grouped))
    total = sum(counts.values())
    if cursor:
        anchor = query.filter_by(id=cursor).first()
        if anchor is None:
            raise TaskError('invalid_cursor', 'El cursor no corresponde a este contacto.')
        query = query.filter(or_(CrmTask.created_at < anchor.created_at,
            (CrmTask.created_at == anchor.created_at) & (CrmTask.id < anchor.id)))
    rows = query.order_by(CrmTask.created_at.desc(), CrmTask.id.desc()).limit(51).all()
    return {'contract_version': CONTRACT, 'tenant_slug': tenant.slug, 'contact_id': contact_id,
            'items': [serialize(task, actor) for task in rows[:50]], 'total': total, 'counts': counts,
            'next_cursor': rows[49].id if len(rows) > 50 else None}


def append_event(event):
    db.session.add(event)
    db.session.flush()
