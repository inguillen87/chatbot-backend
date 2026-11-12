"""Safe HTTP contract for typed database dependency failures.

These exception classes cannot tell whether retrying is safe or when quota will
recover. Do not advertise automatic retries or pre-dispatch write guarantees.
"""
import uuid

from flask import current_app, g, jsonify, request
from sqlalchemy.exc import DisconnectionError, OperationalError, TimeoutError

from database import db


DATABASE_AVAILABILITY_ERRORS = (OperationalError, DisconnectionError, TimeoutError)


def rollback_database_session_safely():
    try:
        db.session.rollback()
    except Exception as error:
        current_app.logger.warning('Database rollback unavailable error_type=%s', type(error).__name__)


def database_unavailable_response(*, reason_code='database_unavailable'):
    if reason_code not in {'database_unavailable', 'tenant_context_unavailable'}:
        raise ValueError('Unsupported database availability reason')
    rollback_database_session_safely()

    request_id = str(request.headers.get('X-Request-Id')
                     or request.headers.get('X-Correlation-Id')
                     or getattr(g, 'request_id', None) or '').strip() or uuid.uuid4().hex
    g.request_id = request_id
    message = 'El servicio no está disponible temporalmente. Intentá nuevamente más tarde.'
    response = jsonify({
        'contract_version': 'shared.error.v1',
        'status_code': 503,
        'reason_code': reason_code,
        'retryable': False,
        'action_hint': 'retry_later',
        'request_id': request_id,
        'error': {'code': 503, 'message': message},
        'message': message,
        'detail': message,
    })
    response.status_code = 503
    response.headers['X-Request-Id'] = request_id
    response.headers['Cache-Control'] = 'no-store'
    return response
