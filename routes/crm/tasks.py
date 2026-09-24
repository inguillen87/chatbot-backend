"""Independent tasks; disabled by default until the coordinated schema rollout."""
import os
from functools import wraps
from flask import Blueprint, current_app, g, jsonify, request
from database import db
from utils.auth_helpers import token_requerido
from middleware.tenant_context import require_tenant
from routes.crm.routes import require_crm_tenant_operator

crm_tasks_bp = Blueprint('crm_tasks_bp', __name__)
PREFIX = '/api/admin/tenants/<slug>'
CONTRACT = 'crm.tasks.v1'


def rollout_status():
    from services.crm_task_rollout import task_rollout_decision, KEYS
    config = {key: current_app.config.get(key, os.getenv(key, '')) for key in KEYS}
    decision = task_rollout_decision(config, getattr(g.tenant_profile, 'id', None))
    ready = False
    if decision['eligible']:
        from services.crm_task_schema import task_schema_ready
        try:
            ready = task_schema_ready()
        except Exception as exc:
            current_app.logger.warning('CRM task schema unavailable: %s', type(exc).__name__)
        decision['reason_code'] = 'ready' if ready else 'schema_not_ready'
    return {'available': ready, 'activation_mode': 'explicit_tenant_list',
            'tenant_selected': decision['tenant_selected'], 'reason_code': decision['reason_code']}


def ready():
    return rollout_status()['available']


def failure(code, message, status):
    return jsonify({'contract_version': CONTRACT, 'tenant_slug': g.tenant_profile.slug,
                    'error': {'code': code, 'message': message}}), status


def task_endpoint(fn):
    @wraps(fn)
    def guarded(actor, *args, **kwargs):
        if not ready():
            return failure('tasks_unavailable', 'Las tareas no están activadas para esta instalación.', 503)
        from services.crm_tasks import TaskError
        try:
            return fn(actor, *args, **kwargs)
        except TaskError as exc:
            db.session.rollback()
            return failure(exc.code, str(exc), exc.status)
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning('CRM task operation unconfirmed: %s', type(exc).__name__)
            return failure('task_unconfirmed', 'No se confirmó la operación. Conservá la clave para verificarla.', 503)
    return guarded


@crm_tasks_bp.get(PREFIX + '/crm/tasks/capabilities')
@token_requerido
@require_tenant
@require_crm_tenant_operator
def capabilities(actor, slug):
    rollout = rollout_status()
    available = rollout['available']
    body = {'contract_version': CONTRACT, 'tenant_slug': slug, 'available': available, 'rollout': rollout}
    if available:
        from services.crm_tasks import authorize, manager, STATUS_LABELS, PRIORITY_LABELS, TaskError
        try:
            authorize(actor, g.tenant_profile)
        except TaskError as exc:
            return failure(exc.code, str(exc), exc.status)
        body.update(can_create=manager(actor), statuses=STATUS_LABELS, priorities=PRIORITY_LABELS,
            ui={'title':'Tareas del contacto', 'create':'Crear tarea', 'save':'Guardar cambios',
                'assignee':'Responsable', 'due':'Vencimiento', 'reason':'Motivo del cambio',
                'history':'Historial de la tarea', 'unassigned':'Sin asignar',
                'description':'Varias tareas independientes, con responsable, versión e historial.'})
    return jsonify(body)


@crm_tasks_bp.get(PREFIX + '/crm/tasks/assignees')
@token_requerido
@require_tenant
@require_crm_tenant_operator
@task_endpoint
def assignees(actor, slug):
    from services.crm_tasks import authorize, eligible_users
    authorize(actor, g.tenant_profile)
    return jsonify({'contract_version':CONTRACT,'tenant_slug':slug,
        'items':[{'id':user.id,'name':user.name or f'Usuario #{user.id}'} for user in eligible_users(g.tenant_profile)]})


@crm_tasks_bp.route(PREFIX + '/contacts/<contact_id>/tasks', methods=['GET','POST'])
@token_requerido
@require_tenant
@require_crm_tenant_operator
@task_endpoint
def collection(actor, slug, contact_id):
    from services.crm_tasks import task_list, mutate
    if request.method == 'GET':
        return jsonify(task_list(g.tenant_profile, actor, contact_id, request.args.get('cursor')))
    result = mutate(g.tenant_profile, actor, contact_id, request.get_json(silent=True), request.headers.get('Idempotency-Key'))
    return jsonify(result), 200 if result['receipt']['replayed'] else 201


@crm_tasks_bp.route(PREFIX + '/contacts/<contact_id>/tasks/<task_id>', methods=['GET','PATCH'])
@token_requerido
@require_tenant
@require_crm_tenant_operator
@task_endpoint
def detail(actor, slug, contact_id, task_id):
    from services.crm_tasks import authorize, contact_for, serialize, mutate, TaskError, iso
    from models_crm_tasks import CrmTask, CrmTaskEvent
    authorize(actor, g.tenant_profile)
    contact_for(g.tenant_profile, contact_id)
    if request.method == 'PATCH':
        result = mutate(g.tenant_profile, actor, contact_id, request.get_json(silent=True), request.headers.get('Idempotency-Key'), task_id)
        return jsonify(result)
    task = CrmTask.query.filter_by(tenant_id=g.tenant_profile.id, contact_id=contact_id, id=task_id).first()
    if task is None:
        raise TaskError('task_not_found','Tarea no disponible.',404)
    events = CrmTaskEvent.query.filter_by(tenant_id=task.tenant_id, task_id=task.id)
    before = request.args.get('before_revision')
    if before is not None:
        try:
            before = int(before)
            if before < 1:
                raise ValueError()
        except ValueError:
            raise TaskError('invalid_history_cursor','Cursor de historial inválido.')
        events = events.filter(CrmTaskEvent.task_revision < before)
    rows = events.order_by(CrmTaskEvent.task_revision.desc()).limit(21).all()
    return jsonify({'contract_version':CONTRACT,'tenant_slug':slug,'task':serialize(task,actor),
        'events':[{'id':event.id,'revision':event.task_revision,'actor_id':event.actor_id,
            'operation':event.operation,'reason':event.reason,'at':iso(event.created_at),
            'before':event.before_state,'after':event.after_state} for event in rows[:20]],
        'next_before_revision':rows[19].task_revision if len(rows)>20 else None})
